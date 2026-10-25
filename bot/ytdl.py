"""yt-dlp wrapper: search, metadata, stream resolution (with player-client
fallback + HTTP verification) and the download fallback used when a live
stream dies mid-playback.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yt_dlp

from . import config

log = logging.getLogger("bot.ytdl")

# Player-client fallback order for YouTube extraction. "" = yt-dlp default.
# Which clients survive changes every few weeks — run tools/test_youtube.py to
# print the current matrix for this machine, and reorder here if it shifts again.
STREAM_CLIENTS: list[str] = ["", "android_vr", "tv_embedded", "android", "tv", "web_safari", "web_embedded", "mweb", "ios"]
# Second-pass clients: the checks are probabilistic, so after a short pause the
# top clients are retried once before giving up.
RETRY_CLIENTS: list[str] = ["", "android_vr", "tv_embedded"]
# Errors that mean "stop retrying": the video itself is gone/restricted.
PERMANENT_ERROR_HINTS = ("video unavailable", "private video", "has been removed", "members-only", "does not exist")
# Errors meaning "this IP is bot-walled". Retrying other player clients barely
# helps and each sweep costs 30-60 s, so the first confirmation opens a circuit
# breaker: YouTube is checked ONCE, then SoundCloud serves the rest of the day.
BOTWALL_HINTS = (
    "sign in to confirm",
    "not a bot",
    "use --cookies",
    "cookies for the authentication",
    "this helps protect our community",
    "too many requests",
    "http error 429",
)
BREAK_FILE_NAME = "yt_breaker.json"

UNSUPPORTED_DOMAINS = {
    "open.spotify.com": "Spotify",
    "spotify.link": "Spotify",
    "music.apple.com": "Apple Music",
    "deezer.com": "Deezer",
    "tidal.com": "Tidal",
    "music.amazon.": "Amazon Music",
}

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".webm")
RADIO_HINTS = ("somafm.com", "radioparadise.com", "icecast", ":8000", "/stream")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class ExtractError(Exception):
    """Extraction failed or returned nothing usable."""


class UnsupportedPlatform(Exception):
    """Link points at a platform we cannot stream from directly."""


class StreamError(Exception):
    """No playable stream URL could be resolved."""


def _clean_error(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    text = text.replace("ERROR: ", "").strip()
    if len(text) > 220:
        text = text[:219] + "…"
    return text or "unknown extraction error"


@dataclass
class Track:
    title: str
    webpage_url: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    requester_id: Optional[int] = None
    requester_name: str = ""
    is_live: bool = False
    direct_stream: bool = False
    video_id: Optional[str] = None
    stream_url: Optional[str] = None
    stream_at: float = 0.0
    http_headers: dict = field(default_factory=dict)
    fallback_done: bool = False

    # ---------------- construction ----------------

    @classmethod
    def from_info(cls, info: dict, *, requester=None, direct: bool = False) -> "Track":
        webpage = (
            info.get("webpage_url")
            or info.get("original_url")
            or (info.get("url") if direct else None)
            or ""
        )
        thumb = info.get("thumbnail")
        if not thumb:
            thumbs = info.get("thumbnails") or []
            if thumbs:
                thumb = thumbs[-1].get("url")
        duration = info.get("duration")
        return cls(
            title=info.get("title") or "Unknown title",
            webpage_url=webpage,
            duration=int(duration) if duration else None,
            thumbnail=thumb,
            uploader=info.get("uploader") or info.get("channel") or info.get("artist"),
            requester_id=getattr(requester, "id", None),
            requester_name=(getattr(requester, "display_name", None) or getattr(requester, "name", "") or ""),
            is_live=bool(info.get("is_live")),
            direct_stream=direct,
            video_id=info.get("id"),
            stream_url=info.get("url") if direct else None,
            http_headers=info.get("http_headers") or {},
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "webpage_url": self.webpage_url,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "uploader": self.uploader,
            "requester_id": self.requester_id,
            "requester_name": self.requester_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(
            title=data.get("title") or "Unknown title",
            webpage_url=data.get("webpage_url") or "",
            duration=data.get("duration"),
            thumbnail=data.get("thumbnail"),
            uploader=data.get("uploader"),
            requester_id=data.get("requester_id"),
            requester_name=data.get("requester_name") or "",
        )

    @property
    def source_url(self) -> str:
        if self.webpage_url:
            return self.webpage_url
        if self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}"
        return ""


class Resolver:
    """Runs yt-dlp (synchronous) inside a small thread pool."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdlp")
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self.working_client: Optional[str] = None
        self._yt_break_until: float = self._load_breaker()
        self._suggest_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        self._suggest_tasks: dict[str, asyncio.Task] = {}

    # ---------------- lifecycle ----------------

    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
            return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._pool.shutdown(wait=False)

    # ---------------- youtube circuit breaker ----------------

    def _breaker_path(self) -> Path:
        return config.DATA_DIR / BREAK_FILE_NAME

    def _load_breaker(self) -> float:
        """Restore a previous trip, unless fresh cookies landed after it."""
        path = self._breaker_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            until = float(data.get("until") or 0)
            tripped = float(data.get("tripped") or 0)
        except Exception:  # noqa: BLE001
            return 0.0
        if until <= time.time():
            return 0.0
        cookie_file = config.YTDLP_COOKIE_FILE
        if cookie_file and tripped:
            cp = Path(cookie_file)
            if not cp.is_absolute():
                cp = config.BASE_DIR / cp
            try:
                if cp.exists() and cp.stat().st_mtime > tripped:
                    log.info("youtube cookies were refreshed — resetting the circuit breaker")
                    self._clear_breaker()
                    return 0.0
            except OSError:
                pass
        log.info("youtube circuit open for another %.0f min — soundcloud only", (until - time.time()) / 60)
        return until

    def _clear_breaker(self) -> None:
        self._yt_break_until = 0.0
        try:
            self._breaker_path().unlink(missing_ok=True)
        except OSError:
            pass

    def _trip_breaker(self, reason: str) -> None:
        window = max(60, config.YOUTUBE_BREAK_SECONDS)
        now = time.time()
        self._yt_break_until = now + window
        try:
            self._breaker_path().write_text(
                json.dumps({"tripped": now, "until": self._yt_break_until, "reason": str(reason)[:200]}),
                encoding="utf-8",
            )
        except OSError:
            pass
        log.warning(
            "youtube bot-walled this host — youtube skipped for %.1f h (soundcloud only): %s",
            window / 3600, str(reason)[:140],
        )

    def yt_blocked(self) -> bool:
        """True while YouTube should be skipped entirely."""
        if self._yt_break_until and time.time() >= self._yt_break_until:
            self._clear_breaker()
            return False
        return bool(self._yt_break_until)

    @staticmethod
    def _looks_like_botwall(message: str) -> bool:
        low = (message or "").lower()
        return any(hint in low for hint in BOTWALL_HINTS)

    @staticmethod
    def _is_youtube_target(target: str) -> bool:
        low = (target or "").lower()
        return "youtube.com" in low or "youtu.be" in low

    # ---------------- sync core ----------------

    def _opts(
        self,
        *,
        client: Optional[str] = None,
        flat: Any = None,
        playlist: bool = False,
        extra: Optional[dict] = None,
    ) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": not playlist,
            "default_search": "ytsearch",
            "skip_download": True,
            "format": "bestaudio[acodec=opus]/bestaudio/bestaudio*/best",
            "socket_timeout": 20,
            "retries": 2,
            "cachedir": str(config.YTDLP_CACHE_DIR),
        }
        cookies = config.YTDLP_COOKIES_FROM_BROWSER
        if cookies:
            browser = "firefox" if cookies == "auto" else cookies
            opts["cookiesfrombrowser"] = (browser,)
        cookie_file = config.YTDLP_COOKIE_FILE
        if cookie_file:
            path = Path(cookie_file)
            if not path.is_absolute():
                path = config.BASE_DIR / path
            if path.exists():
                opts["cookiefile"] = str(path)
            else:
                log.warning("YTDLP_COOKIE_FILE is set but the file doesn't exist: %s", path)
        if flat is not None:
            opts["extract_flat"] = flat
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if extra:
            opts.update(extra)
        return opts

    def _extract_sync(
        self,
        target: str,
        *,
        client: Optional[str] = None,
        flat: Any = None,
        playlist: bool = False,
        extra: Optional[dict] = None,
    ) -> dict:
        with yt_dlp.YoutubeDL(self._opts(client=client, flat=flat, playlist=playlist, extra=extra)) as ydl:
            info = ydl.extract_info(target, download=False)
        if not isinstance(info, dict):
            raise ExtractError("Extractor returned no data.")
        entries = info.get("entries")
        if entries is not None:
            entries = [e for e in entries if isinstance(e, dict)]
            if not entries:
                raise ExtractError("No results found.")
            if playlist:
                info["entries"] = entries
                return info
            return entries[0]
        return info

    async def _extract(self, target: str, **kwargs) -> dict:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._pool, functools.partial(self._extract_sync, target, **kwargs)
            )
        except ExtractError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise ExtractError(_clean_error(str(exc))) from exc
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(_clean_error(f"{type(exc).__name__}: {exc}")) from exc

    @staticmethod
    def _is_permanent_error(message: str) -> bool:
        low = (message or "").lower()
        return any(hint in low for hint in PERMANENT_ERROR_HINTS)

    async def _flat_search(self, target: str) -> dict:
        """Flat search: searches hit the same bot-checks as playback (much more
        aggressive on datacenter/host IPs). A confirmed bot-wall trips the
        circuit breaker right away, so YouTube is checked ONCE and SoundCloud
        handles the rest of the day instead of every play re-timing-out."""
        if self.yt_blocked():
            raise ExtractError("youtube skipped (bot-check) — using soundcloud")
        try:
            return await self._extract(target, flat=True, playlist=True)
        except ExtractError as exc:
            msg = str(exc)
            if self._is_permanent_error(msg):
                raise
            if self._looks_like_botwall(msg):
                await asyncio.sleep(0.8)
                try:
                    return await self._extract(target, client="android_vr", flat=True, playlist=True)
                except ExtractError as exc2:
                    if self._looks_like_botwall(str(exc2)):
                        self._trip_breaker(str(exc2))
                    raise
            await asyncio.sleep(1.5)
            for client in ("android_vr", "tv_embedded"):
                try:
                    return await self._extract(target, client=client, flat=True, playlist=True)
                except ExtractError:
                    continue
            raise

    # ---------------- public API ----------------

    @staticmethod
    def is_url(text: str) -> bool:
        return text.startswith("http://") or text.startswith("https://")

    @staticmethod
    def _is_direct_audio(url: str) -> bool:
        low = url.lower()
        path = low.split("?", 1)[0]
        return path.endswith(AUDIO_EXTENSIONS) or any(h in low for h in RADIO_HINTS)

    @staticmethod
    def _is_playlist_url(low: str) -> bool:
        if "/playlist" in low:
            return True
        if "list=" in low and "watch?v=" not in low:
            return True
        return False

    async def search(self, query: str, limit: int = 5) -> list["Track"]:
        if self.yt_blocked():
            return await self.sc_search_many(query, limit=limit)
        info = await self._flat_search(f"ytsearch{limit}:{query}")
        out = []
        for entry in info.get("entries", []):
            if entry.get("title"):
                out.append(Track.from_info(entry))
        return out

    async def resolve(
        self,
        query: str,
        *,
        requester=None,
        playlist_limit: Optional[int] = None,
    ) -> tuple[str, list["Track"]]:
        """Returns (kind, tracks) where kind is 'track' or 'playlist'."""
        q = (query or "").strip()
        if not q:
            raise ExtractError("Empty search query.")
        limit = playlist_limit or config.PLAYLIST_URL_LIMIT
        if self.is_url(q):
            return await self._resolve_url(q, requester=requester, playlist_limit=limit)
        info = await self._flat_search(f"ytsearch1:{q}")
        entries = info.get("entries") or []
        if not entries:
            raise ExtractError(f"No results for `{q}`.")
        return "track", [Track.from_info(entries[0], requester=requester)]

    async def _resolve_url(self, url: str, *, requester, playlist_limit: int) -> tuple[str, list["Track"]]:
        low = url.lower()
        if self._is_youtube_target(low) and self.yt_blocked():
            raise ExtractError("youtube skipped (bot-check) — try the song name instead")
        for domain, platform in UNSUPPORTED_DOMAINS.items():
            if domain in low:
                raise UnsupportedPlatform(
                    f"{platform} links can't be streamed directly yet — search by song name instead."
                )
        if self._is_direct_audio(url):
            title = url.rstrip("/").split("/")[-1].split("?")[0] or url
            return "track", [Track(title=title, webpage_url=url, direct_stream=True, is_live=True)]
        if self._is_playlist_url(low):
            info = await self._extract(
                url, flat=True, playlist=True, extra={"playlistend": max(1, playlist_limit)}
            )
            tracks = [Track.from_info(e, requester=requester) for e in info.get("entries", []) if e.get("title")]
            if not tracks:
                raise ExtractError("That playlist is empty or private.")
            return "playlist", tracks
        info = await self._extract(url)
        return "track", [Track.from_info(info, requester=requester)]

    # ---------------- stream resolution ----------------

    async def verify(self, url: str, headers: dict) -> tuple[bool, str]:
        """Quick HTTP check that the stream URL actually serves audio."""
        try:
            session = await self.session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status not in (200, 206):
                    return False, f"HTTP {resp.status}"
                chunk = await resp.content.read(32768)
                if not chunk:
                    return False, "empty response body"
                return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def get_stream(self, track: "Track") -> tuple[str, dict, dict]:
        """Resolve (stream_url, http_headers, fresh_info).

        Pass 1: every client in STREAM_CLIENTS (the last one that worked first).
        Pass 2: if everything failed with a *soft* error (bot-check / format
        churn — these checks are probabilistic), retry the top clients once
        after a short pause before giving up."""
        if track.direct_stream:
            return track.webpage_url, track.http_headers or {}, {}
        target = track.source_url
        if not target:
            raise StreamError("Track has no source URL.")
        if self._is_youtube_target(target) and self.yt_blocked():
            # breaker open — let the player's SoundCloud rescue handle it
            # instead of running the 30-60 s client sweep all over again
            raise StreamError("youtube skipped (bot-check) — using soundcloud")
        clients: list[str] = []
        if self.working_client is not None:
            clients.append(self.working_client)
        clients += [c for c in STREAM_CLIENTS if c != self.working_client]
        last_err: Optional[Exception] = None
        botwall_hits = 0
        for pass_no in (1, 2):
            client_list = clients if pass_no == 1 else [c for c in RETRY_CLIENTS if c != self.working_client]
            for client in client_list:
                label = client or "(default)"
                try:
                    info = await self._extract(target, client=client)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if self._looks_like_botwall(str(exc)):
                        botwall_hits += 1
                        if botwall_hits >= 2:
                            self._trip_breaker(str(exc))
                            raise StreamError("youtube bot-check — switching to soundcloud") from exc
                    log.debug("pass %d: extract failed for client %s: %r", pass_no, label, exc)
                    continue
                url = info.get("url")
                if not url:
                    last_err = StreamError("extraction returned no direct audio URL")
                    continue
                headers = info.get("http_headers") or {}
                ok, detail = await self.verify(url, headers)
                if not ok:
                    last_err = StreamError(f"stream check failed: {detail}")
                    log.debug("pass %d: verify failed for client %s: %s", pass_no, label, detail)
                    continue
                if self.working_client != client:
                    self.working_client = client
                    log.info("using youtube player client: %s", label)
                return url, headers, info
            if pass_no == 1:
                if self._is_permanent_error(str(last_err)):
                    break
                log.info("all clients failed once (%s) — retrying top clients", last_err)
                await asyncio.sleep(2.5)
        raise StreamError(f"no playable stream found ({last_err})")

    async def _sc_candidates(self, query: str) -> list["Track"]:
        """Raw SoundCloud candidates, trying progressively simpler query
        variants until one returns results. Durations are normalized."""
        base = re.sub(r"\s+", " ", (query or "").strip())
        if not base:
            return []
        attempts = [base]
        simple = re.sub(r"[^\w\s'-]", " ", base)
        simple = re.sub(r"\s+", " ", simple).strip()
        if simple and simple.lower() != base.lower():
            attempts.append(simple)
        short = " ".join(simple.split()[:3])
        if short and short.lower() not in [a.lower() for a in attempts]:
            attempts.append(short)
        for attempt in attempts:
            try:
                info = await self._extract(f"scsearch10:{attempt}", flat=True, playlist=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("soundcloud search failed for %r: %r", attempt, exc)
                continue
            candidates = [Track.from_info(e) for e in info.get("entries") or [] if e.get("title")]
            if not candidates:
                continue
            for cand in candidates:  # normalize odd ms durations defensively
                if cand.duration and cand.duration > 20000:
                    cand.duration = cand.duration // 1000
            return candidates
        return []

    @staticmethod
    def _sc_score(query: str, track: "Track") -> float:
        """Relevance of a SoundCloud hit: fraction of meaningful query words in
        the title, +1 when the whole query appears squashed together (guards
        against SoundCloud's loose matching — 'Grown & Sexy' for 'sexy baka')."""
        words = [w for w in re.sub(r"[^\w\s]", " ", (query or "").lower()).split() if len(w) > 1]
        if not words:
            return 0.0
        title_low = (track.title or "").lower()
        score = sum(1 for w in words if w in title_low) / len(words)
        squashed_q = re.sub(r"[^a-z0-9]", "", (query or "").lower())
        if squashed_q and squashed_q in re.sub(r"[^a-z0-9]", "", title_low):
            score += 1.0
        return score

    async def sc_search(
        self,
        query: str,
        *,
        requester=None,
        target_duration: Optional[int] = None,
        requester_id: Optional[int] = None,
        requester_name: str = "",
    ) -> Optional["Track"]:
        """Best SoundCloud match for a query — the cookie-free rescue path when
        YouTube bot-walls or filters a search (common on hosts). Requires real
        word overlap with the query; within the best-scoring group, prefers the
        duration closest to target_duration."""
        candidates = await self._sc_candidates(query)
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda t: self._sc_score(query, t), reverse=True)
        top_score = self._sc_score(query, ranked[0])
        if top_score < 0.5:
            return None
        pool = [t for t in ranked if self._sc_score(query, t) >= top_score - 0.01]
        if target_duration:
            with_dur = [t for t in pool if t.duration]
            best = min(with_dur, key=lambda t: abs(t.duration - target_duration)) if with_dur else pool[0]
        else:
            best = next((t for t in pool if t.duration and t.duration >= 60), pool[0])
        best.requester_id = requester_id if requester_id is not None else getattr(requester, "id", None)
        best.requester_name = requester_name or (
            getattr(requester, "display_name", None) or getattr(requester, "name", "") or ""
        )
        return best

    async def sc_search_many(self, query: str, limit: int = 8) -> list["Track"]:
        """SoundCloud results list for /search fallback, best-relevance first."""
        candidates = await self._sc_candidates(query)
        candidates.sort(key=lambda t: self._sc_score(query, t), reverse=True)
        return candidates[:limit]

    # ---------------- slash autocomplete ----------------

    async def suggest(self, query: str, *, limit: int = 8) -> list[tuple[str, str]]:
        """(label, value) pairs for /play autocomplete (Flavi-style live list).

        Cached for 5 minutes; work keeps running in the background when Discord's
        ~3 s reply budget would be exceeded, so the next keystroke is instant.
        Never raises — the UI just shows no options on failure."""
        q = re.sub(r"\s+", " ", (query or "").strip())
        if len(q) < 2:
            return []
        now = time.time()
        hit = self._suggest_cache.get(q.lower())
        if hit is not None and now - hit[0] < 300:
            return hit[1]
        task = self._suggest_tasks.get(q.lower())
        if task is None or task.done():
            task = asyncio.ensure_future(self._suggest_build(q, limit))
            self._suggest_tasks[q.lower()] = task
            if len(self._suggest_tasks) > 80:
                for key in [k for k, t in self._suggest_tasks.items() if t.done()][:40]:
                    self._suggest_tasks.pop(key, None)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=2.4)
        except asyncio.TimeoutError:
            return []
        except Exception:  # noqa: BLE001
            return []

    async def _suggest_build(self, q: str, limit: int) -> list[tuple[str, str]]:
        def _fmt(d) -> str:
            try:
                d = int(d)
                return f"{d // 60}:{d % 60:02d}"
            except (TypeError, ValueError):
                return ""

        out: list[tuple[str, str]] = []
        entries: list[dict] = []
        if not self.yt_blocked():
            try:
                info = await self._flat_search(f"ytsearch{max(limit, 6)}:{q}")
                entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
            except Exception:  # noqa: BLE001
                entries = []
        for e in entries[:limit]:
            title = (e.get("title") or "").strip()
            url = (
                e.get("webpage_url")
                or e.get("url")
                or (f"https://www.youtube.com/watch?v={e.get('id')}" if e.get("id") else "")
            )
            if not title or not url:
                continue
            dur = _fmt(e.get("duration"))
            out.append((f"{title[:80]} · {dur}" if dur else title[:90], url))
        if not out:  # YouTube empty/walled — SoundCloud rescue for the picker
            try:
                cands = await self._sc_candidates(q)
            except Exception:  # noqa: BLE001
                cands = []
            for track in cands[:limit]:
                dur = _fmt(track.duration)
                label = f"{track.title[:80]} · {dur}" if dur else track.title[:90]
                out.append((label, track.webpage_url or track.source_url))
        self._suggest_cache[q.lower()] = (time.time(), out)
        if len(self._suggest_cache) > 300:
            for key in list(self._suggest_cache.keys())[:100]:
                self._suggest_cache.pop(key, None)
        return out

    async def sc_fallback(self, track: "Track") -> Optional["Track"]:
        """SoundCloud rescue for one specific track that YouTube refused."""
        query = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", track.title or "")
        query = re.sub(r"\s+", " ", query).strip(" -–—|")
        if track.uploader and track.uploader.lower() not in query.lower():
            query = f"{query} {track.uploader}".strip()
        return await self.sc_search(
            query,
            target_duration=track.duration,
            requester_id=track.requester_id,
            requester_name=track.requester_name,
        )

    # ---------------- download fallback ----------------

    def _download_sync(self, track: "Track") -> Path:
        opts = self._opts(
            client=self.working_client,
            extra={
                "skip_download": False,
                "outtmpl": str(config.CACHE_DIR / "dl_%(id)s.%(ext)s"),
            },
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track.source_url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            matches = sorted(
                config.CACHE_DIR.glob(f"dl_{info.get('id')}.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                path = matches[0]
        if not path.exists():
            raise StreamError("download produced no file")
        return path

    async def download(self, track: "Track") -> Path:
        if self._is_youtube_target(track.source_url) and self.yt_blocked():
            raise StreamError("youtube skipped (bot-check)")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, functools.partial(self._download_sync, track))
        except ExtractError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise StreamError(_clean_error(str(exc))) from exc

    # ---------------- autoplay ----------------

    async def related(self, track: "Track", limit: int = 5, *, seen: Optional[set] = None) -> list["Track"]:
        """YouTube mix seeded by the given track, minus already-played videos."""
        if self.yt_blocked():
            return []
        seen = seen or set()
        vid = track.video_id
        if not vid:
            return []
        target = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
        try:
            info = await self._extract(target, flat=True, playlist=True, extra={"playlistend": 25})
        except Exception as exc:  # noqa: BLE001
            log.warning("autoplay mix extraction failed: %r", exc)
            return []
        out: list["Track"] = []
        for entry in info.get("entries") or []:
            eid = entry.get("id")
            if not eid or eid == vid or eid in seen or not entry.get("title"):
                continue
            t = Track.from_info(entry)
            t.requester_id = track.requester_id
            t.requester_name = track.requester_name or "Autoplay"
            out.append(t)
            if len(out) >= limit:
                break
        return out
