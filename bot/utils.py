"""Formatting helpers, embeds and small shared utilities."""
from __future__ import annotations

import math
import re
from typing import Optional

import discord

from . import config

# ------------------------------------------------------------------ text

def sanitize(text: str, limit: int = 80) -> str:
    """Make user/YouTube-provided text safe to drop inside markdown links."""
    if not text:
        return ""
    text = text.replace("[", "(").replace("]", ")")
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "LIVE"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def progress_bar(position: float, duration: Optional[float], length: int = 16) -> str:
    if not duration or duration <= 0:
        return "🔴 LIVE"
    position = max(0.0, min(position, duration))
    filled = int(length * position / duration)
    filled = max(0, min(length - 1, filled))
    return "▬" * filled + "🔘" + "▬" * (length - filled - 1)


_TS_HMS = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_timestamp(text: str) -> Optional[int]:
    """Parse '1:30', '1:02:03', '90', '1h2m3s', '+30', '-30' into seconds (signed)."""
    if not text:
        return None
    t = text.strip().lower().replace(" ", "")
    if not t:
        return None
    sign = -1 if t.startswith("-") else 1
    t = t.lstrip("+-")
    if not t:
        return None
    if re.search(r"[hms]", t):
        m = _TS_HMS.fullmatch(t)
        if not m or not any(m.groups()):
            return None
        h, mi, s = (int(x) if x else 0 for x in m.groups())
        return sign * (h * 3600 + mi * 60 + s)
    if ":" in t:
        parts = t.split(":")
        if len(parts) > 3:
            return None
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        secs = 0
        for n in nums:
            secs = secs * 60 + n
        return sign * secs
    if t.isdigit():
        return sign * int(t)
    return None


# ------------------------------------------------------------------ embeds

def embed(*, title=None, description=None, color=None, footer=True) -> discord.Embed:
    e = discord.Embed(
        title=title,
        description=description,
        color=color if color is not None else config.EMBED_COLOR,
    )
    if footer:
        e.set_footer(text=config.BOT_NAME)
    return e


def ok_embed(description: str, *, title=None) -> discord.Embed:
    return embed(title=title, description=f"✅ {description}", color=config.EMBED_COLOR_OK)


def error_embed(description: str, *, title=None) -> discord.Embed:
    return embed(title=title, description=f"❌ {description}", color=config.EMBED_COLOR_ERROR)


def track_embed(track, *, title="➕ Added to queue", position: Optional[int] = None) -> discord.Embed:
    e = embed(title=title)
    e.description = f"**[{sanitize(track.title, 90)}]({track.webpage_url})**"
    if track.duration:
        e.add_field(name="Duration", value=f"`{fmt_duration(track.duration)}`", inline=True)
    if position:
        e.add_field(name="Position", value=f"`#{position}`", inline=True)
    if track.requester_name:
        e.add_field(name="Requested by", value=sanitize(track.requester_name, 40), inline=True)
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    return e


def np_embed(
    track,
    *,
    position: float = 0.0,
    loop_mode: str = "off",
    volume: float = 1.0,
    filter_name: str = "off",
    queue_len: int = 0,
    autoplay: bool = False,
    paused: bool = False,
) -> discord.Embed:
    e = embed(title="⏸ Paused" if paused else "▶️ Now playing")
    line = f"**[{sanitize(track.title, 90)}]({track.webpage_url})**"
    if track.uploader:
        line += f"\n`{sanitize(track.uploader, 60)}`"
    if track.duration:
        line += f"\n\n{progress_bar(position, track.duration)}\n`{fmt_duration(position)} / {fmt_duration(track.duration)}`"
    else:
        line += f"\n\n🔴 **LIVE** · `{fmt_duration(position)}` elapsed"
    e.description = line
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    meta = f"🔊 `{int(round(volume * 100))}%` · 🔁 `{loop_mode}` · 🎛 `{filter_name}` · 📜 `{queue_len}` queued"
    if autoplay:
        meta += " · ✨ autoplay"
    e.add_field(name="Settings", value=meta, inline=False)
    if track.requester_name:
        e.add_field(name="Requested by", value=sanitize(track.requester_name, 40), inline=True)
    return e


def queue_embed(
    tracks: list,
    current,
    page: int,
    *,
    per_page: int = 10,
    loop_mode: str = "off",
    autoplay: bool = False,
    paused: bool = False,
) -> tuple[discord.Embed, int]:
    pages = max(1, math.ceil(len(tracks) / per_page))
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    e = embed(title=f"📜 Queue — page {page}/{pages}")
    head = ""
    if current is not None:
        head = f"**{'⏸' if paused else '▶️'} Now playing:** [{sanitize(current.title, 70)}]({current.webpage_url})\n\n"
    lines = []
    for i, t in enumerate(tracks[start : start + per_page], start=start + 1):
        entry = f"`{i:>2}.` [{sanitize(t.title, 55)}]({t.webpage_url}) · `{fmt_duration(t.duration)}`"
        if t.requester_name:
            entry += f" — {sanitize(t.requester_name, 25)}"
        lines.append(entry)
    e.description = head + ("\n".join(lines)[:4000] if lines else "*The queue is empty.*")
    total = sum(t.duration or 0 for t in tracks)
    e.set_footer(
        text=f"{config.BOT_NAME} · {len(tracks)} tracks · {fmt_duration(total)} total · loop: {loop_mode} · autoplay: {'on' if autoplay else 'off'}"
    )
    return e, pages


# ------------------------------------------------------------------ permissions

def is_dj(member: discord.Member, settings: dict) -> bool:
    try:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
    except AttributeError:
        return False
    role_id = settings.get("dj_role")
    if role_id:
        return any(r.id == int(role_id) for r in member.roles)
    return False


def has_dj_bypass(member: discord.Member, settings: dict, vc) -> bool:
    """DJ role holders, managers, or someone alone with the bot may control playback."""
    if is_dj(member, settings):
        return True
    if vc is not None and vc.channel is not None:
        humans = [m for m in vc.channel.members if not m.bot]
        if len(humans) <= 1:
            return True
    return False
