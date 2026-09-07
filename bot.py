#!/usr/bin/env python3
"""Immich daily Telegram bot.

Modes:
  python bot.py --today                          # nightly: local today, all users
  python bot.py --date 2026-09-06                # manual push one day
  python bot.py --date 2026-09-06 --user tony    # one user only
  python bot.py --range 2026-09-01:2026-09-05    # backfill inclusive
  python bot.py --range 2026-09-01:2026-09-05 --user tony --user ninnette
  python bot.py --month 2026-09                  # archive-only: whole month, one zip split at SPLIT_MB
  python bot.py --year 2026                      # archive-only: whole year, one zip split at SPLIT_MB
  python bot.py --list-users                     # discover library user dirs
  python bot.py --date 2026-09-06 --dry-run      # plan only, no Telegram calls

Per user per date it:
  1. posts scrollable gallery to GALLERY_CHAT_ID (sendMediaGroup, 10/chunk,
     HEIC->JPEG converted, MOV sent as video)
  2. posts zipped originals to ARCHIVE_CHAT_ID (sendDocument, split at SPLIT_MB)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE_DEFAULT = BASE_DIR / "state.json"
LOG_FILE_DEFAULT = BASE_DIR / "logs" / "bot.log"

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
HEIC_EXTS = {".heic", ".heif"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
# 10MB Bot API photo-in-group practical cap; videos/documents 50MB (we use 45MB split default)
PHOTO_MAX_BYTES = 10 * 1024 * 1024

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
YEAR_RE = re.compile(r"^\d{4}$")

log = logging.getLogger("immich-bot")


# ---------- config ----------

def load_dotenv_fallback(env_path: Path) -> None:
    """Minimal .env loader if python-dotenv is unavailable."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        os.environ.setdefault(k, v)


def _parse_thread_id(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"Bad thread ID '{value}', expected an integer (e.g. 3)")


def load_config() -> dict:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(BASE_DIR / ".env")
    except Exception:
        load_dotenv_fallback(BASE_DIR / ".env")

    cfg = {
        "bot_token": os.environ.get("BOT_TOKEN", ""),
        "gallery_chat_id": os.environ.get("GALLERY_CHAT_ID", ""),
        "gallery_thread_id": _parse_thread_id(os.environ.get("GALLERY_THREAD_ID")),
        "archive_chat_id": os.environ.get("ARCHIVE_CHAT_ID", ""),
        "archive_thread_id": _parse_thread_id(os.environ.get("ARCHIVE_THREAD_ID")),
        "library_root": os.environ.get(
            "LIBRARY_ROOT",
            "/Volumes/ExFAT/immich-data/library/373b4d5c-77f3-41e6-9dd0-e87e8b476554",
        ),
        # LIBRARY_ROOT may point at a single user dir (legacy) or the parent
        # `.../library` containing many user dirs. Both are supported.
        "tz": os.environ.get("TZ", "Asia/Singapore"),
        "split_mb": float(os.environ.get("SPLIT_MB", "1900")),
        "fallback_split_mb": float(os.environ.get("FALLBACK_SPLIT_MB", "45")),
        "archive_via": os.environ.get("ARCHIVE_VIA", "auto").strip().lower(),
        "userbot_threshold_mb": float(os.environ.get("USERBOT_THRESHOLD_MB", "50")),
        "state_file": os.environ.get("STATE_FILE", str(STATE_FILE_DEFAULT)),
        "aliases": parse_aliases(os.environ),
    }
    return cfg


def parse_aliases(env: dict) -> dict[str, str]:
    """Return {alias_lower: user_dirname}.

    Supports:
      USER_ALIASES="tony:373b4d5c-...,ninnette:admin"
      USER_TONY=373b4d5c-...  (any USER_<ALIAS>=<dirname>)
    """
    aliases: dict[str, str] = {}
    raw = env.get("USER_ALIASES", "")
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        alias, uid = part.split(":", 1)
        aliases[alias.strip().lower()] = uid.strip()
    for k, v in env.items():
        if k.startswith("USER_") and k != "USER_ALIASES" and v.strip():
            aliases[k[5:].strip().lower()] = v.strip()
    return aliases


# ---------- users / dates ----------

