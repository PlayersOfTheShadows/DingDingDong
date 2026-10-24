"""Diagnose YouTube extraction: per-client matrix + cookies-from-browser +
SoundCloud fallback check. Prints exact errors so the bot can be configured
for whatever actually works on this machine/network right now.

Run with:  .venv\\Scripts\\python.exe tools\\test_youtube.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yt_dlp  # noqa: E402

from bot import config  # noqa: E402

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
CLIENTS = ["", "tv", "web_safari", "android_vr", "web_embedded", "mweb", "tv_embedded", "ios", "android"]


def opts(client: str = "", cookies: str = "") -> dict:
    o: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "bestaudio[acodec=opus]/bestaudio[acodec!=none]/bestaudio/best",
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 1,
        "cachedir": str(config.YTDLP_CACHE_DIR),
    }
    if client:
        o["extractor_args"] = {"youtube": {"player_client": [client]}}
    if cookies:
        o["cookiesfrombrowser"] = (cookies,)
    return o


def short(err: Exception) -> str:
    return " ".join(str(err).split())[:220]


print(f"yt-dlp {yt_dlp.version.__version__}")
print("\n== per-client (no cookies) ==")
for c in CLIENTS:
    t0 = time.time()
    try:
        with yt_dlp.YoutubeDL(opts(c)) as ydl:
            info = ydl.extract_info(VIDEO, download=False)
        print(f"[{c or 'default':<12}] OK   stream={'yes' if info.get('url') else 'NO'}  ({time.time() - t0:.1f}s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[{c or 'default':<12}] FAIL ({time.time() - t0:.1f}s): {short(exc)}")

print("\n== cookies-from-browser (first that works) ==")
for browser in ("edge", "chrome", "firefox", "brave", "opera"):
    try:
        with yt_dlp.YoutubeDL(opts("", browser)) as ydl:
            info = ydl.extract_info(VIDEO, download=False)
        print(f"[cookies:{browser:<7}] OK   stream={'yes' if info.get('url') else 'NO'}")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"[cookies:{browser:<7}] FAIL: {short(exc)}")

print("\n== SoundCloud search fallback ==")
try:
    with yt_dlp.YoutubeDL(
        {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "format": "bestaudio/best"}
    ) as ydl:
        info = ydl.extract_info("scsearch1:never gonna give you up", download=False)
    entry = info["entries"][0] if isinstance(info, dict) and info.get("entries") else info
    page = entry.get("webpage_url") or entry.get("url")
    with yt_dlp.YoutubeDL(
        {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "format": "bestaudio/best"}
    ) as ydl:
        full = ydl.extract_info(page, download=False)
    print(f"sc OK: {full.get('title')} | stream={'yes' if full.get('url') else 'NO'} | dur={full.get('duration')}")
except Exception as exc:  # noqa: BLE001
    print(f"sc FAIL: {short(exc)}")
