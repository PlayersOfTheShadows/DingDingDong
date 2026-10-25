"""Configuration — loads .env, resolves paths and the ffmpeg binary."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _color(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw.replace("#", "").replace("0x", ""), 16)
    except ValueError:
        return default


# ---- identity ----
TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
CLIENT_ID = _int("CLIENT_ID", 0)
CLIENT_SECRET = (os.getenv("CLIENT_SECRET") or "").strip()
BOT_NAME = (os.getenv("BOT_NAME") or "Music Bot").strip()
DEFAULT_PREFIX = (os.getenv("DEFAULT_PREFIX") or "!").strip()

# ---- look & feel ----
EMBED_COLOR = _color("EMBED_COLOR", 0x8B5CF6)
EMBED_COLOR_ERROR = _color("EMBED_COLOR_ERROR", 0xEF4444)
EMBED_COLOR_OK = _color("EMBED_COLOR_OK", 0x22C55E)

# ---- limits ----
MAX_QUEUE = _int("MAX_QUEUE", 500)
MAX_PLAYLIST_TRACKS = _int("MAX_PLAYLIST_TRACKS", 500)
MAX_PLAYLISTS_PER_USER = _int("MAX_PLAYLISTS_PER_USER", 20)
PLAYLIST_URL_LIMIT = _int("PLAYLIST_URL_LIMIT", 100)
SEARCH_RESULTS = _int("SEARCH_RESULTS", 8)

# ---- idle behaviour ----
# Generous default: stay in the voice channel for 2 hours even without 24/7.
IDLE_LEAVE_SECONDS = _int("IDLE_LEAVE_SECONDS", 7200)
EMPTY_CHANNEL_SECONDS = _int("EMPTY_CHANNEL_SECONDS", 180)

# ---- YouTube extraction ----
# "" = cookie-less (client fallbacks + automatic SoundCloud rescue when YouTube
# bot-walls). Or a browser name / "auto" (=firefox) to ALSO read that browser's
# live YouTube session — no manual cookie export, nothing to refresh by hand.
YTDLP_COOKIES_FROM_BROWSER = (os.getenv("YTDLP_COOKIES_FROM_BROWSER") or "").strip().lower()
# Optional cookies.txt (Netscape format) for hosts with no browser to read
# from — the reliable fix for YouTube bot-walls on datacenter IPs. Upload the
# file into the project folder (e.g. cookies.txt next to main.py) and set
# YTDLP_COOKIE_FILE=cookies.txt (absolute paths work too). NEVER commit it.
YTDLP_COOKIE_FILE = (os.getenv("YTDLP_COOKIE_FILE") or "").strip()
if not YTDLP_COOKIE_FILE:
    # Auto-detect a cookies.txt dropped next to main.py — the cookie-sync
    # tooling uploads it here automatically, so zero configuration is needed.
    _auto_cookies = BASE_DIR / "cookies.txt"
    if _auto_cookies.exists():
        YTDLP_COOKIE_FILE = str(_auto_cookies)

# When YouTube bot-walls the host IP (it does, on datacenter ranges), don't
# re-check on every play — once confirmed, skip YouTube for this window and go
# straight to SoundCloud (no visible platform plumbing). Uploading a fresh
# cookies.txt resets it instantly, and a restart clears it too.
YOUTUBE_BREAK_SECONDS = _int("YOUTUBE_BREAK_SECONDS", 43200)

# ---- AI (Groq primary, Google Gemini fallback) ----
# Both providers are tried in order; whichever answers first wins. If the
# first hits a quota/error the next takes over automatically.
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
GOOGLE_AI_API_KEY = (os.getenv("GOOGLE_AI_API_KEY") or "").strip()
AI_GROQ_MODEL = (os.getenv("AI_GROQ_MODEL") or "openai/gpt-oss-120b").strip()
AI_GOOGLE_MODEL = (os.getenv("AI_GOOGLE_MODEL") or "gemini-3.8-flash").strip()

# ---- paths ----
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
YTDLP_CACHE_DIR = CACHE_DIR / "ytdlp"
for _d in (DATA_DIR, CACHE_DIR, YTDLP_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _find_ffmpeg() -> str:
    override = (os.getenv("FFMPEG_PATH") or "").strip()
    if override:
        return override
    local = BASE_DIR / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG_BIN = _find_ffmpeg()
