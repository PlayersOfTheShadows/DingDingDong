"""Music commands (hybrid: work as /slash and !prefix)."""
from __future__ import annotations

import logging
import random
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..lyrics import build_query, fetch_lyrics
from ..player import FILTERS, GuildPlayer, UserError
from ..store import get_history, guild_settings, set_setting
from ..utils import (
    embed,
    fmt_duration,
    has_dj_bypass,
    np_embed,
    ok_embed,
    parse_timestamp,
    queue_embed,
    sanitize,
    track_embed,
)
from ..views import NowPlayingView, QueueView, attempt_skip
from ..ytdl import ExtractError, Track, UnsupportedPlatform

log = logging.getLogger("bot.music")

RADIO_STATIONS: list[tuple[str, str]] = [
    ("SomaFM Groove Salad (ambient/downtempo)", "https://ice2.somafm.com/groovesalad-128-mp3"),
    ("SomaFM Drone Zone (atmospheric)", "https://ice2.somafm.com/dronezone-128-mp3"),
    ("SomaFM Beat Blender (deep house)", "https://ice2.somafm.com/beatblender-128-mp3"),
    ("SomaFM Lush (chill vocals)", "https://ice2.somafm.com/lush-128-mp3"),
    ("SomaFM Underground 80s", "https://ice2.somafm.com/u80s-128-mp3"),
    ("SomaFM Indie Pop Rocks", "https://ice2.somafm.com/indiepop-128-mp3"),
    ("SomaFM Metal Detector", "https://ice2.somafm.com/metal-128-mp3"),
    ("SomaFM DEF CON Radio", "https://ice2.somafm.com/defcon-128-mp3"),
    ("Radio Paradise Main Mix", "https://stream.radioparadise.com/mp3-192"),
    ("Radio Paradise Mellow Mix", "https://stream.radioparadise.com/mellow-192"),
]


class SearchView(discord.ui.View):
    """Select one of the top search results to queue."""

    def __init__(self, cog: "Music", results: list[Track], requester: discord.Member):
        super().__init__(timeout=120)
        self.cog = cog
        self.results = results
        self.requester = requester
        options = [
            discord.SelectOption(
                label=sanitize(t.title, 90),
                value=str(i),
                description=(f"Duration: {fmt_duration(t.duration)}" if t.duration else None),
            )
            for i, t in enumerate(results[:5])
        ]
        select = discord.ui.Select(placeholder="Choose a track to queue…", options=options)
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        index = int(self._select.values[0])
        track = self.results[index]
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return
        track.requester_id = member.id
        track.requester_name = getattr(member, "display_name", None) or member.name
        player = self.cog.bot.get_player(interaction.guild)
        player._notice_channel = interaction.channel
        try:
            if player.vc is None or not player.vc.is_connected():
                await player.ensure_voice(member.voice.channel)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        pos = player.enqueue(track)
        await player.start()
        await interaction.response.send_message(
            embed=track_embed(track, title="➕ Added to queue", position=pos), ephemeral=True
        )


