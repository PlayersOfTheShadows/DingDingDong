"""Personal playlists (/playlist …). Stored per-user in data/playlists.json."""
from __future__ import annotations

import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..player import UserError
from ..store import (
    get_playlist,
    playlist_key,
    save_playlists,
    user_playlists,
)
from ..utils import embed, fmt_duration, ok_embed, sanitize
from ..ytdl import ExtractError, Track, UnsupportedPlatform


class Playlists(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ helpers

    def _require(self, ctx, name: str) -> dict:
        pl = get_playlist(ctx.author.id, name)
        if pl is None:
            raise UserError(f"You don't have a playlist named `{sanitize(name, 40)}`.")
        return pl

    @staticmethod
    def _append(pl: dict, tracks: list[Track]) -> int:
        room = max(0, config.MAX_PLAYLIST_TRACKS - len(pl["tracks"]))
        added = tracks[:room]
        pl["tracks"].extend(t.to_dict() for t in added)
        return len(added)

    async def _join_and_get_player(self, ctx):
        member = ctx.author
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            raise UserError("Join a voice channel first.")
        player = self.bot.get_player(ctx.guild)
        player._notice_channel = ctx.channel
        if player.vc is None or not player.vc.is_connected():
            await player.ensure_voice(member.voice.channel)
        elif player.vc.channel is not None and player.vc.channel.id != member.voice.channel.id:
            busy = [m for m in player.vc.channel.members if not m.bot]
            if busy or player.current is not None:
                raise UserError(f"I'm already playing in {player.vc.channel.mention}.")
            await player.ensure_voice(member.voice.channel)
        return player

    # ------------------------------------------------------------------ group

    @commands.hybrid_group(
        name="playlist",
        aliases=["pl"],
        description="Personal playlists.",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def playlist(self, ctx: commands.Context):
        await ctx.send(
            embed=embed(
                title="📃 Playlists",
                description=(
                    "`create <name>` · `delete <name>` · `list`\n"
                    "`add <name> <song or URL>` · `addtrack <name>` · `addqueue <name>`\n"
                    "`show <name> [page]` · `play <name>`\n"
                    "`share <name>` · `lock <name>` · `public`"
                ),
            )
        )

    @playlist.command(name="create", description="Create a playlist.")
    @app_commands.describe(name="Playlist name (one word)")
    async def pl_create(self, ctx: commands.Context, *, name: str):
        key = playlist_key(name)
        if not key:
            raise UserError("Playlist name can't be empty.")
        owned = user_playlists(ctx.author.id)
        if key in owned:
            raise UserError("You already have a playlist with that name.")
        if len(owned) >= config.MAX_PLAYLISTS_PER_USER:
            raise UserError(f"Playlist limit reached ({config.MAX_PLAYLISTS_PER_USER}).")
        owned[key] = {"name": name.strip()[:80], "tracks": [], "public": False, "created": int(time.time())}
        save_playlists()
        await ctx.send(embed=ok_embed(f"Created playlist **{sanitize(name, 60)}**."))

    @playlist.command(name="delete", description="Delete a playlist.")
    @app_commands.describe(name="Playlist name")
    async def pl_delete(self, ctx: commands.Context, *, name: str):
        key = playlist_key(name)
        owned = user_playlists(ctx.author.id)
        if key not in owned:
            raise UserError(f"You don't have a playlist named `{sanitize(name, 40)}`.")
        removed = owned.pop(key)
        save_playlists()
        await ctx.send(embed=ok_embed(f"Deleted playlist **{sanitize(removed['name'], 60)}** ({len(removed['tracks'])} tracks)."))

    @playlist.command(name="list", description="List your playlists.")
    async def pl_list(self, ctx: commands.Context):
        owned = user_playlists(ctx.author.id)
        if not owned:
            raise UserError("You don't have any playlists yet — create one with `/playlist create`.")
        lines = [
            f"**{sanitize(pl['name'], 60)}** — {len(pl['tracks'])} tracks{' · 🌐 public' if pl.get('public') else ''}"
            for pl in owned.values()
        ]
        e = embed(title="📃 Your playlists", description="\n".join(lines)[:4000])
        e.set_footer(text=f"{len(owned)}/{config.MAX_PLAYLISTS_PER_USER} playlists used")
        await ctx.send(embed=e)

    @playlist.command(name="add", description="Add a song or playlist URL to one of your playlists.")
    @app_commands.describe(name="Playlist name", query="Song name or URL")
    async def pl_add(self, ctx: commands.Context, name: str, *, query: str):
        pl = self._require(ctx, name)
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer()
        try:
            kind, tracks = await self.bot.resolver.resolve(query, requester=None)
        except UnsupportedPlatform as exc:
            raise UserError(str(exc)) from exc
        except ExtractError as exc:
            raise UserError(f"Couldn't find `{sanitize(query, 60)}` — {exc}") from exc
        if kind == "track":
            tracks = tracks[:1]
        added = self._append(pl, tracks)
        save_playlists()
        await ctx.send(embed=ok_embed(f"Added **{added}** track(s) to **{sanitize(pl['name'], 60)}**."))

    @playlist.command(name="addtrack", description="Add the currently playing track to a playlist.")
    @app_commands.describe(name="Playlist name")
    async def pl_addtrack(self, ctx: commands.Context, *, name: str):
        pl = self._require(ctx, name)
        player = self.bot.get_player(ctx.guild)
        if player.current is None:
            raise UserError("Nothing is playing.")
        added = self._append(pl, [player.current])
        save_playlists()
        await ctx.send(embed=ok_embed(f"Added **{sanitize(player.current.title)}** to **{sanitize(pl['name'], 60)}**."))

    @playlist.command(name="addqueue", description="Add the whole queue to a playlist.")
    @app_commands.describe(name="Playlist name")
    async def pl_addqueue(self, ctx: commands.Context, *, name: str):
        pl = self._require(ctx, name)
        player = self.bot.get_player(ctx.guild)
        if not player.queue:
            raise UserError("The queue is empty.")
        added = self._append(pl, list(player.queue))
        save_playlists()
        await ctx.send(embed=ok_embed(f"Added **{added}** track(s) to **{sanitize(pl['name'], 60)}**."))

    @playlist.command(name="show", description="Show the tracks in a playlist.")
    @app_commands.describe(name="Playlist name", page="Page number")
    async def pl_show(self, ctx: commands.Context, name: str, page: int = 1):
        pl = self._require(ctx, name)
        tracks = pl["tracks"]
        if not tracks:
            raise UserError("That playlist is empty.")
        per_page = 15
        pages = max(1, (len(tracks) + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        lines = []
        for i, d in enumerate(tracks[start : start + per_page], start=start + 1):
            lines.append(f"`{i:>3}.` [{sanitize(d.get('title') or '?', 55)}]({d.get('webpage_url') or 'https://discord.com'})")
        total = sum(d.get("duration") or 0 for d in tracks)
        e = embed(title=f"📃 {sanitize(pl['name'], 60)} — page {page}/{pages}", description="\n".join(lines)[:4000])
        e.set_footer(text=f"{len(tracks)} tracks · {fmt_duration(total)} total")
        await ctx.send(embed=e)

    @playlist.command(name="play", description="Queue all tracks from a playlist.")
    @app_commands.describe(name="Playlist name")
    async def pl_play(self, ctx: commands.Context, *, name: str):
        pl = self._require(ctx, name)
        if not pl["tracks"]:
            raise UserError("That playlist is empty.")
        player = await self._join_and_get_player(ctx)
        member = ctx.author
        requester_name = getattr(member, "display_name", None) or member.name
        tracks = [Track.from_dict(d) for d in pl["tracks"]]
        for t in tracks:
            t.requester_id = member.id
            t.requester_name = requester_name
        added = player.extend_queue(tracks)
        await player.start()
        e = ok_embed(f"▶️ Queued **{added}** track(s) from **{sanitize(pl['name'], 60)}**.")
        if added < len(tracks):
            e.description += f"\n-# {len(tracks) - added} track(s) didn't fit in the queue."
        await ctx.send(embed=e)

    @playlist.command(name="share", description="Make a playlist public.")
    @app_commands.describe(name="Playlist name")
    async def pl_share(self, ctx: commands.Context, *, name: str):
        pl = self._require(ctx, name)
        pl["public"] = True
        save_playlists()
        await ctx.send(embed=ok_embed(f"🌐 **{sanitize(pl['name'], 60)}** is now public."))

    @playlist.command(name="lock", description="Make a playlist private again.")
    @app_commands.describe(name="Playlist name")
    async def pl_lock(self, ctx: commands.Context, *, name: str):
        pl = self._require(ctx, name)
        pl["public"] = False
        save_playlists()
        await ctx.send(embed=ok_embed(f"🔒 **{sanitize(pl['name'], 60)}** is now private."))

    @playlist.command(name="public", description="Browse public playlists.")
    async def pl_public(self, ctx: commands.Context):
        from ..store import PLAYLISTS

        lines = []
        for uid, owned in PLAYLISTS.all().items():
            for pl in owned.values():
                if pl.get("public"):
                    lines.append(f"**{sanitize(pl['name'], 60)}** — {len(pl['tracks'])} tracks · <@{uid}>")
        if not lines:
            raise UserError("No public playlists yet.")
        await ctx.send(embed=embed(title="🌐 Public playlists", description="\n".join(lines)[:4000]))


async def setup(bot):
    await bot.add_cog(Playlists(bot))
