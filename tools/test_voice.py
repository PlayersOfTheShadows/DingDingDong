"""Live voice end-to-end test.

Logs in, joins an EMPTY voice channel (nobody gets disturbed), plays a real
stream for ~20s through the exact FFmpegOpusAudio setup the bot uses, checks
the voice client state, then disconnects and exits.

Run with:  .venv\\Scripts\\python.exe tools\\test_voice.py
(Stop any other bot instance first — one gateway session per token.)
"""
from __future__ import annotations

import asyncio
import shlex
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

from bot import config  # noqa: E402
from bot.ytdl import Resolver  # noqa: E402

PLAY_SECONDS = 20

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
resolver = Resolver()
state: dict = {}


@client.event
async def on_ready():
    print(f"logged in as {client.user} ({client.user.id})")
    try:
        print("guilds:", [(g.id, g.name) for g in client.guilds])
        guild = client.guilds[0] if client.guilds else None
        if guild is None:
            print("no guilds — invite the bot first")
            state["ok"] = False
            return
        print("voice channels:", [(c.name, len([m for m in c.members if not m.bot])) for c in guild.voice_channels])
        target = next(
            (ch for ch in guild.voice_channels if not [m for m in ch.members if not m.bot]),
            None,
        )
        if target is None:
            print("NO EMPTY VOICE CHANNEL — skipping the live voice test (test manually with /play)")
            state["ok"] = False
            return
        print(f"joining empty channel: {target.name}")
        vc = await target.connect(self_deaf=True, timeout=30)
        print(f"connected: {vc.is_connected()} | channel: {vc.channel.name}")

        _kind, tracks = await resolver.resolve("never gonna give you up rick astley")
        track = tracks[0]
        url, headers, _fresh = await resolver.get_stream(track)
        print("resolved:", track.title)

        before = ["-nostdin", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        before += ["-headers", blob]
        options = ["-vn", "-af", "volume=1.00"]
        src = discord.FFmpegOpusAudio(
            url,
            before_options=" ".join(shlex.quote(str(p)) for p in before),
            options=" ".join(shlex.quote(str(p)) for p in options),
            executable=config.FFMPEG_BIN,
        )
        errors: dict = {}

        def after(error, _errors=errors):
            _errors["error"] = error

        vc.play(src, after=after)
        await asyncio.sleep(3)
        print("is_playing after 3s:", vc.is_playing(), "| paused:", vc.is_paused())

        checks = []
        for i in range(PLAY_SECONDS // 5):
            await asyncio.sleep(5)
            row = (f"t+{3 + (i + 1) * 5}s", vc.is_playing(), vc.is_connected(), errors.get("error"))
            checks.append(row)
            print("check:", row)

        ok = all(row[1] and row[2] and row[3] is None for row in checks)
        try:
            vc.stop()
            await asyncio.sleep(1)
        except Exception:
            pass
        await vc.disconnect(force=True)
        print("disconnected")
        state["ok"] = ok
        print("LIVE VOICE TEST:", "PASS" if ok else "FAIL")
    except Exception:
        traceback.print_exc()
        state["ok"] = False
    finally:
        await resolver.close()
        await client.close()


async def main() -> int:
    if not config.TOKEN:
        print("no token in .env")
        return 1
    try:
        await client.start(config.TOKEN)
    except Exception as exc:  # noqa: BLE001
        print("login failed:", repr(exc))
        return 1
    return 0 if state.get("ok") else 1


sys.exit(asyncio.run(main()))
