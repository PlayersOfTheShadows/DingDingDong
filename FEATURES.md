# LunaBot 🌙 + FlaviBot — research & feature matrix

Research date: 2026-10-23 (v1), updated 2026-10-25 (v2 — AI, autocomplete, filters, jump, 2 h idle).
Sources: lunabot.vc (commands/premium pages), discordbotlist.com, bestdiscord.org command index,
discord.bots.gg, luna.nx.kg, flavibot.xyz/docs (music module), flavibot.xyz/features/music.

## The two reference bots

### LunaBot 🌙 (lunabot.vc) — discord.js, ~1.7M servers
Music bot with a very wide command surface + a profile/economy layer.

- **Play:** play `p`, playskip `ps`, playtop `pt`, playfile, search `yts`, artist
- **Transport:** pause `pu`, resume `r`, replay `rp`, back `b`, addprevious, skip `s`,
  skipto / jump, queue `q`, nowplaying `np`, clear `c`, stop, leave/disconnect `dc`,
  join, moveme `mm`, clean, ping
- **Tuning:** volume `vol` (1–200), defaultvolume, loop (queue/song/off), shuffle `sh`,
  move `mv` (to top / from-to), remove `rm`, seek (flexible: `1:00`, `5`, `+70`, `-70`),
  forward `ff`, rewind `rw`, autoplay `ap`, filters (nightcore, speed, …), lyrics `ly`
- **Server:** 24/7 `24h`, autorejoin, announce (now-playing toggle), pauseat, leaveclear,
  **songstatus** (voice-channel status = current song), sleeptimer, noprefix, texttoggle,
  setdj, setdjonly, setup (request channel), prefix `sp`, reset-prefix, setsource, setlang
- **Playlists:** create, add, addtrack, addqueue, show, share, public, delete, lock,
  removedupes, remove, play, details, import
- **Extras:** XP/profile/shop/daily, activity, buttons under now-playing messages

### FlaviBot (flavibot.xyz) — music module
Modern multi-surface bot (slash + prefix + buttons + controller channel + web player).

- **Play:** `/play` (autocomplete, `insert-first`, playlist/album links), `/play-file`,
  `/tts`, `/radios` (catalogue + submissions), `/start` (genre panel)
- **Transport:** `/pause` `/resume` (button), `/skip` (**vote: ≤2 listeners instant,
  3–4 → 2 votes, 5+ → 60%**), `/previous`, `/jump`, `/seek` `H:M:S`, `/fastforward`,
  `/rewind` (10–3600 s), `/stop`, `/disconnect`, `/join`
- **Queue:** `/queue`, `/remove`, `/move`, `/shuffle`, `/clearqueue`, `/nowplaying`,
  `/history`, `/volume` 1–200 (+ −10/+10/100 buttons), `/loop` track/queue/disable
- **Extras:** `/autoplay`, `/24-7`, `/filter` (bassboost, nightcore, vaporwave, …),
  `/lyrics` (synced), `/fix` (change region / reconnect / recreate player), `/music-panel`,
  controller channel (type a song name → plays; `>` lines are comments, multi-line batches),
  now-playing buttons (pause/skip/stop/autoplay/dashboard/like/love), **DJ mode**
  (per-action Allow/Deny/Inherit for roles & members), per-user playlists with
  autoplay/vibe/top50 generation, Liked Songs, 10-minute idle disconnect (24/7 removes it),
  web player + dashboard

## Implementation matrix — DingDingDong

Legend: ✅ implemented · ➖ equivalent alternative · 🔜 planned (phase 3)

