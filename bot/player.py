"""Per-guild playback engine: queue, runner loop, filters, seek, idle logic."""
from __future__ import annotations

import asyncio
import logging
import random
import shlex
import time
from collections import deque
from pathlib import Path
from typing import Optional

import discord

from . import config
from .store import add_history, get_history, guild_settings, set_setting, user_playlists
from .utils import fmt_duration, friendly_stream_error, np_embed, sanitize
from .views import NowPlayingView
from .ytdl import ExtractError, StreamError, Track

log = logging.getLogger("bot.player")


class UserError(Exception):
    """An error whose message should be shown to the user as-is."""


FILTERS: dict[str, Optional[str]] = {
    "off": None,
    "bassboost": "bass=g=12",
    "bassboost+": "bass=g=20",
    "nightcore": "aresample=48000,asetrate=60000,aresample=48000",
    "vaporwave": "aresample=48000,asetrate=38400,aresample=48000",
    "8d": "apulsator=hz=0.09",
    "speed": "atempo=1.3",
    "slowed": "atempo=0.85",
    "treble": "treble=g=6",
    "lofi": "lowpass=f=3200",
    "karaoke": "pan=stereo|c0=c0|c1=-1*c1",
    "deep": "aresample=48000,asetrate=40800,aresample=48000,atempo=1.176",
    "chipmunk": "aresample=48000,asetrate=66000,aresample=48000,atempo=0.727",
    "earrape": "volume=6",
    "dance": "bass=g=5,treble=g=3,aecho=0.8:0.9:40:0.3",
    "party": "aecho=0.8:0.88:60:0.4,acompressor",
    "soft": "volume=0.85,treble=g=-3,bass=g=3",
}


