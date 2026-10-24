# DingDingDong — Discord Music Bot

A self-hosted Discord music bot packing the music feature set of **LunaBot 🌙** and **FlaviBot**
(see `FEATURES.md` for the full research + feature matrix). Built on `discord.py 2.7`,
plays audio from YouTube via `yt-dlp` + bundled `ffmpeg`.

**Everything works as both a slash command (`/play`) and a prefix command (`!play`).**

---

## Quick start

```
run.bat                # start the bot (Windows, double-click or from a terminal)
update-ytdlp.bat       # update yt-dlp when a stream suddenly fails (most common fix)
```

> Run **only one instance per token** at a time — a second instance kicks the first
> offline (each new gateway login invalidates the old session).

## What's inside

- `bot/` — source (`main.py` entry, `player.py` playback engine, `ytdl.py` resolver, `cogs/`)
- `bin/` — bundled `ffmpeg.exe` + `ffprobe.exe` (libopus-enabled build; nothing to install)
- `data/` — settings / playlists / history JSON + download cache (auto-created)
- `.env` — **your credentials live here** (token, client id/secret). Never commit/ship this file.
- `tools/` — diagnostic scripts (see below)

## Requirements

Already set up in this folder:

- Python 3.12 venv at `.venv` (discord.py 2.7.1, yt-dlp, PyNaCl, **davey**, python-dotenv, aiohttp)
- ffmpeg + ffprobe in `bin/`

Nothing else to install on this machine. To move the bot elsewhere: copy the folder
(except `.venv` and `data/`), then `pip install -r requirements.txt` (use
`python -m pip install "discord.py[voice]"` so **davey** is included — voice refuses
to start without it), and keep `bin/ffmpeg.exe` or install ffmpeg system-wide.

## Configuration (`.env`)

| Key | Meaning |
|---|---|
| `DISCORD_TOKEN` | bot token (required) |
| `CLIENT_ID` / `CLIENT_SECRET` | application id/secret (used for the invite link) |
| `BOT_NAME` | shown in embeds |
| `DEFAULT_PREFIX` | default prefix when a server hasn't set one (`!`) |
| `MAX_QUEUE` | queue cap (default 500) |
| `IDLE_LEAVE_SECONDS` | leave after this much idle time (default 600) |
| `EMPTY_CHANNEL_SECONDS` | leave after the channel sits empty this long (default 180) |
| `FFMPEG_PATH` | override ffmpeg binary (optional) |

## Commands

**Playback** — `play` `playskip` `playtop` `playfile` `search` `pause` `resume` `replay`
`back` `addprevious` `skip` `skipto` `stop` `clear` `disconnect` `join` `moveme`

**Queue & tuning** — `queue` `nowplaying` `history` `shuffle` `move` `remove` `volume`
`loop` `seek` `forward` `rewind` `filter`

**Extras** — `autoplay` `24-7` `lyrics` `radios` `clean` `ping`

**Playlists** — `playlist create/add/addtrack/addqueue/show/play/delete/share/lock/list/public`

**Server settings** — `prefix` `reset-prefix` `setdj` `controller` `songstatus` `announce`
`autorejoin` `settings` `invite` `updateytdlp` (owner)

Prefix short forms (like Luna/Flavi): `!p` `!ps` `!pt` `!q` `!np` `!s` `!r` `!vol` `!l` `!sh`
`!rm` `!mv` `!ff` `!rw` `!dc` `!j` `!cq` `!247` … Full list: `/help`.

### Handy behaviours

- `/play <query> insert_first:true` — jump the line (also `playskip`).
- 3+ listeners → `/skip` starts a **vote** (Flavi style: 3–4 need 2 votes, 5+ need 60%).
  DJ role holders and people alone with the bot skip instantly.
- `/24-7` keeps the bot in the channel; with `/autorejoin` it re-joins after a kick.
- `/controller create` makes a request channel: type a song name there and it plays.
- `/seek 1:30`, `/seek +30`, `/seek -30` all work. `/filter nightcore|vaporwave|bassboost|…`
- Autoplay queues related tracks from the track's YouTube mix when the queue ends.
- Voice-channel status shows the current song (toggle: `/songstatus`).

## Reliability notes (why this one actually plays)

- **davey** (Discord's E2EE voice requirement) is installed — without it `connect()` raises
  and *no* music plays. This is the #1 reason new bots "never run".
- Stream URLs are extracted with a **player-client fallback list** and each candidate is
  **HTTP-verified** before playback starts (kills the classic "starts, then dies" 403).
- If a live stream still dies mid-track, playback **auto-retries via download** once.
- ffmpeg is bundled with libopus; nothing depends on system PATH.
- `-reconnect` flags keep network streams alive through hiccups.
- If YouTube breaks extraction again: run `update-ytdlp.bat` (or `/updateytdlp`), then restart.

## Diagnostics (tools/)

```
.venv\Scripts\python.exe tools\smoke_test.py     # offline: cogs/commands/aliases check
.venv\Scripts\python.exe tools\test_pipeline.py  # yt-dlp -> ffmpeg streaming + download check
.venv\Scripts\python.exe tools\test_voice.py     # live: joins an EMPTY vc and plays 20s
```

## Going public later

- `/invite` prints a ready invite link (`bot` + `applications.commands` scopes).
- For 100+ servers Discord requires **app verification**; the message-content intent is
  currently in "limited" mode (fine under 100 servers).
- Host it on a small VPS or a home machine that stays on. Slash commands sync
  automatically per guild (≤3 guilds) or globally (more) — no action needed.
- Spotify/Apple/Deezer links aren't supported directly (they need platform API keys) —
  search by song name instead. Radio stations and direct audio/stream URLs work.

## Deploying to a hosting panel (bot-hosting.net)

The panel pulls straight from GitHub → **https://github.com/PlayersOfTheShadows/DingDingDong** (public, `main`).

Panel settings:

- **Runtime:** `Python` — version **3.12** (the tested version; 3.11/3.13/3.14 also fine).
- **Main file / start:** `main.py` (the root shim) — or in a console: `python -m bot.main`.
- **Install:** `pip install -r requirements.txt` — includes `discord.py[voice]` → **davey**, which voice refuses to start without.
- **Environment:** upload your local `.env` (file manager / SFTP) or set the same values as environment variables in the panel. `DISCORD_TOKEN` is the only strictly required one.
- **ffmpeg:** the repo ships no binaries; the bot uses system `ffmpeg` when `bin/` is absent, so a host with ffmpeg preinstalled just works. If voice connects but is silent, run `ffmpeg -version` in the panel console; if missing, upload a static Linux ffmpeg to `bin/ffmpeg` — it is picked up automatically.
- Only one instance per token — stop the local copy before the hosted one comes online.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot joins but no sound | Check **Connect** + **Speak** perms in that channel (and server voice region) |
| "Couldn't play …" / 403 storms | `update-ytdlp.bat`, restart bot |
| Slash commands missing | Wait a minute, or re-invite with `applications.commands` scope (`/invite`) |
| Bot went offline after you started another copy | Only one instance per token — close the other |
| Voice connect fails instantly | davey missing → `python -m pip install "discord.py[voice]"` |