| Feature | Status | Notes |
|---|---|---|
| play / search / playtop / playskip | ✅ | **live autocomplete on /play — type and pick from a song list (Flavi-style)** |
| play-file | ✅ | `/playfile` attachment |
| pause / resume / replay / back / addprevious | ✅ | pause+resume buttons on now-playing |
| skip (direct + vote) | ✅ | Flavi thresholds; DJ/alone bypass |
| jump (skipto) | ✅ | first-class `/jump <position>` — straight to that queued track |
| queue (pagination) | ✅ | buttons Previous/Next/Close |
| nowplaying (+buttons) | ✅ | one panel message per player — edits in place (no spam on loops); rectangle 3×4 buttons + filter dropdown everywhere |
| songinfo | ✅ | artist / duration / source (YouTube / SoundCloud / radio) / position |
| clear / stop / disconnect / join / moveme | ✅ | |
| volume 1–200 | ✅ | seamless ffmpeg restart; persists per server as default volume |
| loop off/track/queue | ✅ | button cycles modes |
| shuffle / move / remove | ✅ | |
| seek / forward / rewind | ✅ | `1:30`, `90`, `+30`, `-30`, `1h2m3s` |
| filters | ✅ | **17 presets** — added deep, chipmunk, earrape, dance, party, soft; autocomplete on `/filter` |
| autoplay | ✅ | **AI picks the next song from the listener's last ~10 tracks + their playlists (Groq → Google failover); YouTube-mix fallback** |
| AI chat channel | ✅ | `/ai channel` — talk naturally: play, skip, pause, filters, volume, jump, queue questions… |
| AI Q&A | ✅ | `/ai ask` anywhere; every AI reply footers the model in use (`Groq AI · …` / `Google AI · …`) |
| AI recommendations | ✅ | `/recommend [@user]` — built from recent history + playlists |
| 24/7 + autorejoin | ✅ | `/24-7`, `/autorejoin` |
| lyrics | ✅ | lrclib.net (plain text; synced-live display 🔜) |
| radios | ✅ | 10 built-in stations + any custom stream URL |
| controller / request channel | ✅ | type a name to play, `>` comments, multi-line |
| songstatus (voice channel status) | ✅ | `/songstatus` toggle |
| announce toggle | ✅ | `/announce` |
| DJ role gating | ✅ | `/setdj`; role holders/managers/alone bypass. Per-action matrix 🔜 |
| per-user playlists | ✅ | create/add/addtrack/addqueue/**remove/rename**/show/play/delete/share/lock/list/public |
| playlist generation | ✅ | via `/recommend` (AI) + AI autoplay; dedicated `/playlist autoplay` 🔜 |
| history | ✅ | `/history` (last 100 per server) |
| idle auto-disconnect | ✅ | **2 h idle / 3 min empty (24/7 exempt) — and it actually leaves now (watchdog self-cancel bug fixed)** |
| vote system | ✅ | for skips |
| stats | ✅ | `/stats` — uptime, songs played, latency, yt-dlp version, AI model |
| embeds / UI | ✅ | emoji-spam stripped, progress-bar line replaced by a plain `3:45`; small panels |
| TTS | 🔜 | needs a TTS engine (edge-tts) |
| /fix panel (region/reconnect/recreate) | 🔜 | manual revenge: `/disconnect` → `/join` |
| web player / dashboard | 🔜 | separate web project |
| sleep timer / pauseat / leaveclear / removedupes | 🔜 | small follow-ups |
| prefix / reset-prefix / settings | ✅ | per-server prefix; `/settings` shows AI channel + volumes |
| texttoggle / noprefix / setlang / setsource | 🔜 | |
| Luna profile/economy (XP, daily, shop) | 🔜 | out of scope for the music phase |
| Spotify / Apple / Deezer / Tidal links | ➖ | need platform API keys — search by name instead |
| reliability: client fallback + stream verify + download retry | ✅ | beyond both reference bots' public docs |
| SoundCloud rescue | ✅ | relevance-scored: bot-walled YouTube searches play from SoundCloud instead |
| errors are backend-only | ✅ | raw yt-dlp/SoundCloud errors never reach Discord; friendly text only |

## v2 notes (2026-10-25)

- **AI:** `bot/ai.py` — Groq (`openai/gpt-oss-120b`) primary, Google (`gemini-3.8-flash`)
  fallback, automatic failover on quota/error; strict-JSON mode with plain-text retry;
  transient 503/timeout one-shot retry. Keys: `GROQ_API_KEY`, `GOOGLE_AI_API_KEY`.
  **On a hosting panel, set these as environment variables there too** (a panel-managed
  app usually does not read `.env`).
- **Fix notes:** `NowPlayingView._refresh` renamed → `_refresh_panel` (it was shadowing
  discord.py's internal sync `View._refresh`); `send_message(view=None)` → conditional
  kwargs (crashed with `NoneType.is_finished`); idle watchdog no longer cancels itself
  mid-disconnect; emoji stripped at embed level (`strip_leading_emoji`).
- **Autocomplete engine:** `Resolver.suggest()` — 5-minute cache, shielded background
  task, 2.4 s reply budget, YouTube → SoundCloud fallback. Values are direct URLs so
  picking a suggestion plays that exact track.

## Phase 3 candidates (priority order)

1. Synced/live lyrics overlay + `/tts`
2. DJ per-action permission matrix (Flavi DJ mode)
3. `/fix` panel (change voice region / reconnect / recreate player)
4. Web dashboard + player (biggest effort; needs hosting + OAuth)
5. Sleep timer / pauseat / leaveclear (small quality-of-life batch)