def discover_users(library_root: str) -> list[str]:
    """List user dir names. Handles LIBRARY_ROOT being either .../library or .../library/<user>."""
    root = Path(library_root)
    if not root.exists():
        return []
    # Legacy: LIBRARY_ROOT points directly at a single user's folder (contains YYYY dirs)
    if any(p.is_dir() and re.fullmatch(r"\d{4}", p.name) for p in root.iterdir()):
        return [root.name]
    users = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    return users


def library_base(cfg: dict) -> Path:
    """Parent dir that contains user dirs."""
    return _cached_library_base(cfg["library_root"])


@lru_cache(maxsize=8)
def _cached_library_base(library_root: str) -> Path:
    root = Path(library_root)
    try:
        if any(p.is_dir() and re.fullmatch(r"\d{4}", p.name) for p in root.iterdir()):
            return root.parent
    except OSError:
        pass
    return root


def resolve_user_dir(cfg: dict, user_id: str) -> Path:
    return library_base(cfg) / user_id


def user_label(cfg: dict, user_id: str) -> str:
    rev = cfg.get("_alias_rev")
    if rev is None:
        rev = {}
        for alias, uid in cfg["aliases"].items():
            rev.setdefault(uid, alias)
        cfg["_alias_rev"] = rev
    return rev.get(user_id, user_id[:8])


def resolve_user_selection(cfg: dict, users_arg: list[str], all_users: bool) -> list[str]:
    discovered = discover_users(cfg["library_root"])
    if not discovered:
        raise SystemExit(f"No user folders found under {cfg['library_root']}")
    if not users_arg and not all_users:
        # Default nightly behaviour: all users.
        return discovered
    if all_users and not users_arg:
        return discovered
    selected: list[str] = []
    for u in users_arg:
        key = u.strip().lower()
        uid = cfg["aliases"].get(key, u.strip())  # alias -> dirname, else raw dirname
        if uid not in discovered:
            raise SystemExit(
                f"Unknown user '{u}'. Discovered: {discovered}. "
                f"Aliases: {cfg['aliases'] or '(none — set USER_ALIASES in .env)'}"
            )
        if uid not in selected:
            selected.append(uid)
    return selected


def local_today(cfg: dict) -> str:
    try:
        tz = ZoneInfo(cfg["tz"])
    except Exception:
        tz = ZoneInfo("Asia/Singapore")
    return dt.datetime.now(tz).strftime("%Y-%m-%d")


def expand_dates(args, cfg: dict) -> list[str]:
    if args.range:
        try:
            start_s, end_s = args.range.split(":", 1)
        except ValueError:
            raise SystemExit("--range must be START:END, e.g. 2026-09-01:2026-09-05")
        for s in (start_s, end_s):
            if not DATE_RE.match(s):
                raise SystemExit(f"Bad date '{s}', expected YYYY-MM-DD")
        start = dt.date.fromisoformat(start_s)
        end = dt.date.fromisoformat(end_s)
        if start > end:
            raise SystemExit("--range START must be <= END")
        if (end - start).days > 366:
            raise SystemExit("--range limited to 366 days per run")
        return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    if args.date:
        if not DATE_RE.match(args.date):
            raise SystemExit(f"Bad --date '{args.date}', expected YYYY-MM-DD")
        return [args.date]
    return [local_today(cfg)]  # --today or bare default


def day_dir(cfg: dict, user_id: str, date_s: str) -> Path:
    return resolve_user_dir(cfg, user_id) / date_s[:4] / date_s


