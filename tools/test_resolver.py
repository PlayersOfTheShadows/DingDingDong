"""End-to-end check of the resolver ladder: normal play, ffmpeg decode, and
the SoundCloud rescue path. This is the verification that a user's /play will
actually produce audible audio on this machine, now.

Run with:  .venv\\Scripts\\python.exe tools\\test_resolver.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.ytdl import Resolver  # noqa: E402


def ffmpeg_decode(url: str, headers: dict) -> tuple[int, str]:
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = [str(config.FFMPEG_BIN), "-hide_banner", "-loglevel", "error"]
    if header_blob:
        cmd += ["-headers", header_blob]
    cmd += ["-i", url, "-t", "3", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    return proc.returncode, proc.stderr.decode(errors="replace")[:200]


async def main() -> int:
    resolver = Resolver()
    ok = True

    # 1) normal YouTube resolve + stream
    kind, tracks = await resolver.resolve("never gonna give you up")
    track = tracks[0]
    print(f"resolve    : kind={kind} {track.title!r}")
    url, headers, _fresh = await resolver.get_stream(track)
    print(f"get_stream : ok, client={resolver.working_client!r}, url={url[:70]}...")
    code, err = ffmpeg_decode(url, headers)
    print(f"ffmpeg yt  : exit={code} {err}")
    ok &= code == 0

    # 2) SoundCloud rescue path (what runs when YouTube bot-walls)
    fallback = await resolver.sc_fallback(track)
    if fallback is None:
        print("sc_fallback: NONE (no results) — FAIL")
        ok = False
    else:
        print(f"sc_fallback: {fallback.title!r} dur={fallback.duration}")
        url2, headers2, _ = await resolver.get_stream(fallback)
        code2, err2 = ffmpeg_decode(url2, headers2)
        print(f"ffmpeg sc  : exit={code2} {err2}")
        ok &= code2 == 0

    print("RESULT     :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
