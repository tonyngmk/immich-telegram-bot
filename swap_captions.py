#!/usr/bin/env python3
"""Bulk-fix swapped tony/ninnette captions on posted channel messages.

Background: USER_ALIASES was inverted (tony pointed at Ninnette's library
dir and vice versa), so every posted caption names the wrong person. This
finds all bot-format captions, swaps the label, and rewrites the caption as
canonical HTML (also repairing any literal `**` markers / skewed entities).

Stored caption forms handled (plain text + entities are ignored and rebuilt):
  📸 tony — 2026-09-05  (79 items)         (clean)
  📸 **tony — 2026-09-05**  (79 items)     (literal markers)
  🗄️ ninnette — 2025  part 3/24  (1900.0 MB[, via userbot])

Usage:
  .venv/bin/python swap_captions.py --dry-run [--limit 5000]
  .venv/bin/python swap_captions.py --apply [--limit 5000]

Edited message IDs are recorded in logs/swap-done.json and skipped on
re-runs (running twice would swap the names back).
Userbot (own) messages edit via MTProto; the bot's gallery messages fall
back to Bot API editMessageCaption (BOT_TOKEN from .env).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from userbot_send import SESSION_PATH, _creds, load_dotenv  # noqa: E402

log = logging.getLogger("swap-captions")
DONE_FILE = BASE_DIR / "logs" / "swap-done.json"

FIRST_LINE_RE = re.compile(
    r"^(📸|🗄️)\s+(\*\*|<b>)?(tony|ninnette) — (\d{4}(?:-\d{2}(?:-\d{2})?)?)(\*\*|</b>)?(.*)$"
)
SWAP = {"tony": "ninnette", "ninnette": "tony"}


def canonical_html(text: str) -> str | None:
    """Rebuild canonical caption HTML with swapped label, or None if no match."""
    lines = text.split("\n")
    m = FIRST_LINE_RE.match(lines[0])
    if not m:
        return None
    emoji, _open, label, stamp, _close, inline_suffix = m.groups()
    rest = "\n".join(lines[1:])
    out = f"{emoji} <b>{SWAP[label]} — {stamp}</b>{inline_suffix}"
    if rest:
        out += "\n" + rest
    return out


def load_done() -> set:
    try:
        return set(json.loads(DONE_FILE.read_text()))
    except Exception:
        return set()


def save_done(ids: set) -> None:
    DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DONE_FILE.write_text(json.dumps(sorted(ids)))


async def collect(chat: int, limit: int, skip: set):
    from telethon import TelegramClient

    api_id, api_hash = _creds()
    matches = []
    async with TelegramClient(str(SESSION_PATH), api_id, api_hash) as client:
        if not await client.is_user_authorized():
            raise SystemExit("userbot session not logged in")
        async for msg in client.iter_messages(chat, limit=limit):
            if not getattr(msg, "text", None) or msg.id in skip:
                continue
            new = canonical_html(msg.text)
            if new is not None:
                matches.append((msg.id, str(msg.date), msg.text.split("\n")[0][:90], new))
    return matches


def edit_via_bot_api(chat: str, msg_id: int, caption_html: str) -> None:
    import requests
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN missing, cannot fall back to Bot API edit")
    for caption in (caption_html, re.sub(r"<[^>]+>", "", caption_html)):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/editMessageCaption",
            data={"chat_id": chat, "message_id": msg_id,
                  "caption": caption[:1024], "parse_mode": "HTML"},
            timeout=60,
        )
        body = r.json()
        if body.get("ok"):
            if caption != caption_html:
                log.warning("msg %d edited as plain text (HTML rejected)", msg_id)
            return
    raise RuntimeError(f"editMessageCaption failed: {body}")


async def apply(chat: int, matches, chat_s: str, pause: float) -> tuple[int, int, set]:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError

    api_id, api_hash = _creds()
    ok, failed, done_ids = 0, 0, set()
    async with TelegramClient(str(SESSION_PATH), api_id, api_hash) as client:
        for msg_id, _date, _old, new in matches:
            success = False
            for attempt in (1, 2):
                try:
                    await client.edit_message(chat, msg_id, new, parse_mode="html")
                    success = True
                    break
                except FloodWaitError as e:
                    log.warning("flood wait %ss (msg %d)", e.seconds, msg_id)
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    # Likely the bot's message (not ours via MTProto):
                    # fall back to the Bot API, which edits the bot's own posts.
                    try:
                        edit_via_bot_api(chat_s, msg_id, new)
                        success = True
                        break
                    except Exception as e2:
                        if attempt == 2:
                            log.error("msg %d FAILED: mtproto=%s botapi=%s", msg_id, e, e2)
                        else:
                            await asyncio.sleep(2)
            if success:
                ok += 1
                done_ids.add(msg_id)
            else:
                failed += 1
            time.sleep(pause)
    return ok, failed, done_ids


def main() -> int:
    p = argparse.ArgumentParser(description="Swap tony/ninnette caption labels on posted messages.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="list matching messages, change nothing")
    g.add_argument("--apply", action="store_true", help="edit the captions")
    p.add_argument("--limit", type=int, default=8000, help="channel history depth to scan")
    p.add_argument("--pause", type=float, default=0.4, help="seconds between edits")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    load_dotenv(BASE_DIR / ".env")
    chat_s = os.environ.get("ARCHIVE_CHAT_ID", "").strip()
    if not chat_s:
        raise SystemExit("ARCHIVE_CHAT_ID missing from .env")

    done = load_done()
    matches = asyncio.run(collect(int(chat_s), args.limit, done))
    print(f"Matched {len(matches)} captioned messages (limit={args.limit}, skipping {len(done)} done)")
    for msg_id, date, old, _new in matches[:30]:
        print(f"  {msg_id} {date} :: {old}")
    if len(matches) > 30:
        print(f"  ... and {len(matches) - 30} more")
    if args.dry_run:
        # Self-test the normalizer on both stored forms.
        assert canonical_html("📸 tony — 2026-09-05  (79 items)").startswith("📸 <b>ninnette — 2026-09-05</b>")
        assert canonical_html("🗄️ **ninnette — 2026-04**  (936.7 MB, via userbot)").startswith("🗄️ <b>tony — 2026-04</b>")
        assert canonical_html("hello world") is None
        print("normalizer self-test OK")
        return 0
    ok, failed, done_ids = asyncio.run(apply(int(chat_s), matches, chat_s, args.pause))
    save_done(done | done_ids)
    print(f"Edited {ok}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