def scan_day(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(
        (p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def validate_period(args) -> str | None:
    """Return the requested period 'YYYY-MM' / 'YYYY', or None for daily modes.

    --month and --year are mutually exclusive with --today/--date/--range
    (enforced by argparse) and are archive-only.
    """
    period = args.month or args.year
    if period is None:
        return None
    if args.month:
        if not MONTH_RE.match(args.month):
            raise SystemExit(f"Bad --month '{args.month}', expected YYYY-MM")
        if not 1 <= int(args.month[5:7]) <= 12:
            raise SystemExit(f"Bad --month '{args.month}', month must be 01-12")
    else:
        if not YEAR_RE.match(args.year):
            raise SystemExit(f"Bad --year '{args.year}', expected YYYY")
    return period


def iter_day_dirs(year_dir: Path, prefix: str | None) -> list[Path]:
    """Day dirs (YYYY-MM-DD) under a year dir, optionally filtered by prefix."""
    return sorted(
        p for p in year_dir.iterdir()
        if p.is_dir() and DATE_RE.match(p.name) and (prefix is None or p.name.startswith(prefix))
    )


def collect_period_files(cfg: dict, user_id: str, period_s: str) -> list[Path]:
    """Collect all files for a month (YYYY-MM) or year (YYYY) for one user.

    Layout is <user>/<YYYY>/<YYYY-MM-DD>/files. Returns files ordered by
    day dir then filename.
    """
    user_dir = resolve_user_dir(cfg, user_id)
    if len(period_s) == 7:  # YYYY-MM
        year, prefix = period_s[:4], period_s
    else:  # YYYY
        year, prefix = period_s, None
    year_dir = user_dir / year
    if not year_dir.is_dir():
        return []
    files: list[Path] = []
    for d in iter_day_dirs(year_dir, prefix):
        files.extend(scan_day(d))
    return files


# ---------- state ----------

def load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


# ---------- media prep ----------

_HEIF_READY: bool | None = None


def _heif_ready() -> bool:
    """Import HEIC support once per run instead of once per file."""
    global _HEIF_READY
    if _HEIF_READY is None:
        try:
            from pillow_heif import register_heif_opener  # type: ignore
            register_heif_opener()
            _HEIF_READY = True
        except Exception as e:
            log.warning("HEIC support unavailable (%s); HEIC files get zip-only, no gallery preview", e)
            _HEIF_READY = False
    return _HEIF_READY


def convert_heic_to_jpeg(src: Path, tmpdir: Path) -> Path | None:
    """Convert HEIC/HEIF to JPEG. Returns path or None on failure."""
    if not _heif_ready():
        log.warning("HEIC support unavailable; will zip %s without gallery preview", src.name)
        return None
    try:
        from PIL import Image, ImageOps
    except Exception as e:
        log.warning("Pillow unavailable (%s); will zip %s without gallery preview", e, src.name)
        return None
    try:
        img = Image.open(src)
        img = ImageOps.exif_transpose(img).convert("RGB")
        # Downscale safety so sendMediaGroup photo stays under ~10MB
        img.thumbnail((2560, 2560), Image.LANCZOS)
        out = tmpdir / (src.stem + ".jpg")
        img.save(out, "JPEG", quality=90)
        if out.stat().st_size > PHOTO_MAX_BYTES:
            # Second pass, smaller
            img.thumbnail((1920, 1920), Image.LANCZOS)
            img.save(out, "JPEG", quality=85)
        return out
    except Exception as e:
        log.warning("HEIC convert failed for %s: %s", src, e)
        return None


def prepare_gallery_items(files: list[Path], tmpdir: Path,
                            convert_heic: bool = True) -> tuple[list[dict], list[str]]:
    """Return (sendable items, skipped notes).

    item = {"kind": "photo"|"video", "path": Path, "name": str}
    With convert_heic=False (dry-run estimates), HEICs count as sendable
    without paying for conversion.
    """
    items: list[dict] = []
    notes: list[str] = []
    for f in files:
        ext = f.suffix.lower()
        size = f.stat().st_size  # single stat per file; missing files raise (caught upstream)
        if ext in PHOTO_EXTS:
            if size > PHOTO_MAX_BYTES:
                notes.append(f"{f.name} skipped in gallery (>10MB photo, still in zip)")
                continue
            items.append({"kind": "photo", "path": f, "name": f.name})
        elif ext in HEIC_EXTS:
            if not convert_heic:
                items.append({"kind": "photo", "path": f, "name": f"{f.name} (as JPEG)"})
                continue
            jpg = convert_heic_to_jpeg(f, tmpdir)
            if jpg is None:
                notes.append(f"{f.name} HEIC convert failed (still in zip)")
                continue
            items.append({"kind": "photo", "path": jpg, "name": f"{f.name} (as JPEG)"})
        elif ext in VIDEO_EXTS:
            if size > 45 * 1024 * 1024:
                notes.append(f"{f.name} too large for gallery wall (still in zip)")
                continue
            items.append({"kind": "video", "path": f, "name": f.name})
        else:
            notes.append(f"{f.name} zip-only (unsupported gallery type)")
    return items, notes


# ---------- telegram ----------

class Telegram:
    def __init__(self, token: str, dry_run: bool = False):
        self.token = token
        self.dry_run = dry_run
        self.s = requests.Session()

    def _post(self, method: str, data=None, files=None, retries: int = 4):
        if self.dry_run:
            log.info("[dry-run] would call %s with %s", method, list((data or {}).keys()))
            return {"ok": True, "result": []}
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        backoff = 2.0
        for attempt in range(1, retries + 1):
            try:
                r = self.s.post(url, data=data, files=files, timeout=120)
            except requests.RequestException as e:
                log.warning("%s attempt %d network error: %s", method, attempt, e)
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", backoff))
                except Exception:
                    wait = backoff
                log.warning("Rate limited, sleeping %.0fs", wait)
                time.sleep(wait)
                backoff *= 2
                continue
            if r.status_code >= 500:
                log.warning("%s attempt %d HTTP %d", method, attempt, r.status_code)
                time.sleep(backoff)
                backoff *= 2
                continue
            try:
                body = r.json()
            except Exception:
                raise RuntimeError(f"{method} bad response HTTP {r.status_code}: {r.text[:300]}")
            if not body.get("ok"):
                desc = body.get("description", "unknown error")
                if "thread" in desc.lower():
                    raise RuntimeError(
                        f"{method} failed: {desc} "
                        "(hint: thread IDs only work in a forum supergroup with "
                        "Topics enabled; check GALLERY_/ARCHIVE_THREAD_ID)"
                    )
                if "retry_after" in str(body) or "too many" in desc.lower():
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise RuntimeError(f"{method} failed: {desc}")
            return body
        raise RuntimeError(f"{method} failed after {retries} retries")

    def send_media_group(self, chat_id: str, items: list[dict], caption: str,
                         message_thread_id: int | None = None) -> None:
        """items: up to 10 {"kind","path"}. Caption attached to first item."""
        media: list[dict] = []
        files: dict = {}
        for i, it in enumerate(items):
            ref = f"file{i}"
            entry: dict = {"type": it["kind"], "media": f"attach://{ref}"}
            if i == 0 and caption:
                entry["caption"] = caption[:1024]
                entry["parse_mode"] = "HTML"
            if it["kind"] == "video":
                entry["supports_streaming"] = True
            media.append(entry)
        # open late so dry-run never touches files unnecessarily (still fine either way)
        handles = [open(it["path"], "rb") for it in items]
        try:
            for i, h in enumerate(handles):
                files[f"file{i}"] = (items[i]["path"].name, h)
            data: dict = {"chat_id": chat_id, "media": json.dumps(media)}
            if message_thread_id is not None:
                data["message_thread_id"] = str(message_thread_id)
            self._post("sendMediaGroup", data=data, files=files)
        finally:
            for h in handles:
                try:
                    h.close()
                except Exception:
                    pass

    def send_document(self, chat_id: str, path: Path, caption: str,
                      message_thread_id: int | None = None) -> None:
        with open(path, "rb") as h:
            data: dict = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
            if message_thread_id is not None:
                data["message_thread_id"] = str(message_thread_id)
            self._post("sendDocument", data=data, files={"document": (path.name, h)})


def post_gallery(tg: Telegram, chat_id: str, label: str, date_s: str,
                 items: list[dict], notes: list[str],
                 message_thread_id: int | None = None) -> int:
    """Post items in chunks of 10. Returns number of messages (chunks) sent."""
    if not items:
        log.info("[%s %s] nothing gallery-sendable (%s)", label, date_s, "; ".join(notes) or "empty")
        return 0
    total_chunks = (len(items) + 9) // 10
    base = f"📸 <b>{label} — {date_s}</b>"
    if len(items) > 1:
        base += f"  ({len(items)} items)"
    if notes and len("\n".join(notes)) < 300:
        base += "\n<i>" + "; ".join(notes) + "</i>"
    for idx in range(0, len(items), 10):
        chunk = items[idx:idx + 10]
        n = idx // 10 + 1
        caption = base if total_chunks == 1 else f"{base}\n({n}/{total_chunks})"
        # caption only renders on first item; later chunks repeat for context
        log.info("Sending gallery chunk %d/%d (%d items) for %s %s", n, total_chunks, len(chunk), label, date_s)
        tg.send_media_group(chat_id, chunk, caption, message_thread_id)
        if n < total_chunks:
            time.sleep(2)
    return total_chunks


def _flat_arcname(f: Path) -> str:
    return f.name


def _day_arcname(f: Path) -> str:
    """Day-prefixed path so same filenames across days never collide in period zips."""
    return f"{f.parent.name}/{f.name}"


def make_zip_and_split(files: list[Path], label: str, stamp: str,
                       tmpdir: Path, split_mb: float,
                       arcname=_flat_arcname) -> list[Path]:
    safe_label = re.sub(r"[^\w\-]+", "_", label)
    zippath = tmpdir / f"{safe_label}-{stamp}.zip"
    with zipfile.ZipFile(zippath, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in files:
            zf.write(f, arcname=arcname(f))
    limit = int(split_mb * 1024 * 1024)
    size = zippath.stat().st_size
    if size <= limit:
        return [zippath]
    return split_file(zippath, split_mb)


_SPLIT_BUF = 8 * 1024 * 1024


def split_file(path: Path, split_mb: float) -> list[Path]:
    """Split file into <=split_mb chunks. Returns [path] untouched if small enough.

    Streams with a fixed 8MB buffer, so peak RAM stays flat instead of
    scaling with split_mb (previously one whole ~1900MB chunk per read).
    Output names and contents are unchanged.
    """
    limit = int(split_mb * 1024 * 1024)
    if path.stat().st_size <= limit:
        return [path]
    parts: list[Path] = []
    with open(path, "rb") as src:
        i = 1
        while True:
            part = path.parent / f"{path.stem}.part{i:03d}"
            with open(part, "wb") as dst:
                remaining = limit
                while remaining > 0:
                    buf = src.read(min(_SPLIT_BUF, remaining))
                    if not buf:
                        break
                    dst.write(buf)
                    remaining -= len(buf)
            if part.stat().st_size == 0:
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            i += 1
    path.unlink(missing_ok=True)
    return parts


def _looks_like_size_rejection(e: Exception) -> bool:
    """True if the failure smells like Telegram's upload size cap.

    The official Bot API caps bot uploads at ~50MB and, per tested reports,
    over-limit uploads die as a dropped TLS connection rather than a clean
    JSON error — so transport errors on a >50MB file count as size rejections.
    """
    msg = str(e).lower()
    if any(s in msg for s in ("too large", "too big", "file is too big",
                              "request entity too large", "413")):
        return True
    return isinstance(e, requests.RequestException)


def post_archive(tg: Telegram, chat_id: str, label: str, date_s: str, parts: list[Path],
                 message_thread_id: int | None = None,
                 fallback_split_mb: float | None = None) -> int:
    """Send archive parts. Returns number of documents sent.

    If a single part is rejected as too large (official Bot API caps bot
    uploads at ~50MB) and fallback_split_mb is set, re-split at that size
    and send the pieces instead.
    """
    def _send_all(ps: list[Path]) -> None:
        total = len(ps)
        for i, part in enumerate(ps, 1):
            mb = part.stat().st_size / 1024 / 1024
            caption = f"🗄️ <b>{label} — {date_s}</b>  part {i}/{total}  ({mb:.1f} MB)"
            log.info("Sending archive %s (%.1f MB)", part.name, mb)
            tg.send_document(chat_id, part, caption, message_thread_id)
            if i < total:
                time.sleep(2)

    try:
        _send_all(parts)
        return len(parts)
    except Exception as e:
        single_too_big = (
            fallback_split_mb
            and len(parts) == 1
            and parts[0].stat().st_size > 50 * 1024 * 1024
            and _looks_like_size_rejection(e)
        )
        if not single_too_big:
            raise
        log.warning("Archive rejected as too large (%s); re-splitting at %.0fMB",
                    e, fallback_split_mb)
        smaller = split_file(parts[0], fallback_split_mb)
        _send_all(smaller)
        return len(smaller)


def choose_archive_sender(zip_bytes: int, cfg: dict, args) -> str:
    """Return 'bot' or 'userbot' for an archive of zip_bytes.

    --archive-via userbot|bot forces; auto (default) uses the userbot when the
    zip exceeds USERBOT_THRESHOLD_MB (50MB ≈ official Bot API upload cap).
    """
    forced = (args.archive_via or cfg["archive_via"]).strip().lower()
    if forced not in ("auto", "bot", "userbot"):
        raise SystemExit(f"Bad archive sender '{forced}': pick auto|bot|userbot")
    if forced != "auto":
        return forced
    if zip_bytes > cfg["userbot_threshold_mb"] * 1024 * 1024:
        return "userbot"
    return "bot"


def send_archive_via_userbot(chat_id: str, label: str, stamp: str, part: Path,
                              message_thread_id: int | None,
                              part_info: tuple[int, int] | None = None) -> int:
    """Send a single zip part via the MTProto user account. Returns 1 document sent."""
    try:
        import userbot_send
    except ImportError as e:
        raise RuntimeError(f"userbot unavailable (telethon missing?): {e}")
    mb = part.stat().st_size / 1024 / 1024
    if part_info is not None:
        i, n = part_info
        caption = f"🗄️ <b>{label} — {stamp}</b>  part {i}/{n}  ({mb:.1f} MB, via userbot)"
    else:
        caption = f"🗄️ <b>{label} — {stamp}</b>  ({mb:.1f} MB, via userbot)"
    userbot_send.send_document(chat_id, part, caption, message_thread_id)
    return 1


# ---------- per user/date run ----------

def deliver_archive(tg: Telegram, cfg: dict, args, label: str, stamp: str,
                    files: list[Path], tmpdir: Path, arcname=_flat_arcname):
    """Shared archive pipeline for daily and period uploads.

    Returns (sent, via, total_mb, is_dry_run). In dry-run nothing is
    transmitted and sent is the estimated part count. Log lines match the
    historical format exactly.
    """
    archive_thread = args.archive_thread if args.archive_thread is not None else cfg["archive_thread_id"]
    split_mb = args.split_mb or cfg["split_mb"]
    total_bytes = 0
    for f in files:
        total_bytes += f.stat().st_size
    total_mb = total_bytes / 1024 / 1024
    if args.dry_run:
        nparts = max(1, int(-(-total_mb // split_mb))) if split_mb else 1
        via = choose_archive_sender(total_bytes, cfg, args)
        log.info("[dry-run] archive %s %s -> chat %s thread %s via %s: %d files, %.1f MB -> ~%d part(s) at %.0fMB",
                 label, stamp, cfg["archive_chat_id"], archive_thread, via,
                 len(files), total_mb, nparts, split_mb)
        return nparts, via, total_mb, True
    parts = make_zip_and_split(files, label, stamp, tmpdir, split_mb, arcname=arcname)
    zip_bytes = sum(p.stat().st_size for p in parts)
    via = choose_archive_sender(zip_bytes, cfg, args)
    if via == "bot":
        sent = post_archive(tg, cfg["archive_chat_id"], label, stamp, parts,
                            archive_thread, cfg["fallback_split_mb"])
        return sent, via, total_mb, False
    # Userbot path (forced or auto over threshold): one part at a time. Each
    # part is <= SPLIT_MB (within the ~2GB user upload cap), so multi-GB
    # periods that pre-split into many parts still deliver. Sends serialize
    # across parallel jobs via the file lock in userbot_send.
    sent = 0
    fell_back = False
    total_parts = len(parts)
    for i, part in enumerate(parts, 1):
        info = (i, total_parts) if total_parts > 1 else None
        try:
            sent += send_archive_via_userbot(
                cfg["archive_chat_id"], label, stamp, part, archive_thread, info)
        except Exception as e:
            log.warning("userbot send failed for %s (%s); falling back to Bot API split",
                        part.name, e)
            sent += post_archive(tg, cfg["archive_chat_id"], label, stamp, [part],
                                 archive_thread, cfg["fallback_split_mb"])
            fell_back = True
    if fell_back:
        via = "bot(fallback)" if total_parts == 1 else "userbot+bot(fallback)"
    return sent, via, total_mb, False


def process_one(tg: Telegram, cfg: dict, state: dict, user_id: str,
                date_s: str, args) -> str:
    label = user_label(cfg, user_id)
    key = f"{user_id}/{date_s}"
    entry = state.get(key, {})
    ddir = day_dir(cfg, user_id, date_s)
    files = scan_day(ddir)
    log.info("[%s %s] dir=%s files=%d", label, date_s, ddir, len(files))
    if not files:
        log.info("[%s %s] empty/missing, skipping", label, date_s)
        return "empty"

    do_gallery = not args.only_archive
    do_archive = not args.only_gallery
    if not args.force:
        if do_gallery and entry.get("gallery_done"):
            log.info("[%s %s] gallery already posted, skipping", label, date_s)
            do_gallery = False
        if do_archive and entry.get("archive_done"):
            log.info("[%s %s] archive already posted, skipping", label, date_s)
            do_archive = False
    if not do_gallery and not do_archive:
        return "skipped"

    with tempfile.TemporaryDirectory(prefix="immich-bot-") as tmp:
        tmpdir = Path(tmp)
        gallery_thread = args.gallery_thread if args.gallery_thread is not None else cfg["gallery_thread_id"]
        if do_gallery:
            items, notes = prepare_gallery_items(files, tmpdir,
                                                 convert_heic=not args.dry_run)
            if args.dry_run:
                log.info("[dry-run] gallery %s %s -> chat %s thread %s: %d sendable, %d notes: %s",
                         label, date_s, cfg["gallery_chat_id"], gallery_thread,
                         len(items), len(notes), notes or "-")
                for it in items:
                    log.info("[dry-run]   %s %s", it["kind"], it["name"])
            else:
                post_gallery(tg, cfg["gallery_chat_id"], label, date_s, items, notes, gallery_thread)
            entry["gallery_done"] = True
        if do_archive:
            sent, via, _total_mb, is_dry = deliver_archive(
                tg, cfg, args, label, date_s, files, tmpdir)
            if is_dry:
                entry["archive_done"] = True  # not persisted in dry-run (see below)
            else:
                entry["archive_done"] = True
                entry["archive_parts"] = sent
                entry["archive_via"] = via
        entry["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        if not args.dry_run:
            state[key] = entry
    return "posted"


def process_period(tg: Telegram, cfg: dict, state: dict, user_id: str,
                   period_s: str, args) -> str:
    """Archive-only upload of a whole month (YYYY-MM) or year (YYYY).

    Collects every file under <user>/<YYYY>/<YYYY-MM-DD>/ for the period,
    zips them as <label>-<period>.zip and splits at split_mb (default 1900MB
    ≈ 2GB max per file). Entries are stored as <YYYY-MM-DD>/<filename> so
    same filenames across days never collide. Delivery reuses the daily path: userbot single file
    over USERBOT_THRESHOLD_MB, else Bot API with FALLBACK_SPLIT_MB retry.

    State key is f"{user_id}/{period_s}" (e.g. "uuid/2026-09"), which cannot
    collide with daily keys ("uuid/2026-09-06") by length.
    """
    label = user_label(cfg, user_id)
    key = f"{user_id}/{period_s}"
    entry = state.get(key, {})
    files = collect_period_files(cfg, user_id, period_s)
    log.info("[%s %s] period files=%d", label, period_s, len(files))
    if not files:
        log.info("[%s %s] empty/missing, skipping", label, period_s)
        return "empty"

    if not args.force and entry.get("archive_done"):
        log.info("[%s %s] archive already posted, skipping", label, period_s)
        return "skipped"

    with tempfile.TemporaryDirectory(prefix="immich-bot-") as tmp:
        tmpdir = Path(tmp)
        sent, via, total_mb, is_dry = deliver_archive(
            tg, cfg, args, label, period_s, files, tmpdir, arcname=_day_arcname)
        if not is_dry:
            entry["archive_done"] = True
            entry["archive_parts"] = sent
            entry["archive_via"] = via
            entry["file_count"] = len(files)
            entry["total_mb"] = round(total_mb, 1)
        entry["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        if not args.dry_run:
            state[key] = entry
    return "posted"


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Immich daily Telegram bot (multi-user).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--today", action="store_true", help="post local today (nightly default)")
    g.add_argument("--date", help="manual push one day YYYY-MM-DD")
    g.add_argument("--range", help="backfill inclusive START:END")
    g.add_argument("--month", help="archive-only upload whole month YYYY-MM (single zip, split at SPLIT_MB)")
    g.add_argument("--year", help="archive-only upload whole year YYYY (single zip, split at SPLIT_MB)")
    p.add_argument("--user", action="append", default=[],
                   help="user alias or dir name; repeatable (default: all users)")
    p.add_argument("--all-users", action="store_true", help="explicitly select all users")
    p.add_argument("--list-users", action="store_true", help="print discovered users + aliases and exit")
    p.add_argument("--only-gallery", action="store_true")
    p.add_argument("--only-archive", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="no Telegram calls, no state writes")
    p.add_argument("--split-mb", type=float, default=None, help="zip split size MB (default from .env SPLIT_MB=1900)")
    p.add_argument("--gallery-thread", type=int, default=None, help="override .env GALLERY_THREAD_ID")
    p.add_argument("--archive-thread", type=int, default=None, help="override .env ARCHIVE_THREAD_ID")
    p.add_argument("--archive-via", choices=["auto", "bot", "userbot"], default=None,
                   help="archive sender: auto=userbot over 50MB (default), bot=Bot API only, userbot=always userbot")
    p.add_argument("--force", action="store_true", help="re-post even if state says done")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    try:
        logf = Path(LOG_FILE_DEFAULT)
        logf.parent.mkdir(parents=True, exist_ok=True)
        logging.getLogger().addHandler(logging.FileHandler(logf))
    except Exception:
        pass

    cfg = load_config()

    if args.list_users:
        users = discover_users(cfg["library_root"])
        print(f"LIBRARY base: {library_base(cfg)}")
        print(f"Users ({len(users)}):")
        for u in users:
            alias = next((a for a, uid in cfg["aliases"].items() if uid == u), "-")
            print(f"  {u}  alias={alias}")
        if not cfg["aliases"]:
            print("No aliases configured. Set USER_ALIASES in .env, e.g.:")
            print('  USER_ALIASES="tony:<uuid-dir>,ninnette:admin"')
        return 0

    if args.only_gallery and args.only_archive:
        print("Pick at most one of --only-gallery / --only-archive", file=sys.stderr)
        return 2

    try:
        period_s = validate_period(args)
        if period_s and args.only_gallery:
            raise SystemExit("--month/--year are archive-only; --only-gallery is not supported with them")
        users = resolve_user_selection(cfg, args.user, args.all_users)
        dates = [] if period_s else expand_dates(args, cfg)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2

    if not args.dry_run:
        if not cfg["bot_token"] or not cfg["archive_chat_id"]:
            print("Missing BOT_TOKEN / ARCHIVE_CHAT_ID in .env", file=sys.stderr)
            return 2
        if not period_s and not cfg["gallery_chat_id"]:
            print("Missing GALLERY_CHAT_ID in .env", file=sys.stderr)
            return 2

    tg = Telegram(cfg["bot_token"], dry_run=args.dry_run)
    state = load_state(cfg["state_file"])
    gallery_thread = args.gallery_thread if args.gallery_thread is not None else cfg["gallery_thread_id"]
    archive_thread = args.archive_thread if args.archive_thread is not None else cfg["archive_thread_id"]
    if period_s:
        log.info("Users=%s period=%s archive-only dry_run=%s",
                 users, period_s, args.dry_run)
    else:
        log.info("Users=%s dates=%s gallery=%s archive=%s dry_run=%s",
                 users, dates,
                 "no" if args.only_archive else "yes",
                 "no" if args.only_gallery else "yes",
                 args.dry_run)
    log.info("Gallery -> chat %s thread %s | Archive -> chat %s thread %s via %s",
             cfg["gallery_chat_id"], gallery_thread,
             cfg["archive_chat_id"], archive_thread,
             args.archive_via or cfg["archive_via"])

    summary: dict[str, int] = {}
    failures: list[str] = []
    # Single job list preserves the historical order (dates outer, users inner).
    jobs = ([(uid, period_s, True) for uid in users] if period_s
            else [(uid, date_s, False) for date_s in dates for uid in users])
    for uid, stamp, is_period in jobs:
        label = user_label(cfg, uid)
        status = None
        try:
            status = (process_period(tg, cfg, state, uid, stamp, args) if is_period
                      else process_one(tg, cfg, state, uid, stamp, args))
            summary[status] = summary.get(status, 0) + 1
        except Exception as e:
            log.exception("[%s %s] FAILED: %s", label, stamp, e)
            failures.append(f"{label}/{stamp}: {e}")
        if not args.dry_run and status == "posted":
            # State only mutates on posted (empty/skipped/failed write nothing).
            try:
                save_state(cfg["state_file"], state)
            except Exception as e:
                log.warning("state save failed: %s", e)
        time.sleep(1)  # gentle pacing across users/dates

    log.info("Done: %s failures=%d", summary, len(failures))
    for f in failures:
        log.error("FAILED: %s", f)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
