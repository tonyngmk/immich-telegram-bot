#!/usr/bin/env python3
"""Delete partial gallery chunks for days that failed mid-post.

When a media-group chunk fails partway through a day (e.g. the old
consumed-handle retry bug), earlier chunks stay posted with no state record.
This finds them by caption (📸 label — date) and deletes them so the day can
be re-posted cleanly.

Usage:
  .venv/bin/python delete_partials.py --dry-run
  .venv/bin/python delete_partials.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from swap_captions import FIRST_LINE_RE, _connect  # noqa: E402
from userbot_send import SESSION_PATH, _creds, load_dotenv  # noqa: E402

log = logging.getLogger("delete-partials")

# Gallery dates with partial chunks on the channel. EITHER label matches:
# the first gallery job posted these dates under the old (inverted) mapping
# and the re-push posted partials under the corrected one — both copies go,
# then the days are re-posted fresh exactly once.
FAILED_DATES = {
    "2026-08-24",
    "2026-08-25",
    "2026-08-27",
    "2026-08-29",
    "2026-08-30",
    "2026-08-31",
}


def parse_gallery(text: str):
    """Return (label, stamp) for 📸 gallery captions, else None."""
    lines = text.split("\n")
    m = FIRST_LINE_RE.match(lines[0])
    if not m or m.group(1) != "📸":
        return None
    _emoji, _open, label, stamp, _close, _suffix = m.groups()
    if len(stamp) != 10:  # daily gallery stamps only (never touch 🗄️ archives)
        return None
    if stamp not in FAILED_DATES:
        return None
    return label, stamp


async def collect(chat: int, limit: int):
    api_id, api_hash = _creds()
    hits = []
    client = await _connect(api_id, api_hash)
    async with client:
        async for msg in client.iter_messages(chat, limit=limit):
            if not getattr(msg, "text", None):
                continue
            parsed = parse_gallery(msg.text)
            if parsed is not None:
                hits.append((msg.id, str(msg.date), msg.text.split("\n")[0][:80]))
    return hits


async def apply(chat: int, ids: list[int], pause: float) -> tuple[int, int]:
    from telethon.errors import FloodWaitError

    api_id, api_hash = _creds()
    ok, failed = 0, 0
    client = await _connect(api_id, api_hash)
    async with client:
        # Delete in small batches; ids share an album only within a day-chunk set.
        for i in range(0, len(ids), 10):
            batch = ids[i:i + 10]
            try:
                await client.delete_messages(chat, batch)
                ok += len(batch)
            except FloodWaitError as e:
                log.warning("flood wait %ss", e.seconds)
                await asyncio.sleep(e.seconds + 1)
                try:
                    await client.delete_messages(chat, batch)
                    ok += len(batch)
                except Exception as e2:
                    log.error("batch %s failed: %s", batch, e2)
                    failed += len(batch)
            except Exception as e:
                log.error("batch %s failed: %s", batch, e)
                failed += len(batch)
            await asyncio.sleep(pause)
    return ok, failed


def main() -> int:
    p = argparse.ArgumentParser(description="Delete partial gallery chunks for failed days.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=8000)
    p.add_argument("--pause", type=float, default=0.5)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    load_dotenv(BASE_DIR / ".env")
    chat = int(os.environ["ARCHIVE_CHAT_ID"])
    hits = asyncio.run(collect(chat, args.limit))
    print(f"Matched {len(hits)} partial-chunk messages")
    for msg_id, date, first in hits[:40]:
        print(f"  {msg_id} {date} :: {first}")
    if len(hits) > 40:
        print(f"  ... and {len(hits) - 40} more")
    if args.dry_run:
        # Self-test the parser (accepts clean and **-marked forms).
        assert parse_gallery("📸 tony — 2026-08-29  (50 items)") == ("tony", "2026-08-29")
        assert parse_gallery("📸 **tony — 2026-08-29**  (50 items)\n(3/5)") == ("tony", "2026-08-29")
        assert parse_gallery("📸 ninnette — 2026-08-26  (10 items)") is None  # not a failed date
        assert parse_gallery("🗄️ tony — 2026-08  part 1/2  (1.0 MB)") is None
        print("parser self-test OK")
        return 0
    ok, failed = asyncio.run(apply(chat, [h[0] for h in hits], args.pause))
    print(f"Deleted {ok}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
