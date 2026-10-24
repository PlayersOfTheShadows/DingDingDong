"""Lyrics lookup via lrclib.net (free, no API key)."""
from __future__ import annotations

import logging
import re
from typing import Optional

import aiohttp

from . import config

log = logging.getLogger("bot.lyrics")

LRCLIB_URL = "https://lrclib.net/api/search"

_NOISE_RE = re.compile(
    r"[\(\[][^)\]]*(official|video|audio|lyric|hd|4k|mv|m/v|visuali[sz]er|remaster|live|performance|version|clip)[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    title = _NOISE_RE.sub(" ", title or "")
    title = re.sub(r"\s+", " ", title).strip(" -–—|")
    return title


def build_query(track) -> str:
    title = clean_title(track.title)
    parts = [p for p in (title, track.uploader or "") if p]
    return " ".join(parts[:2]).strip() or track.title


async def fetch_lyrics(query: str) -> Optional[dict]:
    headers = {"User-Agent": f"{config.BOT_NAME}/1.0 (personal Discord bot)"}
    try:
        async with aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(LRCLIB_URL, params={"q": query}) as resp:
                if resp.status != 200:
                    return None
                results = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("lyrics lookup failed: %r", exc)
        return None
    if not isinstance(results, list) or not results:
        return None
    best = next((r for r in results if r.get("plainLyrics")), None) or results[0]
    return {
        "title": best.get("trackName") or query,
        "artist": best.get("artistName") or "",
        "album": best.get("albumName") or "",
        "plain": best.get("plainLyrics") or "",
        "instrumental": bool(best.get("instrumental")),
    }