class Music(commands.Cog):
    """Playback commands."""

    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ helpers

    def _player(self, ctx) -> GuildPlayer:
        player = self.bot.get_player(ctx.guild)
        player._notice_channel = ctx.channel
        return player

    def _gate(self, ctx) -> GuildPlayer:
        """Player + DJ-role check (only enforced when a DJ role is configured)."""
        player = self._player(ctx)
        settings = guild_settings(ctx.guild.id)
        if settings.get("dj_role"):
            member = ctx.author
            if not (isinstance(member, discord.Member) and has_dj_bypass(member, settings, player.vc)):
                raise UserError("You need the DJ role to control playback here.")
        return player

    async def _require_voice(self, ctx) -> discord.VoiceChannel:
        member = ctx.author
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            raise UserError("Join a voice channel first, then run this again.")
        return member.voice.channel

    async def _ensure_player_voice(self, ctx, player: GuildPlayer) -> discord.VoiceChannel:
        channel = await self._require_voice(ctx)
        vc = player.vc
        if vc is not None and vc.is_connected() and vc.channel is not None:
            if vc.channel.id != channel.id:
                busy = [m for m in vc.channel.members if not m.bot]
                if busy or player.current is not None:
                    raise UserError(
                        f"I'm already playing in {vc.channel.mention}. Join me there, or use `/disconnect` first."
                    )
                await player.ensure_voice(channel)
            return channel
        await player.ensure_voice(channel)
        return channel

    @staticmethod
    async def _defer(ctx) -> None:
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer()

    # ------------------------------------------------------------------ play

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song by name or URL (adds to the queue while something plays).")
    @commands.guild_only()
    @app_commands.describe(query="Song name, YouTube URL or playlist URL", insert_first="Put this right after the current track")
    async def play(self, ctx: commands.Context, *, query: str, insert_first: bool = False):
        await self._do_play(ctx, query, insert_first=insert_first, then_skip=False)

    @commands.hybrid_command(name="playskip", aliases=["ps"], description="Queue a song and skip straight to it.")
    @commands.guild_only()
    @app_commands.describe(query="Song name, YouTube URL or playlist URL")
    async def playskip(self, ctx: commands.Context, *, query: str):
        await self._do_play(ctx, query, insert_first=True, then_skip=True)

    @commands.hybrid_command(name="playtop", aliases=["pt"], description="Add a song to the top of the queue.")
    @commands.guild_only()
    @app_commands.describe(query="Song name, YouTube URL or playlist URL")
    async def playtop(self, ctx: commands.Context, *, query: str):
        await self._do_play(ctx, query, insert_first=True, then_skip=False)

    @commands.hybrid_command(name="playfile", description="Play an uploaded audio file.")
    @commands.guild_only()
    @app_commands.describe(attachment="The audio file to play")
    async def playfile(self, ctx: commands.Context, attachment: Optional[discord.Attachment] = None):
        if attachment is None:
            raise UserError("Attach an audio file to the command (or with the message when using the prefix form).")
        await self._defer(ctx)
        player = self._player(ctx)
        await self._ensure_player_voice(ctx, player)
        track = Track(
            title=attachment.filename.rsplit(".", 1)[0].replace("_", " ")[:90],
            webpage_url=attachment.url,
            direct_stream=True,
            is_live=False,
            requester_id=ctx.author.id,
            requester_name=getattr(ctx.author, "display_name", None) or ctx.author.name,
        )
        pos = player.enqueue(track)
        await player.start()
        await ctx.send(embed=track_embed(track, title="➕ Added to queue", position=pos))

    async def _do_play(self, ctx, query: str, *, insert_first: bool, then_skip: bool):
        await self._defer(ctx)
        player = self._player(ctx)
        await self._ensure_player_voice(ctx, player)
        was_idle = player.current is None and not (player.is_playing or player.is_paused)
        try:
            kind, tracks = await self.bot.resolver.resolve(query, requester=ctx.author)
        except UnsupportedPlatform as exc:
            raise UserError(str(exc)) from exc
        except ExtractError as exc:
            raise UserError(f"Couldn't find anything for `{sanitize(query, 60)}` — {exc}") from exc
        if kind == "track":
            track = tracks[0]
            pos = player.enqueue(track, insert_first=insert_first or then_skip)
            await player.start()
            if then_skip:
                await player.skip()
            if then_skip or insert_first:
                title = "⏭ Playing next"
            elif was_idle:
                title = "▶️ Playing"
            else:
                title = "➕ Added to queue"
            await ctx.send(embed=track_embed(track, title=title, position=None if (was_idle or insert_first) else pos))
        else:
            added = player.extend_queue(tracks)
            await player.start()
            e = embed(title="📥 Playlist queued", description=f"Added **{added}** tracks to the queue.")
            if added < len(tracks):
                e.description += f"\n-# Queue limit reached — {len(tracks) - added} track(s) were dropped."
            await ctx.send(embed=e)

    @commands.hybrid_command(name="search", aliases=["yts"], description="Search YouTube and pick from the top results.")
    @commands.guild_only()
    @app_commands.describe(query="What to search for")
    async def search(self, ctx: commands.Context, *, query: str):
        await self._defer(ctx)
        player = self._player(ctx)
        await self._ensure_player_voice(ctx, player)
        try:
            results = await self.bot.resolver.search(query, limit=config.SEARCH_RESULTS)
        except ExtractError as exc:
            raise UserError(f"Search failed — {exc}") from exc
        if not results:
            raise UserError(f"No results for `{sanitize(query, 60)}`.")
        e = embed(
            title=f"🔎 Results for `{sanitize(query, 60)}`",
            description="\n".join(f"`{i}.` {sanitize(t.title, 70)}" for i, t in enumerate(results, 1)),
        )
        e.set_footer(text="Pick one from the menu below")
        await ctx.send(embed=e, view=SearchView(self, results, ctx.author))

    # ------------------------------------------------------------------ transport

    @commands.hybrid_command(name="pause", aliases=["pu", "break"], description="Pause playback.")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        player = self._player(ctx)
        if not player.pause():
            raise UserError("Nothing is playing.")
        await ctx.send(embed=ok_embed("⏸ Paused."))

    @commands.hybrid_command(name="resume", aliases=["r", "unpause"], description="Resume playback.")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        player = self._player(ctx)
        if not player.resume():
            raise UserError("Nothing is paused.")
        await ctx.send(embed=ok_embed("▶️ Resumed."))

    @commands.hybrid_command(name="replay", aliases=["rp"], description="Restart the current track.")
    @commands.guild_only()
    async def replay(self, ctx: commands.Context):
        player = self._gate(ctx)
        if player.current is None:
            raise UserError("Nothing is playing.")
        await player.seek(0)
        await ctx.send(embed=ok_embed("🔁 Replaying from the start."))

    @commands.hybrid_command(name="back", aliases=["b", "previous", "prev"], description="Play the previous track again.")
    @commands.guild_only()
    async def back(self, ctx: commands.Context):
        player = self._gate(ctx)
        prev = await player.play_previous()
        if prev is None:
            raise UserError("No previous track in this session.")
        await ctx.send(embed=track_embed(prev, title="⏮ Playing previous"))

    @commands.hybrid_command(name="panel", aliases=["controls"], description="Post a control panel with playback buttons.")
    @commands.guild_only()
    async def panel(self, ctx: commands.Context):
        player = self._player(ctx)
        e = embed(
            title="🎛 Control panel",
            description=(
                "Nothing is playing right now — `/play` something first.\n"
                "**Prev · Play/Pause · Skip · Stop · Disconnect · Volume · Loop · Shuffle · Autoplay · Queue**"
                if player.current is None
                else "**Prev · Play/Pause · Skip · Stop · Disconnect · Volume · Loop · Shuffle · Autoplay · Queue**"
            ),
        )
        await ctx.send(embed=e, view=NowPlayingView(player))

    @commands.hybrid_command(name="addprevious", aliases=["addback"], description="Put the previous track back into the queue.")
    @commands.guild_only()
    async def addprevious(self, ctx: commands.Context):
        player = self._gate(ctx)
        prev = player.previous_track()
        if prev is None:
            raise UserError("No previous track in this session.")
        pos = player.enqueue(prev, insert_first=True)
        await ctx.send(embed=track_embed(prev, title="➕ Previous track queued", position=pos))

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Skip the current track (vote-skip when 3+ listeners).")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        player = self._player(ctx)
        result = await attempt_skip(player, ctx.author, channel=ctx.channel)
        if result == "nothing":
            raise UserError("Nothing is playing.")
        if result == "vote":
            await ctx.send(embed=ok_embed("🗳️ Skip vote started — check the message above."))
        else:
            await ctx.send(embed=ok_embed("⏭ Skipping."))

    @commands.hybrid_command(name="skipto", aliases=["jump"], description="Jump to a track position in the queue.")
    @commands.guild_only()
    @app_commands.describe(position="Queue position to jump to (1 = next up)")
    async def skipto(self, ctx: commands.Context, position: int):
        player = self._gate(ctx)
        q = list(player.queue)
        if not q:
            raise UserError("The queue is empty.")
        if not (1 <= position <= len(q)):
            raise UserError(f"Position must be between 1 and {len(q)}.")
        for _ in range(position - 1):
            player.queue.popleft()
        target = player.queue[0]
        if player.is_playing or player.is_paused:
            await player.skip()
        else:
            await player.start()
        await ctx.send(embed=ok_embed(f"⏭ Jumping to **{sanitize(target.title)}**."))

    # ------------------------------------------------------------------ info

    @commands.hybrid_command(name="queue", aliases=["q", "list"], description="Show the queue.")
    @commands.guild_only()
    @app_commands.describe(page="Page number")
    async def queue(self, ctx: commands.Context, page: int = 1):
        player = self._player(ctx)
        e, pages = queue_embed(
            list(player.queue),
            player.current,
            page,
            loop_mode=player.loop_mode,
            autoplay=player.autoplay,
            paused=player.is_paused,
        )
        view = QueueView(player, max(1, min(page, pages)), pages) if pages > 1 else None
        await ctx.send(embed=e, view=view)

    @commands.hybrid_command(name="nowplaying", aliases=["np", "song"], description="Show what is playing right now.")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context):
        player = self._player(ctx)
        if player.current is None:
            raise UserError("Nothing is playing right now.")
        e = np_embed(
            player.current,
            position=player.position(),
            loop_mode=player.loop_mode,
            volume=player.volume,
            filter_name=player.filter,
            queue_len=len(player.queue),
            autoplay=player.autoplay,
            paused=player.is_paused,
        )
        await ctx.send(embed=e, view=NowPlayingView(player))

    @commands.hybrid_command(name="history", aliases=["hist"], description="Show recently played tracks.")
    @commands.guild_only()
    async def history(self, ctx: commands.Context):
        entries = get_history(ctx.guild.id, 15)
        if not entries:
            raise UserError("No play history yet.")
        lines = []
        for i, entry in enumerate(entries, 1):
            who = f" — {sanitize(entry.get('requester_name') or '', 25)}" if entry.get("requester_name") else ""
            lines.append(f"`{i:>2}.` [{sanitize(entry.get('title') or '?', 55)}]({entry.get('webpage_url') or 'https://discord.com'}){who}")
        e = embed(title="📚 Recently played", description="\n".join(lines)[:4000])
        await ctx.send(embed=e)

    # ------------------------------------------------------------------ queue control

    @commands.hybrid_command(name="clear", aliases=["c", "empty", "cq", "clearqueue"], description="Clear the queue (keeps the current track).")
    @commands.guild_only()
    async def clear(self, ctx: commands.Context):
        player = self._gate(ctx)
        n = len(player.queue)
        player.queue.clear()
        await ctx.send(embed=ok_embed(f"🧹 Cleared **{n}** track(s) from the queue."))

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue.")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        player = self._gate(ctx)
        await player.stop()
        await ctx.send(embed=ok_embed("⏹ Stopped and cleared the queue."))

    @commands.hybrid_command(name="disconnect", aliases=["dc", "leave"], description="Stop and leave the voice channel.")
    @commands.guild_only()
    async def disconnect(self, ctx: commands.Context):
        player = self._gate(ctx)
        if player.vc is None:
            raise UserError("I'm not in a voice channel.")
        await player.disconnect()
        set_setting(ctx.guild.id, "stay247", False)
        await ctx.send(embed=ok_embed("👋 Disconnected. (24/7 mode disabled)"))

    @commands.hybrid_command(name="join", aliases=["j", "connect"], description="Make me join your voice channel.")
    @commands.guild_only()
    async def join(self, ctx: commands.Context):
        player = self._player(ctx)
        channel = await self._ensure_player_voice(ctx, player)
        await ctx.send(embed=ok_embed(f"🔊 Joined {channel.mention}."))

    @commands.hybrid_command(name="moveme", aliases=["mm"], description="Move you to my voice channel.")
    @commands.guild_only()
    async def moveme(self, ctx: commands.Context):
        player = self._player(ctx)
        vc = player.vc
        if vc is None or vc.channel is None:
            raise UserError("I'm not in a voice channel.")
        try:
            await ctx.author.move_to(vc.channel)
        except discord.Forbidden as exc:
            raise UserError("I need the **Move Members** permission for that.") from exc
        await ctx.send(embed=ok_embed(f"Moved you to {vc.channel.mention}."))

    @commands.hybrid_command(name="move", aliases=["mv"], description="Move a track to another queue position.")
    @commands.guild_only()
    @app_commands.describe(from_pos="Current position", to_pos="New position")
    async def move(self, ctx: commands.Context, from_pos: int, to_pos: int):
        player = self._gate(ctx)
        track = player.move_track(from_pos, to_pos)
        await ctx.send(embed=ok_embed(f"Moved **{sanitize(track.title)}** to position `#{to_pos}`."))

    @commands.hybrid_command(name="remove", aliases=["rm", "del"], description="Remove a track from the queue.")
    @commands.guild_only()
    @app_commands.describe(position="Queue position to remove")
    async def remove(self, ctx: commands.Context, position: int):
        player = self._gate(ctx)
        track = player.remove_track(position)
        await ctx.send(embed=ok_embed(f"🗑️ Removed **{sanitize(track.title)}** from the queue."))

    @commands.hybrid_command(name="shuffle", aliases=["sh"], description="Shuffle the queue.")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        player = self._gate(ctx)
        if not player.queue:
            raise UserError("The queue is empty.")
        q = list(player.queue)
        random.shuffle(q)
        player.queue = deque(q)
        await ctx.send(embed=ok_embed(f"🔀 Shuffled **{len(q)}** track(s)."))

    # ------------------------------------------------------------------ tuning

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Show or set the volume (1–200).")
    @commands.guild_only()
    @app_commands.describe(level="Volume 1–200 (omit to show the current level)")
    async def volume(self, ctx: commands.Context, level: Optional[int] = None):
        player = self._gate(ctx)
        if level is None:
            await ctx.send(embed=embed(title="🔊 Volume", description=f"Current volume: **{int(round(player.volume * 100))}%**"))
            return
        await player.set_volume(level)
        await ctx.send(embed=ok_embed(f"🔊 Volume set to **{int(round(player.volume * 100))}%**."))

    @commands.hybrid_command(name="loop", aliases=["l", "repeat"], description="Loop off / track / queue.")
    @commands.guild_only()
    @app_commands.describe(mode="off, track or queue (omit to show the current mode)")
    async def loop(self, ctx: commands.Context, mode: Optional[str] = None):
        player = self._gate(ctx)
        mapping = {
            "off": "off", "disable": "off", "none": "off",
            "track": "track", "song": "track", "current": "track",
            "queue": "queue", "all": "queue",
        }
        if mode is None:
            await ctx.send(embed=embed(title="🔁 Loop", description=f"Current mode: **{player.loop_mode}** — use `off`, `track` or `queue`."))
            return
        key = mapping.get(mode.strip().lower())
        if key is None:
            raise UserError("Mode must be `off`, `track` or `queue`.")
        player.loop_mode = key
        await ctx.send(embed=ok_embed(f"🔁 Loop mode set to **{key}**."))

    @commands.hybrid_command(name="seek", description="Jump to a timestamp (e.g. 1:30, 90, +30, -30).")
    @commands.guild_only()
    @app_commands.describe(time="Timestamp: 1:30, 90, +30 or -30")
    async def seek(self, ctx: commands.Context, *, time: str):
        player = self._gate(ctx)
        secs = parse_timestamp(time)
        if secs is None:
            raise UserError("Couldn't parse that time. Try `1:30`, `90`, `+30` or `-30`.")
        if secs < 0:
            secs = max(0, int(player.position()) + secs)
        await player.seek(secs)
        await ctx.send(embed=ok_embed(f"⏩ Seeking to `{fmt_duration(secs)}`."))

    @commands.hybrid_command(name="forward", aliases=["ff"], description="Fast-forward by N seconds (default 10).")
    @commands.guild_only()
    @app_commands.describe(seconds="Seconds to skip forward")
    async def forward(self, ctx: commands.Context, seconds: int = 10):
        player = self._gate(ctx)
        await player.seek(int(player.position()) + max(1, seconds))
        await ctx.send(embed=ok_embed(f"⏩ +{seconds}s"))

    @commands.hybrid_command(name="rewind", aliases=["rw"], description="Rewind by N seconds (default 10).")
    @commands.guild_only()
    @app_commands.describe(seconds="Seconds to go back")
    async def rewind(self, ctx: commands.Context, seconds: int = 10):
        player = self._gate(ctx)
        await player.seek(max(0, int(player.position()) - max(1, seconds)))
        await ctx.send(embed=ok_embed(f"⏪ -{seconds}s"))

    @commands.hybrid_command(name="autoplay", aliases=["ap", "auto"], description="Toggle autoplay (queues related tracks when the queue ends).")
    @commands.guild_only()
    @app_commands.describe(state="on/off (omit to toggle)")
    async def autoplay(self, ctx: commands.Context, state: Optional[bool] = None):
        player = self._gate(ctx)
        player.autoplay = (not player.autoplay) if state is None else bool(state)
        await ctx.send(embed=ok_embed(f"✨ Autoplay **{'enabled' if player.autoplay else 'disabled'}**."))
        if player.autoplay and player.current is None and not player.queue and player.history:
            try:
                related = await self.bot.resolver.related(player.history[-1], 5, seen=player._autoplay_seen)
                player.queue.extend(related)
                if related:
                    await player.start()
            except Exception:
                log.debug("autoplay seed failed", exc_info=True)

    @commands.hybrid_command(name="24-7", aliases=["247", "24h"], description="Stay in the voice channel 24/7.")
    @commands.guild_only()
    @app_commands.describe(state="on/off (omit to toggle)")
    async def stay247(self, ctx: commands.Context, state: Optional[bool] = None):
        player = self._gate(ctx)
        settings = guild_settings(ctx.guild.id)
        if state is None:
            state = not settings.get("stay247")
        set_setting(ctx.guild.id, "stay247", bool(state))
        if state:
            if player.vc is None or not player.vc.is_connected():
                channel = await self._require_voice(ctx)
                await player.ensure_voice(channel)
            await ctx.send(embed=ok_embed("♾️ **24/7 mode enabled** — I'll stay in the voice channel even when idle."))
        else:
            await ctx.send(embed=ok_embed("24/7 mode disabled — I'll leave once idle."))

    @commands.hybrid_command(name="filter", aliases=["fl", "filters"], description="Audio filters (bassboost, nightcore, …).")
    @commands.guild_only()
    @app_commands.describe(preset="Filter name (omit to list presets)")
    async def filter_(self, ctx: commands.Context, preset: Optional[str] = None):
        player = self._gate(ctx)
        if preset is None:
            listed = ", ".join(f"`{k}`" for k in FILTERS)
            await ctx.send(embed=embed(title="🎛 Filters", description=f"Current: **{player.filter}**\nAvailable: {listed}"))
            return
        await player.set_filter(preset)
        await ctx.send(embed=ok_embed(f"🎛 Filter set to **{player.filter}**."))

    # ------------------------------------------------------------------ extras

    @commands.hybrid_command(name="lyrics", aliases=["ly"], description="Lyrics for the current song (or a search).")
    @commands.guild_only()
    @app_commands.describe(query="Song to look up (omit for the current track)")
    async def lyrics(self, ctx: commands.Context, *, query: Optional[str] = None):
        await self._defer(ctx)
        player = self._player(ctx)
        if query is None:
            if player.current is None:
                raise UserError("Nothing is playing — pass a song name.")
            query = build_query(player.current)
        data = await fetch_lyrics(query)
        if not data or (not data.get("plain") and not data.get("instrumental")):
            raise UserError(f"No lyrics found for `{sanitize(query, 60)}`.")
        if data.get("instrumental"):
            await ctx.send(embed=embed(title=f"🎼 {sanitize(data['title'], 60)}", description="Instrumental — no lyrics."))
            return
        text = data["plain"]
        if len(text) > 3900:
            text = text[:3899] + "…"
        await ctx.send(
            embed=embed(
                title=f"📜 {sanitize(data['title'], 60)} — {sanitize(data['artist'], 40)}",
                description=text,
            )
        )

    @commands.hybrid_command(name="radios", aliases=["radio"], description="Built-in radio stations (or a stream URL).")
    @commands.guild_only()
    @app_commands.describe(name="Station name or stream URL (omit to list)")
    async def radios(self, ctx: commands.Context, *, name: Optional[str] = None):
        if name is None:
            lines = [f"`{i:>2}.` {n}" for i, (n, _) in enumerate(RADIO_STATIONS, 1)]
            await ctx.send(
                embed=embed(
                    title="📻 Radio stations",
                    description="\n".join(lines) + "\n\nUse `/radios name:<text>` or paste any stream URL.",
                )
            )
            return
        await self._defer(ctx)
        player = self._player(ctx)
        await self._ensure_player_voice(ctx, player)
        arg = name.strip()
        if arg.startswith("http"):
            title, url = arg.split("/")[2][:80], arg
        else:
            low = arg.lower()
            match = next(((n, u) for n, u in RADIO_STATIONS if low in n.lower()), None)
            if match is None:
                raise UserError(f"No station matches `{sanitize(arg, 40)}` — run `/radios` to list them.")
            title, url = match
        track = Track(
            title=title,
            webpage_url=url,
            direct_stream=True,
            is_live=True,
            requester_id=ctx.author.id,
            requester_name=getattr(ctx.author, "display_name", None) or ctx.author.name,
        )
        pos = player.enqueue(track)
        await player.start()
        await ctx.send(embed=track_embed(track, title="📻 Tuning in", position=pos))

    @commands.hybrid_command(name="clean", aliases=["purge"], description="Delete my recent messages in this channel.")
    @commands.guild_only()
    async def clean(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.TextChannel):
            raise UserError("This only works in a text channel.")
        try:
            deleted = await ctx.channel.purge(limit=100, check=lambda m: m.author == self.bot.user)
        except discord.Forbidden as exc:
            raise UserError("I need **Manage Messages** to do that.") from exc
        await ctx.send(embed=ok_embed(f"🧽 Deleted {len(deleted)} of my messages."))

    @commands.hybrid_command(name="ping", aliases=["latency"], description="Check the bot's latency.")
    @commands.guild_only()
    async def ping(self, ctx: commands.Context):
        ws = int(round(self.bot.latency * 1000))
        await ctx.send(embed=embed(title="🏓 Pong!", description=f"WebSocket latency: `{ws} ms`"))


async def setup(bot):
    await bot.add_cog(Music(bot))