class GuildPlayer:
    def __init__(self, bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild

        self.queue: deque[Track] = deque()
        self.history: list[Track] = []
        self.current: Optional[Track] = None
        self.loop_mode: str = "off"
        self.autoplay: bool = False
        self.filter: str = "off"

        self._volume: float = 1.0
        self._runner: Optional[asyncio.Task] = None
        self._idle_watchdog: Optional[asyncio.Task] = None
        self._prefetch_task: Optional[asyncio.Task] = None

        self._done = asyncio.Event()
        self._after_error: Optional[BaseException] = None
        self._skip_flag = False
        self._seek_to: Optional[int] = None
        self._stopping = False
        self._disconnecting = False

        self._seek_base = 0
        self._started_at: Optional[float] = None
        self._paused_at: Optional[float] = None
        self._paused_total = 0.0

        self._autoplay_seen: set[str] = set()
        self._notice_channel: Optional[discord.abc.Messageable] = None
        self._np_message: Optional[discord.Message] = None

    # ------------------------------------------------------------------ state

    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client

    @property
    def is_playing(self) -> bool:
        vc = self.vc
        return bool(vc and vc.is_playing())

    @property
    def is_paused(self) -> bool:
        vc = self.vc
        return bool(vc and vc.is_paused())

    @property
    def volume(self) -> float:
        return self._volume

    def position(self) -> float:
        if self._started_at is None:
            return 0.0
        paused = self._paused_total
        if self._paused_at is not None:
            paused += time.monotonic() - self._paused_at
        return max(0.0, self._seek_base + (time.monotonic() - self._started_at) - paused)

    def previous_track(self) -> Optional[Track]:
        if len(self.history) >= 2:
            return self.history[-2]
        if self.history and self.current is None:
            return self.history[-1]
        return None

    async def play_previous(self) -> Optional[Track]:
        """Skip back to the previous track — shared by /back, /prev and the panel button."""
        prev = self.previous_track()
        if prev is None:
            return None
        self.queue.appendleft(prev)
        if self.is_playing or self.is_paused:
            await self.skip()
        else:
            await self.start()
        return prev

    def shuffle_queue(self) -> int:
        q = list(self.queue)
        random.shuffle(q)
        self.queue = deque(q)
        return len(q)

    # ------------------------------------------------------------------ voice

    async def ensure_voice(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        vc = self.vc
        if vc is not None and vc.is_connected():
            if vc.channel is None or vc.channel.id != channel.id:
                try:
                    await vc.move_to(channel)
                except discord.Forbidden as exc:
                    raise UserError("I can't move to that voice channel — check my **Connect** permission.") from exc
            return vc
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        try:
            return await channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
        except discord.Forbidden as exc:
            raise UserError("I need **Connect** and **Speak** permissions in that channel.") from exc
        except asyncio.TimeoutError as exc:
            raise UserError("Voice connection timed out — please try again.") from exc

    # ------------------------------------------------------------------ queue

    def enqueue(self, track: Track, *, insert_first: bool = False) -> int:
        if len(self.queue) >= config.MAX_QUEUE:
            raise UserError(f"The queue is full (max {config.MAX_QUEUE} tracks).")
        if insert_first:
            self.queue.appendleft(track)
            return 1
        self.queue.append(track)
        return len(self.queue)

    def extend_queue(self, tracks: list[Track]) -> int:
        room = max(0, config.MAX_QUEUE - len(self.queue))
        added = tracks[:room]
        self.queue.extend(added)
        return len(added)

    def move_track(self, from_pos: int, to_pos: int) -> Track:
        q = list(self.queue)
        if not (1 <= from_pos <= len(q)) or not (1 <= to_pos <= len(q)):
            raise UserError(f"Positions must be between 1 and {len(q)}.")
        track = q.pop(from_pos - 1)
        q.insert(to_pos - 1, track)
        self.queue = deque(q)
        return track

    def remove_track(self, pos: int) -> Track:
        q = list(self.queue)
        if not (1 <= pos <= len(q)):
            raise UserError(f"Position must be between 1 and {len(q)}.")
        track = q.pop(pos - 1)
        self.queue = deque(q)
        return track

    def jump_to(self, position: int) -> Track:
        """Drop everything before `position` and return the track that plays next."""
        q = list(self.queue)
        if not q:
            raise UserError("The queue is empty.")
        if not (1 <= position <= len(q)):
            raise UserError(f"Position must be between 1 and {len(q)}.")
        for _ in range(position - 1):
            skipped = self.queue.popleft()
            self.history.append(skipped)
        del self.history[:-100]
        return self.queue[0]

    # ------------------------------------------------------------------ runner

    def _cancel_idle_watchdog(self) -> None:
        task = self._idle_watchdog
        # Never cancel the watchdog from inside itself: disconnect() calls this
        # while running inside the watchdog task, and self-cancellation would
        # abort the disconnect before the voice client actually leaves.
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._idle_watchdog = None

    async def start(self) -> None:
        self._cancel_idle_watchdog()
        if self._runner is None or self._runner.done():
            self._stopping = False
            self._disconnecting = False
            self._runner = self.bot.loop.create_task(self._run_guarded())

    async def _run_guarded(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("runner crashed in guild %s", self.guild.id)
        finally:
            self.current = None
            if not self._disconnecting:
                self._idle_watchdog = self.bot.loop.create_task(self._idle_watch())

    async def _run(self) -> None:
        while not self._stopping:
            if self._seek_to is not None and self.current is not None:
                track = self.current
                seek = max(0, int(self._seek_to))
                self._seek_to = None
            else:
                track = await self._next_track()
                seek = 0
                if track is None:
                    return
            try:
                err = await self._play_track(track, seek=seek)
            except (StreamError, ExtractError, UserError) as exc:
                log.warning("playback failed in guild %s for %r: %s", self.guild.id, track.title, exc)
                await self._notice(f"Couldn't play **{sanitize(track.title)}** — {friendly_stream_error(str(exc))}")
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("playback loop error in guild %s", self.guild.id)
                continue
            if self._stopping:
                return
            if self._skip_flag:
                self._skip_flag = False
                continue
            if err is not None:
                await self._handle_ffmpeg_error(track, err)
                self._skip_flag = False
                continue
            # natural end of track
            if self.loop_mode == "track":
                self._seek_to = 0
            elif self.loop_mode == "queue":
                self.queue.append(track)

    def _schedule_prefetch(self) -> None:
        """Warm the next queued track's stream while the current one plays.

        A cold YouTube resolve costs ~12s; resolving during playback means the
        gap between songs is ~0 instead of dead air. Best-effort and silent.
        """
        task = self._prefetch_task
        if task is not None and not task.done():
            return
        if not self.queue:
            return
        nxt = self.queue[0]
        if nxt is None or nxt.direct_stream or nxt.stream_url:
            return
        if not (nxt.video_id or nxt.source_url or nxt.webpage_url):
            return
        try:
            self._prefetch_task = self.bot.loop.create_task(self._prefetch_stream(nxt))
        except RuntimeError:
            self._prefetch_task = None

    async def _prefetch_stream(self, track: Track) -> None:
        try:
            await self.bot.resolver.get_stream(track)
            log.info("prefetched %r — next track starts instantly", track.title)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("prefetch for %r failed (harmless): %r", track.title, exc)

    async def _next_track(self) -> Optional[Track]:
        if not self.queue and self.autoplay and self.current is not None:
            self.queue.extend(await self._autoplay_fill())
        if not self.queue:
            self.current = None
            return None
        track = self.queue.popleft()
        self.current = track
        self.history.append(track)
        del self.history[:-100]
        if track.video_id:
            self._autoplay_seen.add(track.video_id)
        try:
            add_history(self.guild.id, track.to_dict())
        except Exception:
            log.debug("history save failed", exc_info=True)
        return track

    async def _autoplay_fill(self) -> list[Track]:
        """Queue sources when the queue runs dry: AI suggestion first (based on
        the listener's recent tracks and playlists), classic YouTube mix as fallback."""
        current = self.current
        if current is None:
            return []
        ai = getattr(self.bot, "ai", None)
        if ai is not None and ai.available:
            try:
                seed_id = current.requester_id
                hist = get_history(self.guild.id, 80)
                recent = [h.get("title") or "" for h in hist if seed_id and h.get("requester_id") == seed_id][:10]
                if not recent:
                    recent = [h.get("title") or "" for h in hist][:10]
                pl_songs: list[str] = []
                if seed_id:
                    for pl in list(user_playlists(seed_id).values())[:3]:
                        pl_songs += [t.get("title") or "" for t in (pl.get("tracks") or [])][:15]
                query = await ai.suggest_track(recent=recent, playlists=pl_songs, current=current.title)
                if query:
                    track: Optional[Track] = None
                    try:
                        kind, tracks = await self.bot.resolver.resolve(query, requester=None)
                        if kind == "track" and tracks:
                            track = tracks[0]
                    except ExtractError:
                        track = await self.bot.resolver.sc_search(query)
                    if track is not None:
                        track.requester_id = current.requester_id
                        track.requester_name = current.requester_name or "Autoplay"
                        await self._notice(
                            f"Autoplay picked **{sanitize(track.title)}** ({ai.last_label or 'AI'})."
                        )
                        return [track]
            except Exception:
                log.debug("ai autoplay failed", exc_info=True)
        try:
            related = await self.bot.resolver.related(current, 5, seen=self._autoplay_seen)
        except Exception:
            return []
        if related:
            await self._notice("Autoplay queued 5 related tracks.")
        return related

    # ------------------------------------------------------------------ playback

    def _audio_filter_string(self) -> Optional[str]:
        parts = []
        preset = FILTERS.get(self.filter)
        if preset:
            parts.append(preset)
        if abs(self._volume - 1.0) > 0.001:
            parts.append(f"volume={self._volume:.2f}")
        return ",".join(parts) if parts else None

    def _make_source(self, target: str, headers: Optional[dict], *, seek: int = 0, local: bool = False):
        before = ["-nostdin"]
        if not local:
            before += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
            ]
            if headers:
                blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                before += ["-headers", blob]
        if seek and seek > 0:
            before += ["-ss", str(int(seek))]
        options = ["-vn"]
        af = self._audio_filter_string()
        if af:
            options += ["-af", af]
        before_str = " ".join(shlex.quote(str(p)) for p in before)
        options_str = " ".join(shlex.quote(str(p)) for p in options)
        return discord.FFmpegOpusAudio(
            target,
            before_options=before_str,
            options=options_str,
            executable=config.FFMPEG_BIN,
        )

    def _after_cb(self, error: Optional[BaseException]) -> None:
        self._after_error = error
        try:
            self.bot.loop.call_soon_threadsafe(self._done.set)
        except RuntimeError:
            pass

    async def _play_track(self, track: Track, *, seek: int = 0) -> Optional[BaseException]:
        vc = self.vc
        if vc is None or not vc.is_connected():
            raise StreamError("I got disconnected from the voice channel.")
        if track.direct_stream:
            url, headers = track.webpage_url, (track.http_headers or {})
        elif track.stream_url and (time.time() - track.stream_at) < 3600:
            url, headers = track.stream_url, track.http_headers
        else:
            try:
                url, headers, fresh = await self.bot.resolver.get_stream(track)
            except StreamError:
                fallback = None
                try:
                    fallback = await self.bot.resolver.sc_fallback(
                        track, query_hint=getattr(track, "query", "") or None
                    )
                except Exception:
                    log.debug("soundcloud fallback lookup failed", exc_info=True)
                if fallback is None:
                    raise
                # Silently switch to the SoundCloud match — users shouldn't see
                # platform plumbing, only music.
                log.info(
                    "youtube blocked %r — silently switched to soundcloud match %r",
                    track.title,
                    fallback.title,
                )
                track = fallback
                url, headers, fresh = await self.bot.resolver.get_stream(track)
            track.stream_url = url
            track.http_headers = headers
            track.stream_at = time.time()
            if fresh:
                if fresh.get("duration") and not track.duration:
                    track.duration = int(fresh["duration"])
                track.thumbnail = track.thumbnail or fresh.get("thumbnail")
                track.video_id = fresh.get("id") or track.video_id
                track.is_live = bool(fresh.get("is_live"))
        source = self._make_source(url, headers, seek=seek)
        self._done.clear()
        self._after_error = None
        self._seek_base = int(seek or 0)
        self._started_at = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0
        try:
            vc.play(source, after=self._after_cb)
        except Exception as exc:  # noqa: BLE001
            return exc
        await self._announce_track(track)
        self._schedule_prefetch()
        await self._done.wait()
        return self._after_error

    async def _play_local(self, track: Track, path: Path) -> Optional[BaseException]:
        vc = self.vc
        if vc is None or not vc.is_connected():
            return StreamError("I got disconnected from the voice channel.")
        source = self._make_source(str(path), None, local=True)
        self._done.clear()
        self._after_error = None
        self._seek_base = 0
        self._started_at = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0
        try:
            vc.play(source, after=self._after_cb)
        except Exception as exc:  # noqa: BLE001
            return exc
        await self._done.wait()
        return self._after_error

    async def _handle_ffmpeg_error(self, track: Track, err: BaseException) -> bool:
        """One download-based retry per track. Returns True if handled."""
        if track.fallback_done or track.direct_stream:
            log.warning("playback failed twice in guild %s for %r: %r", self.guild.id, track.title, err)
            await self._notice(f"Playback of **{sanitize(track.title)}** failed — skipping. {friendly_stream_error(str(err))}")
            return False
        track.fallback_done = True
        track.stream_url = None
        try:
            self.bot.resolver.drop_cached(track)
        except Exception:  # noqa: BLE001
            log.debug("cache drop failed", exc_info=True)
        log.info("stream died for %r — retrying via download (silent)", track.title)
        try:
            path = await self.bot.resolver.download(track)
        except Exception as exc:  # noqa: BLE001
            log.warning("download fallback failed: %r", exc)
            await self._notice(f"Couldn't play **{sanitize(track.title)}** — skipping.")
            return False
        try:
            err2 = await self._play_local(track, path)
            if err2 is None:
                return True
            await self._notice(f"Couldn't play **{sanitize(track.title)}** — skipping.")
            return False
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------ controls

    async def skip(self) -> None:
        self._seek_to = None
        vc = self.vc
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            self._skip_flag = True
            vc.stop()
        else:
            self._skip_flag = False
            await self.start()

    async def seek(self, seconds: int) -> None:
        if self.current is None:
            raise UserError("Nothing is playing.")
        if self.current.is_live or self.current.duration is None:
            raise UserError("This is a live stream — seeking isn't possible.")
        if seconds < 0:
            seconds = 0
        if self.current.duration and seconds >= self.current.duration:
            raise UserError(f"The track is only {fmt_duration(self.current.duration)} long.")
        self._seek_to = int(seconds)
        await self._kick_source()

    async def _kick_source(self) -> None:
        vc = self.vc
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        else:
            await self.start()

    def _soft_restart(self) -> None:
        """Rebuild the ffmpeg source at the current position (volume/filter changes)."""
        vc = self.vc
        if vc is None or self.current is None:
            return
        if not (vc.is_playing() or vc.is_paused()):
            return
        if self.current.is_live:
            self._seek_to = 0
        else:
            self._seek_to = int(self.position())
        vc.stop()

    async def set_volume(self, level: int) -> None:
        level = max(1, min(200, int(level)))
        self._volume = level / 100.0
        self._soft_restart()
        try:
            set_setting(self.guild.id, "default_volume", level)
        except Exception:
            pass

    async def set_filter(self, name: str) -> None:
        key = (name or "").strip().lower()
        if key not in FILTERS:
            raise UserError(f"Unknown filter `{name}`. Use {', '.join(f'`{k}`' for k in FILTERS)}.")
        self.filter = key
        self._soft_restart()

    def pause(self) -> bool:
        vc = self.vc
        if vc is not None and vc.is_playing() and not vc.is_paused():
            vc.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        vc = self.vc
        if vc is not None and vc.is_paused():
            vc.resume()
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
            return True
        return False

    async def stop(self, *, keep_current: bool = False) -> None:
        """Stop playback and clear the queue, staying connected."""
        if not keep_current:
            self.queue.clear()
        self.current = None
        self._skip_flag = False
        self._seek_to = None
        self._stopping = True
        self._disconnecting = False
        await self._update_song_status(None)
        vc = self.vc
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        if self._runner is not None and not self._runner.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._runner), timeout=5)
            except Exception:
                pass

    async def disconnect(self) -> None:
        self._stopping = True
        self._disconnecting = True
        self.queue.clear()
        self.current = None
        self._skip_flag = False
        self._seek_to = None
        self._cancel_idle_watchdog()
        await self._update_song_status(None)
        vc = self.vc
        if vc is not None:
            if vc.is_playing() or vc.is_paused():
                try:
                    vc.stop()
                except Exception:
                    pass
            try:
                await vc.disconnect(force=False)
            except Exception:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        if self._runner is not None and not self._runner.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._runner), timeout=5)
            except Exception:
                pass
        self._disconnecting = False
        self._stopping = False

    # ------------------------------------------------------------------ messages

    async def _notice(self, text: str) -> None:
        log.info("guild %s notice: %s", self.guild.id, text)
        ch = self._notice_channel
        if ch is None:
            return
        try:
            await ch.send(text[:1900])
        except Exception:
            self._notice_channel = None

    async def _announce_track(self, track: Track) -> None:
        self.bot._songs_played = getattr(self.bot, "_songs_played", 0) + 1
        settings = guild_settings(self.guild.id)
        if settings.get("announce", True) and self._notice_channel is not None:
            e = np_embed(
                track,
                position=0.0,
                loop_mode=self.loop_mode,
                volume=self._volume,
                filter_name=self.filter,
                queue_len=len(self.queue),
                autoplay=self.autoplay,
            )
            view = NowPlayingView(self)
            edited = False
            # One panel message per player: edit it in place — loop repeats and
            # each next track update the same message instead of posting a new one.
            if self._np_message is not None:
                if self._np_message.channel.id != getattr(self._notice_channel, "id", None):
                    self._np_message = None
                else:
                    try:
                        await self._np_message.edit(embed=e, view=view)
                        edited = True
                    except discord.NotFound:
                        self._np_message = None
                    except Exception:
                        self._np_message = None
            if not edited:
                try:
                    self._np_message = await self._notice_channel.send(embed=e, view=view)
                except Exception:
                    self._np_message = None
        if settings.get("songstatus", True):
            await self._update_song_status(track)

    async def _update_song_status(self, track: Optional[Track]) -> None:
        vc = self.vc
        if vc is None or vc.channel is None:
            return
        text = sanitize(track.title, 90)[:500] if track is not None else None
        try:
            await vc.channel.edit(status=text)
        except Exception:
            pass

    # ------------------------------------------------------------------ idle

    async def _idle_watch(self) -> None:
        started = time.monotonic()
        try:
            while True:
                await asyncio.sleep(15)
                if self.current is not None or self.queue:
                    return
                vc = self.vc
                if vc is None or not vc.is_connected():
                    return
                settings = guild_settings(self.guild.id)
                if settings.get("stay247"):
                    continue
                humans = [m for m in (vc.channel.members if vc.channel else []) if not m.bot]
                idle = time.monotonic() - started
                if not humans and idle >= config.EMPTY_CHANNEL_SECONDS:
                    await self._notice("Nobody is left in the channel — disconnecting.")
                    await self.disconnect()
                    return
                if idle >= config.IDLE_LEAVE_SECONDS:
                    await self._notice("I've been idle for too long — disconnecting. (Enable **24/7** to keep me here.)")
                    await self.disconnect()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("idle watchdog error", exc_info=True)
