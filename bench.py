#!/usr/bin/env python3
"""Local-only performance harness for archive uploads (stdlib only, no network).

Measures local CPU/disk stages with mocked Telegram sends so runs are
deterministic, free, and spam-free:

  .venv/bin/python bench.py --suite all --fast
  .venv/bin/python bench.py --suite month --seed 7 --compare
  .venv/bin/python bench.py --suite split --split-mb 1 --fast --compare

Suites (each builds its own deterministic fake library under a temp dir):
  daily : 10 x 2MB across 1 day   (scan_day + zip + deliver)
  month : 100 x 2MB across 10 days, SAME filenames each day
          (exposes flat-arcname collisions in period zips)
  split : 3 x 5MB across 1 day, split_mb=1 (forces multi-part path)

Baselines append as JSON rows to logs/bench.jsonl (local only, never commit).
Use --compare to diff against the previous matching row, then optimize one
stage at a time and re-run with the same --suite/--seed/--fast/--split-mb.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
import tracemalloc
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
BENCH_LOG = BASE_DIR / "logs" / "bench.jsonl"

import bot  # noqa: E402  (local module under test)

log = logging.getLogger("bench")

CHUNK = 1024 * 1024  # 1MB fixture write granularity


def rss_mb() -> float:
    """Current process high-water RSS in MB (0.0 if unavailable)."""
    try:
        import resource
        import sys as _sys
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux reports kilobytes.
        if _sys.platform == "darwin":
            return rss / 1024 / 1024
        return rss / 1024
    except Exception:
        return 0.0


def write_random(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as h:
        remaining = size
        while remaining > 0:
            n = min(CHUNK, remaining)
            h.write(os.urandom(n))
            remaining -= n


def build_fixture(root: Path, suite: str, seed: int, fast: bool):
    """Create fake library tree. Returns (user_id, period_s, day_dirs)."""
    rng = random.Random(seed)  # reserved for future shuffling; names stay deterministic
    _ = rng
    div = 10 if fast else 1
    user = "benchuser"
    if suite == "daily":
        days = ["2026-09-01"]
        per_day, size = 10, (2 * 1024 * 1024) // div
        names = [f"f{i:02d}.jpg" for i in range(per_day)]
    elif suite == "month":
        days = [f"2026-09-{d:02d}" for d in range(1, 11)]
        per_day, size = 10, (2 * 1024 * 1024) // div
        names = [f"dup{i:02d}.jpg" for i in range(per_day)]  # same names every day
    else:  # split
        days = ["2026-09-01"]
        per_day, size = 3, (5 * 1024 * 1024) // div
        names = [f"s{i:02d}.jpg" for i in range(per_day)]
    for day in days:
        ddir = root / user / day[:4] / day
        for name in names:
            write_random(ddir / name, size)
    return user, "2026-09", [root / user / days[0][:4] / d for d in days]


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


class StubTG:
    """Zero-latency stand-in for bot.Telegram (pacing sleeps are patched out)."""

    def __init__(self):
        self.docs: list[tuple[str, str]] = []

    def send_document(self, chat_id, path, caption, message_thread_id=None):
        self.docs.append((Path(path).name, caption))

    def send_media_group(self, chat_id, items, caption, message_thread_id=None):
        self.docs.append((f"media-group-{len(items)}", caption))


def timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, time.perf_counter() - t0


def run_suite(suite: str, seed: int, fast: bool, split_mb: float | None,
              simulate_net_mbps: float | None, verbose: bool):
    default_split = 1.0 if suite == "split" else 1900.0
    split = default_split if split_mb is None else split_mb
    stages: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix=f"bench-{suite}-lib-") as lib_s, \
            tempfile.TemporaryDirectory(prefix=f"bench-{suite}-tmp-") as tmp_s:
        libroot, tmpdir = Path(lib_s), Path(tmp_s)
        user, period_s, day_dirs = build_fixture(libroot, suite, seed, fast)
        cfg = {
            "library_root": str(libroot),
            "split_mb": split,
            "fallback_split_mb": 45.0,
            "archive_via": "bot",
            "userbot_threshold_mb": 50.0,
            "archive_chat_id": "-100bench",
            "archive_thread_id": 3,
            "gallery_chat_id": "-100bench",
            "aliases": {},
        }

        # 1. collect / scan
        if suite == "daily":
            files, dt_collect = timed(bot.scan_day, day_dirs[0])
        else:
            files, dt_collect = timed(bot.collect_period_files, cfg, user, period_s)
        stages["collect"] = dt_collect
        input_bytes = sum(f.stat().st_size for f in files)
        input_mb = input_bytes / 1024 / 1024

        # 2. gallery prep (secondary; archives-first focus)
        (prep, dt_gallery) = timed(bot.prepare_gallery_items, files, tmpdir)
        stages["gallery_prep"] = dt_gallery
        if verbose:
            log.info("gallery sendable=%d notes=%d", len(prep[0]), len(prep[1]))

        # 3. zip (+split inside)
        parts, dt_zip = timed(bot.make_zip_and_split, files, user, period_s, tmpdir, split)
        stages["zip_split"] = dt_zip
        zip_bytes = sum(p.stat().st_size for p in parts)
        tmp_peak = dir_size(tmpdir)

        # collision signal: flat arcnames repeat across days in period zips
        collisions = 0
        if len(parts) == 1 and parts[0].suffix == ".zip":
            try:
                with zipfile.ZipFile(parts[0]) as zf:
                    names = zf.namelist()
                collisions = len(files) - len(set(names))
            except Exception as e:
                log.warning("zip inspect failed: %s", e)

        # 4. isolated split_file on a standalone blob (isolates split cost/RSS)
        blob = tmpdir / "blob.bin"
        blob_size = min(input_bytes, (2 * 1024 * 1024) if fast else (20 * 1024 * 1024))
        write_random(blob, max(blob_size, 1))
        _parts2, dt_split = timed(bot.split_file, blob, split if suite == "split" else 1.0)
        stages["split_isolated"] = dt_split
        for p in _parts2:  # keep tmp_peak measurement above unpolluted
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

        # 5. delivery loop with stub sender, pacing sleeps patched out
        stub = StubTG()
        real_sleep = bot.time.sleep
        bot.time.sleep = lambda s: None
        try:
            _n, dt_deliver = timed(bot.post_archive, stub, cfg["archive_chat_id"],
                                   user, period_s, parts, cfg["archive_thread_id"],
                                   cfg["fallback_split_mb"])
        finally:
            bot.time.sleep = real_sleep
        stages["deliver_stub"] = dt_deliver

        # 6. state save/load roundtrip
        state_path = tmpdir / "state.json"
        state = {f"{user}/{period_s}": {"archive_done": True, "n": 1}}
        _, dt_state_save = timed(bot.save_state, str(state_path), state)
        _, dt_state_load = timed(bot.load_state, str(state_path))
        stages["state_io"] = dt_state_save + dt_state_load

        peak_rss = rss_mb()
        traced_peak_mb = tracemalloc.get_traced_memory()[1] / 1024 / 1024
        total = sum(stages.values())
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "suite": suite,
            "seed": seed,
            "fast": fast,
            "split_mb": split,
            "input_files": len(files),
            "input_mb": round(input_mb, 2),
            "zip_mb": round(zip_bytes / 1024 / 1024, 2),
            "parts": len(parts),
            "collisions": collisions,
            "stages_s": {k: round(v, 3) for k, v in stages.items()},
            "total_s": round(total, 3),
            "peak_rss_mb": round(peak_rss, 1),
            "tracemalloc_peak_mb": round(traced_peak_mb, 1),
            "tmp_peak_mb": round(tmp_peak / 1024 / 1024, 2),
            "deliver_docs": len(stub.docs),
            "pacing": "excluded (sleep patched out)",
        }
        if simulate_net_mbps:
            # computed only, never slept: bytes -> bits / mbps
            row["est_net_s"] = round((zip_bytes * 8) / (simulate_net_mbps * 1e6), 1)
            row["simulate_net_mbps"] = simulate_net_mbps
        return row


def print_row(row: dict) -> None:
    print(f"\n== {row['suite']} (seed={row['seed']} fast={row['fast']} split={row['split_mb']}) ==")
    print(f"   input: {row['input_files']} files, {row['input_mb']} MB "
          f"-> zip {row['zip_mb']} MB in {row['parts']} part(s), "
          f"collisions={row['collisions']}, tmp_peak={row['tmp_peak_mb']} MB")
    for stage, sec in row["stages_s"].items():
        mbps = ""
        if stage in ("zip_split", "split_isolated") and sec > 0:
            mbps = f" ({row['input_mb'] / sec:.1f} MB/s)" if stage == "zip_split" else ""
        print(f"   {stage:15s} {sec:8.3f}s{mbps}")
    print(f"   {'total':15s} {row['total_s']:8.3f}s   "
          f"peak_rss={row['peak_rss_mb']} MB  pymem={row['tracemalloc_peak_mb']} MB")
    if "est_net_s" in row:
        print(f"   est_net @{row['simulate_net_mbps']}Mbps ~{row['est_net_s']}s (computed, not sent)")


def find_baseline(suite: str, seed: int, fast: bool, split_mb: float):
    if not BENCH_LOG.exists():
        return None
    prev = None
    for line in BENCH_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if (r.get("suite") == suite and r.get("seed") == seed
                and r.get("fast") == fast and r.get("split_mb") == split_mb):
            prev = r
    return prev


def print_compare(prev: dict, cur: dict) -> None:
    print(f"\n-- compare vs {prev['ts']} --")
    for stage, sec in cur["stages_s"].items():
        old = prev.get("stages_s", {}).get(stage)
        if old is None:
            continue
        d = sec - old
        pct = (d / old * 100) if old else 0.0
        print(f"   {stage:15s} {old:8.3f}s -> {sec:8.3f}s  ({d:+.3f}s, {pct:+.1f}%)")
    for key in ("total_s", "peak_rss_mb", "tracemalloc_peak_mb", "tmp_peak_mb"):
        old, new = prev.get(key), cur.get(key)
        if old is None or new is None:
            continue
        d = new - old
        print(f"   {key:15s} {old:8.1f} -> {new:8.1f}  ({d:+.1f})")


def main() -> int:
    p = argparse.ArgumentParser(description="Local-only perf harness (stdlib, mocked sends).")
    p.add_argument("--suite", choices=["daily", "month", "split", "all"], default="all")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--fast", action="store_true", help="10x smaller fixtures for quick iteration")
    p.add_argument("--split-mb", type=float, default=None, help="override suite default split size")
    p.add_argument("--simulate-net-mbps", type=float, default=None,
                   help="add computed (not slept) network estimate column")
    p.add_argument("--compare", action="store_true", help="diff against last matching baseline row")
    p.add_argument("--no-save", action="store_true", help="do not append to logs/bench.jsonl")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    tracemalloc.start()
    suites = ["daily", "month", "split"] if args.suite == "all" else [args.suite]
    rows = []
    for suite in suites:
        prev = find_baseline(suite, args.seed, args.fast,
                             args.split_mb if args.split_mb is not None
                             else (1.0 if suite == "split" else 1900.0)) if args.compare else None
        row = run_suite(suite, args.seed, args.fast, args.split_mb,
                        args.simulate_net_mbps, args.verbose)
        print_row(row)
        if prev:
            print_compare(prev, row)
        rows.append(row)

    if not args.no_save:
        BENCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(BENCH_LOG, "a") as h:
            for row in rows:
                h.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"\nSaved {len(rows)} row(s) to {BENCH_LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
