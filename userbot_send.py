#!/usr/bin/env python3
"""MTProto userbot sender, importable by bot.py for >50MB archives.

Uses the same session file as userbot_test.py (log in once via that script).
Posts appear under your user account, which uploads up to 2GB natively
(4GB with Premium) — bypassing the ~50MB Bot API cap.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("immich-bot")

BASE_DIR = Path(__file__).resolve().parent
SESSION_PATH = BASE_DIR / "userbot"

# Official Bot API rejects bot uploads over ~50MB (measured cap is the
# 52,428,800-byte request body). Anything above this goes via userbot in auto.
BOT_API_UPLOAD_CAP = 50 * 1024 * 1024


class UserbotError(RuntimeError):
    pass


def load_dotenv(env_path: Path | None = None) -> None:
    """Load .env without overriding existing environment (shared with userbot_test)."""
    p = env_path or BASE_DIR / ".env"
    try:
        from dotenv import load_dotenv as _load
        _load(p)
        return
    except Exception:
        pass
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _creds() -> tuple[int, str]:
    api_id = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id or not api_hash:
        raise UserbotError("API_ID/API_HASH missing from .env — run userbot_test.py login first")
    return int(api_id), api_hash


async def send_document_async(chat_id: str | int, path: Path, caption: str,
                              message_thread_id: int | None = None) -> int:
    """Send one file via the user account. Returns the message id."""
    from telethon import TelegramClient
    from contextlib import asynccontextmanager

    api_id, api_hash = _creds()

    @asynccontextmanager
    async def _connected():
        client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise UserbotError("userbot session not logged in — run userbot_test.py --login")
            yield client
        finally:
            await client.disconnect()

    size_mb = path.stat().st_size / 1024 / 1024
    last_pct = 0

    def progress(sent: int, total: int) -> None:
        nonlocal last_pct
        pct = int(sent * 100 / total)
        if pct >= last_pct + 10 or pct == 100:
            log.info("userbot upload %d%% (%.1f/%.1f MB)", pct, sent / 1024 / 1024, total / 1024 / 1024)
            last_pct = pct

    log.info("Sending archive via userbot %s (%.1f MB)", path.name, size_mb)
    t0 = time.time()
    async with _connected() as client:
        msg = await client.send_file(
            int(chat_id), str(path), caption=caption[:1024],
            reply_to=message_thread_id, force_document=True,
            parse_mode="html",  # captions use <b>/<i>; Telethon defaults to Markdown
            progress_callback=progress)
    log.info("userbot sent in %.0fs: message id=%s", time.time() - t0, msg.id)
    return msg.id


def send_document(chat_id: str | int, path: Path, caption: str,
                  message_thread_id: int | None = None) -> int:
    """Sync wrapper for bot.py (runs its own event loop)."""
    return asyncio.run(send_document_async(chat_id, path, caption, message_thread_id))
