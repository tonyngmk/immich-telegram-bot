# Immich Telegram Bot

Posts Immich day folders to Telegram:
1. **Gallery thread** — scrollable album (`sendMediaGroup`, 10/chunk), caption `📸 <user> — YYYY-MM-DD`. HEIC→JPEG converted, MOV sent as video.
2. **Archive thread** — zipped originals (`sendDocument`) as a single file unless
   over ~2GB (`SPLIT_MB=1900`), then split into parts.

Both can be two topics in one forum supergroup (via `*_THREAD_ID`) or two
separate channels — just set the IDs in `.env`.

## Setup

```bash
cd ~/immich-telegram-bot
cp .env.example .env   # fill BOT_TOKEN, channel IDs, USER_ALIASES
chmod 600 .env
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py --list-users   # confirm tony / ninnette mapping
```

Bot must be **admin** in the channel. If you use topic threads, the group must be a
forum supergroup with **Topics enabled** (thread IDs only work there, not in
broadcast channels).

## Usage

```bash
# nightly (launchd calls this at 23:55)
.venv/bin/python bot.py --today --all-users

# manual push one day (all users)
.venv/bin/python bot.py --date 2026-09-06

# selected users only
.venv/bin/python bot.py --date 2026-09-06 --user tony
.venv/bin/python bot.py --date 2026-09-06 --user tony --user ninnette

# backfill range inclusive
.venv/bin/python bot.py --range 2026-09-01:2026-09-05
.venv/bin/python bot.py --range 2026-09-01:2026-09-05 --user ninnette --only-archive

# monthly / yearly archive (single zip per user, split at SPLIT_MB ~2GB)
.venv/bin/python bot.py --month 2026-09 --dry-run -v   # preview first
.venv/bin/python bot.py --month 2026-09 --user tony
.venv/bin/python bot.py --year 2026 --all-users

# preview without sending / writing state
.venv/bin/python bot.py --date 2026-09-06 --dry-run -v

# force archives through one sender
.venv/bin/python bot.py --date 2026-09-06 --archive-via userbot
.venv/bin/python bot.py --date 2026-09-06 --archive-via bot

# re-post even if state says done
.venv/bin/python bot.py --date 2026-09-06 --user tony --force
```

## Perf harness (local-only, no Telegram sends)

```bash
.venv/bin/python bench.py --suite all --fast            # quick baseline (~25MB fixtures)
.venv/bin/python bench.py --suite month --seed 7        # full-scale month (200MB fixture)
.venv/bin/python bench.py --suite month --fast --compare # diff vs last matching baseline
```

`bench.py` is stdlib-only: deterministic fake libraries, mocked sends (pacing
sleeps patched out), per-stage wall time + MB/s + peak RSS + temp-disk peak.
Rows append to `logs/bench.jsonl` (local only). Workflow: baseline → optimize
one stage → re-run same flags → `--compare`. The `month` suite already reports
`collisions=90` — flat zip names repeat across days (see Notes follow-up).

## Nightly schedule (macOS launchd, 23:55)

```bash
cp com.tonyngmk.immich-telegrambot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tonyngmk.immich-telegrambot.plist
launchctl list | grep immich
tail -f logs/launchd.out.log logs/bot.log
# unload: launchctl unload ~/Library/LaunchAgents/com.tonyngmk.immich-telegrambot.plist
```

## Notes

- `LIBRARY_ROOT` should be the parent `.../library` (auto-discovers users). Legacy single-user path also works.
- `USER_ALIASES` maps `tony`/`ninnette` → dir names. `--user` accepts alias or raw dir name (case-insensitive).
- State in `state.json` keyed `user/date` — re-runs skip posted dates unless `--force`.
  Monthly/yearly archives use keys `user/YYYY-MM` / `user/YYYY`, so they never
  collide with daily entries.
- `--month` / `--year` are archive-only (no gallery posts) and send one
  `<user>-<period>.zip` per user to the same archive thread, split at
  `SPLIT_MB` (1900MB ≈ 2GB max per file). A large month/year becomes
  sequential `partNNN` uploads; ensure ~2× the period size in temp disk space.
- HEIC converts to JPEG (longest side ≤2560, q90). Originals always go in the zip unmodified.
- Videos transcode to H.264/AAC MP4 with faststart for the gallery wall
  (iPhone HEVC `.mov` previews poorly on Telegram — black tiles / won't play
  inline). Needs `ffmpeg` (`brew install ffmpeg`); without it videos go up
  as-is. Originals always go in the zip unmodified.
- Videos >45MB skip the gallery wall but stay in the zip (Bot API media-group limit).
- Telegram's **official Bot API caps bot uploads at ~50MB**, so a day-zip between
  50MB and 2GB is automatically re-split into `FALLBACK_SPLIT_MB` (45MB) parts on
  delivery failure. To send single files up to 2GB, self-host a local Bot API
  server (`tdlib/telegram-bot-api`) and point the bot at it.

## Userbot alternative (tested, works)

`userbot_test.py` drives your **user account** via MTProto (Telethon) instead of the
Bot API, so archives upload whole up to 2GB (4GB with Premium) — verified with a
57.5MB and a 587MB single-file delivery to the archive thread. Needs `API_ID`/
`API_HASH`/`PHONE_NUMBER` in `.env` (from https://my.telegram.org/apps); session persists in
`userbot.session` (`chmod 600`, never commit). Commands: `--request-code`,
`--login CODE`, `--whoami`, `--topics`, `--ping`, `--send FILE --caption ...`.
Posts appear under your name, not the bot's.

`bot.py` uses this automatically: `ARCHIVE_VIA=auto` (default) sends archives via
the Bot API up to `USERBOT_THRESHOLD_MB` (50MB) and via your userbot account above
that — gallery always stays on the bot. `--archive-via bot|userbot` forces one path.
