"""Tiny JSON persistence: per-guild settings, per-user playlists, play history."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from . import config

DEFAULT_SETTINGS: dict[str, Any] = {
    "prefix": None,               # None -> config.DEFAULT_PREFIX
    "dj_role": None,              # role id (int) or None
    "controller_channel": None,   # channel id where typing a song name plays it
    "stay247": False,             # keep the bot in voice forever
    "autorejoin": False,          # rejoin after being kicked (with 24/7)
    "announce": True,             # post "now playing" messages
    "songstatus": True,           # set voice-channel status to the current song
    "default_volume": 100,        # volume applied to new playback sessions
    "ai_channel": None,           # channel id where the AI talks & controls music
}


class JsonStore:
    def __init__(self, name: str):
        self.path = config.DATA_DIR / name
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
            tmp.replace(self.path)

    def bucket(self, scope: Any) -> dict:
        return self._data.setdefault(str(scope), {})

    def all(self) -> dict:
        return self._data

    def get(self, scope: Any, key: str, default: Any = None) -> Any:
        return self._data.get(str(scope), {}).get(key, default)

    def set(self, scope: Any, key: str, value: Any) -> None:
        self.bucket(scope)[key] = value
        self.save()


SETTINGS = JsonStore("settings.json")
PLAYLISTS = JsonStore("playlists.json")
HISTORY = JsonStore("history.json")


# ------------------------------------------------------------------ settings

def guild_settings(guild_id: int) -> dict:
    data = dict(DEFAULT_SETTINGS)
    data.update(SETTINGS.bucket(guild_id))
    return data


def set_setting(guild_id: int, key: str, value: Any) -> None:
    SETTINGS.set(guild_id, key, value)


def guild_prefix(guild_id: int) -> str:
    return guild_settings(guild_id).get("prefix") or config.DEFAULT_PREFIX


# ------------------------------------------------------------------ history

def add_history(guild_id: int, track: dict) -> None:
    entries = HISTORY.bucket(guild_id).setdefault("entries", [])
    entries.append({**track, "played_at": int(time.time())})
    del entries[:-100]
    HISTORY.save()


def get_history(guild_id: int, limit: int = 15) -> list[dict]:
    entries = HISTORY.get(guild_id, "entries", [])
    return list(reversed(entries[-limit:]))


# ------------------------------------------------------------------ playlists

def user_playlists(user_id: int) -> dict:
    """{name_key: {"name": str, "tracks": [trackdict], "public": bool, "created": int}}"""
    return PLAYLISTS.bucket(user_id)


def playlist_key(name: str) -> str:
    return name.strip().lower()[:80]


def get_playlist(user_id: int, name: str) -> Optional[dict]:
    return user_playlists(user_id).get(playlist_key(name))


def save_playlists() -> None:
    PLAYLISTS.save()
