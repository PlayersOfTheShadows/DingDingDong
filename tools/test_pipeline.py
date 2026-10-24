"""Diagnostic: validates the yt-dlp -> ffmpeg streaming pipeline used by the bot.

Run with:  .venv\\Scripts\\python.exe tools\\test_pipeline.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import yt_dlp

BASE = Path(__file__).resolve().parent.parent
FFMPEG = str(BASE / "bin" / "ffmpeg.exe")

QUERY = "ytsearch1:never gonna give you up rick astley"

# Client fallback order — mirrors bot/ytdl.py
CLIENTS = ["", "tv", "web_safari", "android_vr", "web_embedded", "mweb"]

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "format": "bestaudio[acodec=opus]/bestaudio[acodec!=none]/bestaudio/best",
    "skip_download": True,
    "default_search": "ytsearch",
    "socket_timeout": 20,
    "retries": 2,
    "cachedir": str(BASE / "data" / "cache" / "ytdlp"),
}

HEADER_LINE = "\r\n"


def build_headers_arg(headers: dict) -> str:
    return HEADER_LINE.join(f"{k}: {v}" for k, v in headers.items()) + HEADER_LINE


async def verify_stream(url: str, headers: dict) -> tuple[bool, str]:
    """HTTP-verify the stream URL returns 2xx/206 before handing it to ffmpeg."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)
            ) as r:
                chunk = await r.content.read(65536)
                ok = r.status in (200, 206) and len(chunk) > 0
                return ok, f"HTTP {r.status}, {len(chunk)} bytes, ct={r.headers.get('Content-Type')}"
    except Exception as e:  # noqa: BLE001
        return False, f"request failed: {e!r}"


def extract(client: str):
    opts = dict(BASE_OPTS)
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(QUERY, download=False)
    if info and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("search returned no entries")
        info = entries[0]
    return info


async def main() -> int:
    print(f"yt-dlp {yt_dlp.version.__version__} | ffmpeg probe: ", end="")
    p = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, timeout=30)
    first = (p.stdout or p.stderr).splitlines()[0] if (p.stdout or p.stderr) else "?"
    print(first)
    print()

    working_client = None
    info = None
    for client in CLIENTS:
        label = client or "(default)"
        t0 = time.time()
        try:
            info = extract(client)
            url = info.get("url")
            if not url:
                print(f"client {label:<12} -> no direct url, keys={sorted(info.keys())[:8]}")
                continue
            headers = info.get("http_headers") or {}
            ok, detail = await verify_stream(url, headers)
            dt = time.time() - t0
            print(f"client {label:<12} -> {detail} ({dt:.1f}s)")
            if ok:
                working_client = client
                break
        except Exception as e:  # noqa: BLE001
            print(f"client {label:<12} -> FAILED: {type(e).__name__}: {str(e)[:160]}")
    print()

    if not info or working_client is None:
        print("!! NO WORKING STREAM EXTRACTION — investigate before proceeding")
        return 1

    print(f"WORKING CLIENT: {working_client or '(default)'}")
    print(f"title   : {info.get('title')}")
    print(f"video id: {info.get('id')}")
    print(f"duration: {info.get('duration')}s | live: {info.get('is_live')}")
    print(f"thumb   : {bool(info.get('thumbnail'))}")
    print()

    url = info["url"]
    headers = info.get("http_headers") or {}

    # ---- ffmpeg decode test (exact argv style the bot uses) ----
    cmd = [
        FFMPEG,
        "-nostdin",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-headers", build_headers_arg(headers),
        "-i", url,
        "-t", "4",
        "-vn",
        "-f", "null",
        "-",
    ]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, timeout=90)
    tail = p.stderr.decode(errors="replace").strip().splitlines()[-4:]
    print(f"ffmpeg decode exit={p.returncode} in {time.time()-t0:.1f}s (expect 0)")
    for line in tail:
        print("   ", line[:200])
    print()

    # ---- download fallback test ----
    print("testing download fallback ...")
    out_tmpl = str(BASE / "data" / "cache" / "testpipe.%(ext)s")
    dl_opts = dict(BASE_OPTS)
    dl_opts.update({"outtmpl": out_tmpl, "skip_download": False})
    if working_client:
        dl_opts["extractor_args"] = {"youtube": {"player_client": [working_client]}}
    t0 = time.time()
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        dl_info = ydl.extract_info(QUERY, download=True)
    path = Path(ydl.prepare_filename(dl_info))
    if not path.exists():
        cands = list((BASE / "data" / "cache").glob("testpipe.*"))
        path = cands[0] if cands else path
    size = path.stat().st_size if path.exists() else 0
    print(f"download -> {path.name} ({size/1024:.0f} KiB) in {time.time()-t0:.1f}s")
    if path.exists():
        p = subprocess.run(
            [FFMPEG, "-nostdin", "-i", str(path), "-t", "4", "-vn", "-f", "null", "-"],
            capture_output=True, timeout=90,
        )
        print(f"ffmpeg decode of downloaded file exit={p.returncode} (expect 0)")
        path.unlink(missing_ok=True)

    print()
    print("PIPELINE OK" if p.returncode == 0 else "PIPELINE PROBLEM")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
