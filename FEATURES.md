# LunaBot 🌙 + FlaviBot — research & feature matrix

Research date: 2026-10-23. Sources: lunabot.vc (commands/premium pages), discordbotlist.com,
bestdiscord.org command index, discord.bots.gg, luna.nx.kg, flavibot.xyz/docs (music module),
flavibot.xyz/features/music.

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

## Implementation matrix — DingDingDong v1

Legend: ✅ implemented · ➖ equivalent alternative · 🔜 planned (phase 2)

| Feature | Status | Notes |
|---|---|---|
| play / search / playtop / playskip | ✅ | query autocomplete not included; `/search` gives a pick-list instead |
| play-file | ✅ | `/playfile` attachment |
| pause / resume / replay / back / addprevious | ✅ | pause+resume buttons on now-playing |
| skip (direct + vote) | ✅ | Flavi thresholds; DJ/alone bypass |
| skipto / jump | ✅ | |
| queue (pagination) | ✅ | buttons ◀ ▶ ✖ |
| nowplaying (+buttons) | ✅ | pause/skip/stop/loop/autoplay buttons |
| clear / stop / disconnect / join / moveme | ✅ | |
| volume 1–200 | ✅ | applies via seamless ffmpeg restart |
| loop off/track/queue | ✅ | button cycles modes |
| shuffle / move / remove | ✅ | |
| seek / forward / rewind | ✅ | `1:30`, `90`, `+30`, `-30`, `1h2m3s` |
| autoplay (related tracks) | ✅ | seeded from the track's YouTube mix |
| 24/7 + autorejoin | ✅ | `/24-7`, `/autorejoin` |
| filters | ✅ | 11 presets (bassboost, nightcore, vaporwave, 8d, speed, slowed, treble, lofi, karaoke, …) |
| lyrics | ✅ | lrclib.net (plain text; synced-live display 🔜) |
| radios | ✅ | 10 built-in stations + any custom stream URL |
| controller / request channel | ✅ | `/controller create` — type a name to play, `>` comments, multi-line |
| songstatus (voice channel status) | ✅ | `/songstatus` toggle |
| announce toggle | ✅ | `/announce` |
| DJ role gating | ✅ | `/setdj`; role holders/managers/alone bypass. Per-action matrix 🔜 |
| per-user playlists | ✅ | create/add/addtrack/addqueue/show/play/delete/share/lock/list/public |
| playlist generation (autoplay/vibe/top50) | 🔜 | needs recommendation backend |
| history | ✅ | `/history` (last 100 per server) |
| idle auto-disconnect | ✅ | 10 min idle / 3 min empty (24/7 exempt) — matches Flavi |
| vote system | ✅ | for skips |
| TTS | 🔜 | needs a TTS engine (edge-tts) |
| /fix panel (region/reconnect/recreate) | 🔜 | manual revenge: `/disconnect` → `/join` |
| web player / dashboard | 🔜 | separate web project |
| sleep timer / pauseat / leaveclear / removedupes | 🔜 | small follow-ups |
| prefix / reset-prefix / settings | ✅ | per-server prefix |
| texttoggle / noprefix / setlang / setsource | 🔜 | |
| Luna profile/economy (XP, daily, shop) | 🔜 | out of scope for the music phase |
| Spotify / Apple / Deezer / Tidal links | ➖ | need platform API keys — search by name instead |
| reliability: client fallback + stream verify + download retry | ✅ | beyond both reference bots' public docs |

## Phase 2 candidates (priority order)

1. Synced/live lyrics overlay + `/tts`
2. DJ per-action permission matrix (Flavi DJ mode)
3. Playlist generation (similar tracks / server top) for `/playlist autoplay`
4. `/fix` panel (change voice region / reconnect / recreate player)
5. Web dashboard + player (biggest effort; needs hosting + OAuth)
