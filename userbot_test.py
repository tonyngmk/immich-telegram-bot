#!/usr/bin/env python3
"""Userbot (MTProto) test sender — bypasses the 50MB Bot API upload cap.

User accounts upload up to 2GB natively (4GB with Premium).

One-time login (needs API_ID/API_HASH from https://my.telegram.org/apps):
  .venv/bin/python userbot_test.py --request-code     # Telegram sends login code to your app
  .venv/bin/python userbot_test.py --login 12345      # add --password XXX if 2FA enabled
  .venv/bin/python userbot_test.py --whoami           # verify session

Tests (session persists in userbot.session, chmod 600):
  .venv/bin/python userbot_test.py --topics                  # list forum topics + IDs
  .venv/bin/python userbot_test.py --ping                    # tiny text to archive thread
  .venv/bin/python userbot_test.py --send /path/to.zip --caption "test"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SESSION_PATH = BASE_DIR / "userbot"
CODE_HASH_PATH = BASE_DIR / ".userbot_code.json"


def load_env() -> dict:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(BASE_DIR / ".env")
    except Exception:
        p = BASE_DIR / ".env"
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    for var in ("API_ID", "API_HASH", "PHONE_NUMBER"):
        if not os.environ.get(var):
            raise SystemExit(f"Missing {var} in .env (get API_ID/API_HASH at https://my.telegram.org/apps)")
    return {
        "api_id": int(os.environ["API_ID"]),
        "api_hash": os.environ["API_HASH"],
        "phone": os.environ["PHONE_NUMBER"],
        "chat_id": int(os.environ.get("ARCHIVE_CHAT_ID", "-1002292128611")),
        "thread_id": int(os.environ.get("ARCHIVE_THREAD_ID", "3")),
    }


def get_client(cfg: dict):
    from telethon import TelegramClient
    return TelegramClient(str(SESSION_PATH), cfg["api_id"], cfg["api_hash"])


@asynccontextmanager
async def _connected(cfg: dict):
    """Connect without Telethon's interactive start() prompts (no stdin here)."""
    client = get_client(cfg)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def cmd_request_code(cfg: dict) -> None:
    import json
    async with _connected(cfg) as client:
        if await client.is_user_authorized():
            print("Already logged in. Run --whoami to verify.")
            return
        sent = await client.send_code_request(cfg["phone"])
        CODE_HASH_PATH.write_text(json.dumps(
            {"phone": cfg["phone"], "phone_code_hash": sent.phone_code_hash}))
        CODE_HASH_PATH.chmod(0o600)
        print(f"Login code sent to {cfg['phone']} (check Telegram app). "
              f"Then run: --login <code>")


async def cmd_login(cfg: dict, code: str, password: str | None) -> None:
    import json
    from telethon.errors import SessionPasswordNeededError
    if not CODE_HASH_PATH.exists():
        raise SystemExit("No pending code request. Run --request-code first.")
    saved = json.loads(CODE_HASH_PATH.read_text())
    async with _connected(cfg) as client:
        if await client.is_user_authorized():
            print("Already logged in.")
            return
        try:
            await client.sign_in(phone=saved["phone"], code=code,
                                phone_code_hash=saved["phone_code_hash"])
        except SessionPasswordNeededError:
            if not password:
                raise SystemExit("2FA enabled: re-run with --password <your-2fa-password>")
            await client.sign_in(password=password)
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (@{me.username}, id={me.id})")
    SESSION_PATH.with_suffix(".session").chmod(0o600)
    CODE_HASH_PATH.unlink(missing_ok=True)


async def cmd_whoami(cfg: dict) -> None:
    async with _connected(cfg) as client:
        if not await client.is_user_authorized():
            raise SystemExit("Not logged in. Run --request-code first.")
        me = await client.get_me()
        print(f"Session OK: {me.first_name} (@{me.username}, id={me.id}, "
              f"premium={getattr(me, 'premium', False)})")


async def cmd_topics(cfg: dict) -> None:
    from telethon.tl import functions
    async with _connected(cfg) as client:
        res = await client(functions.messages.GetForumTopicsRequest(
            peer=cfg["chat_id"], offset_date=None, offset_id=0,
            offset_topic=0, limit=100))
        print(f"Forum topics in {cfg['chat_id']}:")
        for t in res.topics:
            print(f"  id={t.id} title={t.title!r}")


async def cmd_ping(cfg: dict) -> None:
    async with _connected(cfg) as client:
        msg = await client.send_message(
            cfg["chat_id"], "🔧 userbot test — please ignore/delete",
            reply_to=cfg["thread_id"])
        print(f"Ping sent to thread {cfg['thread_id']}: message id={msg.id}")


async def cmd_send(cfg: dict, path: str, caption: str, thread: int | None) -> None:
    f = Path(path)
    if not f.is_file():
        raise SystemExit(f"File not found: {path}")
    size_mb = f.stat().st_size / 1024 / 1024
    thread = cfg["thread_id"] if thread is None else thread
    last_pct = 0
    last_t = time.time()

    def progress(sent: int, total: int) -> None:
        nonlocal last_pct, last_t
        pct = int(sent * 100 / total)
        now = time.time()
        if pct >= last_pct + 10 or (pct == 100 and now - last_t > 1):
            print(f"  upload {pct}% ({sent/1024/1024:.1f}/{total/1024/1024:.1f} MB)", flush=True)
            last_pct, last_t = pct, now

    print(f"Sending {f.name} ({size_mb:.1f} MB) to chat {cfg['chat_id']} thread {thread} ...")
    async with _connected(cfg) as client:
        msg = await client.send_file(
            cfg["chat_id"], str(f), caption=caption[:1024],
            reply_to=thread, force_document=True,
            parse_mode="html",  # captions use <b>/<i>; Telethon defaults to Markdown
            progress_callback=progress)
        print(f"Sent: message id={msg.id}")


def main() -> int:
    p = argparse.ArgumentParser(description="Userbot MTProto test sender.")
    p.add_argument("--request-code", action="store_true")
    p.add_argument("--login", metavar="CODE")
    p.add_argument("--password", default=None, help="2FA password if enabled")
    p.add_argument("--whoami", action="store_true")
    p.add_argument("--topics", action="store_true")
    p.add_argument("--ping", action="store_true")
    p.add_argument("--send", metavar="FILE")
    p.add_argument("--caption", default="userbot test upload")
    p.add_argument("--thread", type=int, default=None)
    args = p.parse_args()

    cfg = load_env()
    if args.request_code:
        asyncio.run(cmd_request_code(cfg))
    elif args.login:
        asyncio.run(cmd_login(cfg, args.login, args.password))
    elif args.whoami:
        asyncio.run(cmd_whoami(cfg))
    elif args.topics:
        asyncio.run(cmd_topics(cfg))
    elif args.ping:
        asyncio.run(cmd_ping(cfg))
    elif args.send:
        asyncio.run(cmd_send(cfg, args.send, args.caption, args.thread))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
