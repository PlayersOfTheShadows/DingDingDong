"""Check command registration server-side (pure REST — no gateway session).

Shows exactly what Discord has registered for this application: global
commands and guild commands for every guild the bot is in. Use this when
slash commands don't show up in the client.

Run with:  .venv\\Scripts\\python.exe tools\\check_commands.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402

API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {config.TOKEN}",
    "User-Agent": "DingDingDong-CommandCheck/1.0",
}


def get(path: str):
    req = urllib.request.Request(API + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except Exception:
            body = exc.reason
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, repr(exc)


def main() -> int:
    status, guilds = get("/users/@me/guilds")
    print(f"== bot guilds: HTTP {status} ==")
    if not isinstance(guilds, list):
        print("  error:", guilds)
        return 1
    for g in guilds:
        print(f"  - {g['name']} ({g['id']})")

    status, cmds = get(f"/applications/{config.CLIENT_ID}/commands")
    count = len(cmds) if isinstance(cmds, list) else cmds
    print(f"\n== global commands: HTTP {status}, count={count} ==")
    if isinstance(cmds, list) and cmds:
        print("  " + ", ".join(sorted(c['name'] for c in cmds)))

    for g in guilds:
        status, cmds = get(f"/applications/{config.CLIENT_ID}/guilds/{g['id']}/commands")
        if isinstance(cmds, list):
            names = sorted(c["name"] for c in cmds)
            print(f"\n== guild commands for '{g['name']}': HTTP {status}, count={len(names)} ==")
            print("  " + ", ".join(names))
            if not names:
                print("  (none registered!)")
        else:
            print(f"\n== guild commands for '{g['name']}': HTTP {status} -> {cmds} ==")
            if status in (401, 403, 404):
                print("  This can mean the bot was invited WITHOUT the applications.commands scope.")

    try:
        import discord

        url = discord.utils.oauth_url(
            config.CLIENT_ID,
            scopes=("bot", "applications.commands"),
        )
        print("\n== correct re-invite URL (includes applications.commands scope) ==")
        print(f"  {url}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
