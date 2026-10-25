"""Interactive views: now-playing controls, queue pagination, vote-skip.

Note: our refresh helper is deliberately named `_refresh_panel` — discord.py's
View base class owns a private synchronous `_refresh(components)`, and
overriding that name with an async method breaks the library (components stop
refreshing and asyncio warns about an un-awaited coroutine).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import discord

from .store import guild_settings, set_setting
from .utils import embed, has_dj_bypass, np_embed, queue_embed

log = logging.getLogger("bot.views")

# Shown in the filter dropdown — order matters. Mirrors bot/player.py FILTERS.
_FALLBACK_FILTERS = [
    "bassboost", "bassboost+", "nightcore", "vaporwave", "8d", "speed", "slowed",
    "treble", "lofi", "karaoke", "deep", "chipmunk", "earrape", "dance", "party", "soft",
]


def _same_channel(interaction: discord.Interaction, player) -> bool:
    vc = player.vc
    member = interaction.user
    if vc is None or vc.channel is None or not isinstance(member, discord.Member):
        return False
    return member.voice is not None and member.voice.channel == vc.channel


class NowPlayingView(discord.ui.View):
    """The control panel under now-playing messages.

    Layout is a fixed rectangle: 3 rows x 4 buttons, plus a full-width filter
    dropdown underneath:

        [ Previous ] [ Pause   ] [ Skip ] [ Stop ]
        [ Vol -10  ] [ Vol +10 ] [Shuffle] [Loop: …]
        [Autoplay: …] [24/7: …] [Queue ] [Disconnect]
        [ Filter: <current>  ▾ ]

    One panel message per player — loop repeats and every new track edit it in
    place instead of posting a new message.
    """

    def __init__(self, player):
        super().__init__(timeout=600)
        self.player = player
        self.message: Optional[discord.Message] = None
        try:
            from .player import FILTERS  # lazy import — avoids a circular import

            names = ["off", *[n for n in FILTERS.keys() if n != "off"]]
        except Exception:  # noqa: BLE001
            names = ["off", *_FALLBACK_FILTERS]
        self.filter_select.options = [
            discord.SelectOption(label=n, value=n, default=(n == player.filter))
            for n in names[:25]
        ]
        self._sync()

    # ---------------- helpers ----------------

    def _sync(self) -> None:
        p = self.player
        self.toggle.label = "Resume" if p.is_paused else "Pause"
        self.loop_btn.label = f"Loop: {p.loop_mode}"
        self.auto_btn.label = f"Autoplay: {'on' if p.autoplay else 'off'}"
        try:
            stay = bool(guild_settings(p.guild.id).get("stay247"))
        except Exception:  # noqa: BLE001
            stay = False
        self.stay_btn.label = f"24/7: {'on' if stay else 'off'}"
        self.filter_select.placeholder = f"Filter: {p.filter}"

    def _embed(self) -> discord.Embed:
        p = self.player
        if p.current is None:
            return embed(title="Stopped", description="The queue is empty.")
        return np_embed(
            p.current,
            position=p.position(),
            loop_mode=p.loop_mode,
            volume=p.volume,
            filter_name=p.filter,
            queue_len=len(p.queue),
            autoplay=p.autoplay,
            paused=p.is_paused,
        )

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        self._sync()
        try:
            await interaction.response.edit_message(embed=self._embed(), view=self)
        except discord.HTTPException:
            pass

    @staticmethod
    async def _deny(interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_message(
                "You need to be in my voice channel to use these buttons.", ephemeral=True
            )
        except discord.HTTPException:
            pass

    # ---------------- row 1: transport ----------------

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        prev = await self.player.play_previous()
        if prev is None:
            try:
                await interaction.response.send_message("No previous track in this session.", ephemeral=True)
            except discord.HTTPException:
                pass
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        if self.player.is_paused:
            self.player.resume()
        elif self.player.is_playing:
            self.player.pause()
        await self._refresh_panel(interaction)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        result = await attempt_skip(self.player, interaction.user, channel=interaction.channel)
        if result == "vote":
            try:
                await interaction.response.send_message("Vote started — see the message above.", ephemeral=True)
            except discord.HTTPException:
                pass
        else:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        await self.player.stop()
        try:
            await interaction.response.edit_message(
                embed=embed(title="Stopped", description="Playback stopped and the queue was cleared."),
                view=None,
            )
        except discord.HTTPException:
            pass

    # ---------------- row 2: mixer ----------------

    @discord.ui.button(label="Vol -10", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        await self.player.set_volume(int(round(self.player.volume * 100)) - 10)
        await self._refresh_panel(interaction)

    @discord.ui.button(label="Vol +10", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        await self.player.set_volume(int(round(self.player.volume * 100)) + 10)
        await self._refresh_panel(interaction)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        n = self.player.shuffle_queue()
        try:
            await interaction.response.send_message(
                f"Shuffled {n} track(s)." if n else "The queue is empty.", ephemeral=True
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Loop: off", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        order = ["off", "track", "queue"]
        idx = order.index(self.player.loop_mode) if self.player.loop_mode in order else 0
        self.player.loop_mode = order[(idx + 1) % len(order)]
        await self._refresh_panel(interaction)

    # ---------------- row 3: modes & session ----------------

    @discord.ui.button(label="Autoplay: off", style=discord.ButtonStyle.secondary, row=2)
    async def auto_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        self.player.autoplay = not self.player.autoplay
        await self._refresh_panel(interaction)

    @discord.ui.button(label="24/7: off", style=discord.ButtonStyle.secondary, row=2)
    async def stay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        settings = guild_settings(interaction.guild.id)
        new_state = not bool(settings.get("stay247"))
        set_setting(interaction.guild.id, "stay247", new_state)
        await self._refresh_panel(interaction)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, row=2)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        e, pages = queue_embed(
            list(player.queue),
            player.current,
            1,
            loop_mode=player.loop_mode,
            autoplay=player.autoplay,
            paused=player.is_paused,
        )
        kwargs: dict = {"embed": e, "ephemeral": True}
        if pages > 1:
            kwargs["view"] = QueueView(player, 1, pages)
        try:
            await interaction.response.send_message(**kwargs)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Disconnect", style=discord.ButtonStyle.danger, row=2)
    async def disconnect_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        await self.player.disconnect()
        set_setting(interaction.guild.id, "stay247", False)
        try:
            await interaction.response.edit_message(
                embed=embed(title="Disconnected", description="Left the voice channel."), view=None
            )
        except discord.HTTPException:
            pass

    # ---------------- row 4: filter dropdown ----------------

    @discord.ui.select(placeholder="Filter: off", row=3)
    async def filter_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not _same_channel(interaction, self.player):
            return await self._deny(interaction)
        try:
            await self.player.set_filter(select.values[0])
        except Exception as exc:  # noqa: BLE001
            try:
                await interaction.response.send_message(str(exc), ephemeral=True)
            except discord.HTTPException:
                pass
            return
        await self._refresh_panel(interaction)


class QueueView(discord.ui.View):
    def __init__(self, player, page: int, pages: int):
        super().__init__(timeout=300)
        self.player = player
        self.page = page
        self.pages = pages
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.page <= 1
        self.next.disabled = self.page >= self.pages

    def _embed(self) -> discord.Embed:
        e, pages = queue_embed(
            list(self.player.queue),
            self.player.current,
            self.page,
            loop_mode=self.player.loop_mode,
            autoplay=self.player.autoplay,
            paused=self.player.is_paused,
        )
        self.pages = pages
        self._sync()
        return e

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        try:
            await interaction.response.edit_message(embed=self._embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages, self.page + 1)
        try:
            await interaction.response.edit_message(embed=self._embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass


class VoteSkipView(discord.ui.View):
    """Flavi-style skip vote: <=2 listeners skip instantly, 3-4 need 2 votes,
    5+ need 60%. Only people in the voice channel may vote."""

    def __init__(self, player, *, required: int, listeners: list[discord.Member], starter: discord.Member):
        super().__init__(timeout=120)
        self.player = player
        self.required = required
        self.listener_ids = {m.id for m in listeners}
        self.starter_id = starter.id
        self.votes: set[int] = set()
        self.message: Optional[discord.Message] = None
        if starter.id in self.listener_ids:
            self.votes.add(starter.id)
        self._sync()

    def _sync(self) -> None:
        self.vote_btn.label = f"Vote ({len(self.votes)}/{self.required})"

    def build_embed(self, *, done: bool = False, cancelled: bool = False) -> discord.Embed:
        if done:
            return embed(title="Vote passed", description="Skipping to the next track.")
        if cancelled:
            return embed(title="Vote cancelled")
        return embed(
            title="Vote to skip",
            description=(
                f"**{len(self.votes)}/{self.required}** votes needed.\n"
                "Only members in the voice channel can vote. Expires in 2 minutes."
            ),
        )

    @discord.ui.button(label="Vote", style=discord.ButtonStyle.success)
    async def vote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.listener_ids:
            try:
                await interaction.response.send_message("Only members in the voice channel can vote.", ephemeral=True)
            except discord.HTTPException:
                pass
            return
        if interaction.user.id in self.votes:
            await interaction.response.send_message("You already voted.", ephemeral=True)
            return
        self.votes.add(interaction.user.id)
        self._sync()
        if len(self.votes) >= self.required:
            self.stop()
            try:
                await interaction.response.edit_message(embed=self.build_embed(done=True), view=None)
            except discord.HTTPException:
                pass
            await self.player.skip()
            return
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        allowed = member.id == self.starter_id or (
            isinstance(member, discord.Member) and member.guild_permissions.manage_messages
        )
        if not allowed:
            try:
                await interaction.response.send_message(
                    "Only the person who started the vote (or a moderator) can cancel it.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return
        self.stop()
        try:
            await interaction.response.edit_message(embed=self.build_embed(cancelled=True), view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=embed(title="Vote expired", description="Not enough votes — the track keeps playing."),
                    view=None,
                )
            except discord.HTTPException:
                pass


async def attempt_skip(player, member, *, channel=None) -> str:
    """Returns 'skipped', 'vote' or 'nothing'."""
    vc = player.vc
    if vc is None or vc.channel is None:
        return "nothing"
    if not (vc.is_playing() or vc.is_paused()) and player.current is None:
        return "nothing"
    listeners = [m for m in vc.channel.members if not m.bot]
    settings = guild_settings(player.guild.id)
    if len(listeners) <= 2 or has_dj_bypass(member, settings, vc):
        await player.skip()
        return "skipped"
    needed = 2 if len(listeners) <= 4 else math.ceil(len(listeners) * 0.6)
    target = channel or player._notice_channel
    if target is None:
        await player.skip()
        return "skipped"
    view = VoteSkipView(player, required=needed, listeners=listeners, starter=member)
    try:
        view.message = await target.send(embed=view.build_embed(), view=view)
    except discord.HTTPException:
        await player.skip()
        return "skipped"
    return "vote"
