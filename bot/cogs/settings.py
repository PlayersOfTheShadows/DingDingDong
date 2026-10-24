"""Server settings, request-channel (controller), meta commands."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..player import UserError
from ..store import guild_prefix, guild_settings, set_setting
from ..utils import embed, ok_embed, sanitize

log = logging.getLogger("bot.settings")


async def _temp_send(channel, text: str, seconds: int = 6) -> None:
    try:
        msg = await channel.send(text[:1900])
        await asyncio.sleep(seconds)
        await msg.delete()
    except Exception:
        pass


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ prefix

    @commands.hybrid_command(name="prefix", aliases=["sp", "setprefix"], description="Show or change the command prefix.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(new_prefix="New prefix (omit to show the current one)")
    async def prefix(self, ctx: commands.Context, *, new_prefix: Optional[str] = None):
        if new_prefix is None:
            await ctx.send(embed=ok_embed(f"Current prefix: `{guild_prefix(ctx.guild.id)}`"))
            return
        p = new_prefix.strip()[:5]
        if not p:
            raise UserError("The prefix can't be empty.")
        set_setting(ctx.guild.id, "prefix", p)
        await ctx.send(embed=ok_embed(f"Prefix set to `{p}`"))

    @commands.hybrid_command(name="reset-prefix", description="Reset the prefix to the default.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def reset_prefix(self, ctx: commands.Context):
        set_setting(ctx.guild.id, "prefix", None)
        await ctx.send(embed=ok_embed(f"Prefix reset to `{config.DEFAULT_PREFIX}`"))

    # ------------------------------------------------------------------ dj

    @commands.hybrid_command(name="setdj", aliases=["dj"], description="Set (or clear) the DJ role.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(role="Role allowed to control playback (omit to clear)")
    async def setdj(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        if role is None:
            set_setting(ctx.guild.id, "dj_role", None)
            await ctx.send(embed=ok_embed("DJ role cleared — everyone can control playback."))
            return
        set_setting(ctx.guild.id, "dj_role", role.id)
        await ctx.send(
            embed=ok_embed(
                f"DJ role set to {role.mention}. Control commands now need this role, "
                "a Manage Server permission, or being alone with me in the channel."
            )
        )

    # ------------------------------------------------------------------ controller

    @commands.hybrid_command(name="controller", aliases=["setup"], description="Request channel: type a song name there and it plays.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(mode="create or remove", channel="Existing channel to use (omit to create one)")
    async def controller(self, ctx: commands.Context, mode: str = "create", channel: Optional[discord.TextChannel] = None):
        m = (mode or "").strip().lower()
        if m == "remove":
            set_setting(ctx.guild.id, "controller_channel", None)
            await ctx.send(embed=ok_embed("Request channel removed. The channel itself was left alone."))
            return
        if m != "create":
            raise UserError("Mode must be `create` or `remove`.")
        target = channel
        if target is None:
            try:
                target = await ctx.guild.create_text_channel(
                    "music-controller",
                    category=ctx.channel.category if isinstance(ctx.channel, discord.TextChannel) else None,
                    slowmode_delay=5,
                    topic="Type a song name or link here and it will play. Lines starting with '>' are ignored.",
                )
            except discord.Forbidden as exc:
                raise UserError("I need **Manage Channels** to create the controller channel.") from exc
        set_setting(ctx.guild.id, "controller_channel", target.id)
        await target.send(
            embed=embed(
                title="🎵 Music controller",
                description=(
                    "**Type a song name or link in this channel** — I'll queue it and delete your message.\n"
                    "• One track per line (multi-line works)\n"
                    "• Lines starting with `>` are ignored (comments)\n"
                    "• You must be in a voice channel"
                ),
            )
        )
        await ctx.send(embed=ok_embed(f"Request channel set to {target.mention}."))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        try:
            cid = guild_settings(message.guild.id).get("controller_channel")
        except Exception:
            return
        if not cid or message.channel.id != int(cid):
            return
        content = (message.content or "").strip()
        if not content or content.startswith(guild_prefix(message.guild.id)):
            return
        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith(">")]
        try:
            await message.delete()
        except Exception:
            pass
        if not lines:
            return
        member = message.author
        if member.voice is None or member.voice.channel is None:
            await _temp_send(message.channel, f"{member.mention} — join a voice channel first.")
            return
        player = self.bot.get_player(message.guild)
        player._notice_channel = message.channel
        try:
            if player.vc is None or not player.vc.is_connected():
                await player.ensure_voice(member.voice.channel)
        except UserError as exc:
            await _temp_send(message.channel, f"{member.mention} — {exc}")
            return
        added = 0
        for line in lines[:10]:
            try:
                kind, tracks = await self.bot.resolver.resolve(line, requester=member)
            except Exception:
                continue
            if kind == "track":
                player.enqueue(tracks[0])
                added += 1
            else:
                added += player.extend_queue(tracks)
        if added:
            await player.start()
        else:
            await _temp_send(message.channel, f"{member.mention} — nothing found for that.")

    # ------------------------------------------------------------------ toggles

    @commands.hybrid_command(name="songstatus", description="Toggle the voice-channel status showing the current song.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(state="on/off (omit to toggle)")
    async def songstatus(self, ctx: commands.Context, state: Optional[bool] = None):
        s = guild_settings(ctx.guild.id)
        new = (not s.get("songstatus", True)) if state is None else bool(state)
        set_setting(ctx.guild.id, "songstatus", new)
        await ctx.send(embed=ok_embed(f"Voice channel status **{'enabled' if new else 'disabled'}**."))

    @commands.hybrid_command(name="announce", description="Toggle now-playing announcements.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(state="on/off (omit to toggle)")
    async def announce(self, ctx: commands.Context, state: Optional[bool] = None):
        s = guild_settings(ctx.guild.id)
        new = (not s.get("announce", True)) if state is None else bool(state)
        set_setting(ctx.guild.id, "announce", new)
        await ctx.send(embed=ok_embed(f"Now-playing announcements **{'enabled' if new else 'disabled'}**."))

    @commands.hybrid_command(name="autorejoin", description="Rejoin the voice channel after being kicked (with 24/7).")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(state="on/off (omit to toggle)")
    async def autorejoin(self, ctx: commands.Context, state: Optional[bool] = None):
        s = guild_settings(ctx.guild.id)
        new = (not s.get("autorejoin", False)) if state is None else bool(state)
        set_setting(ctx.guild.id, "autorejoin", new)
        await ctx.send(embed=ok_embed(f"Auto-rejoin **{'enabled' if new else 'disabled'}**."))

    @commands.hybrid_command(name="settings", description="Show this server's music settings.")
    @commands.guild_only()
    async def settings_cmd(self, ctx: commands.Context):
        s = guild_settings(ctx.guild.id)
        dj = f"<@&{s['dj_role']}>" if s.get("dj_role") else "*none*"
        ctrl = f"<#{s['controller_channel']}>" if s.get("controller_channel") else "*none*"
        e = embed(
            title="⚙️ Server settings",
            description=(
                f"**Prefix:** `{guild_prefix(ctx.guild.id)}`\n"
                f"**DJ role:** {dj}\n"
                f"**Request channel:** {ctrl}\n"
                f"**24/7:** {'on' if s.get('stay247') else 'off'}\n"
                f"**Auto-rejoin:** {'on' if s.get('autorejoin') else 'off'}\n"
                f"**Announce now playing:** {'on' if s.get('announce', True) else 'off'}\n"
                f"**Voice status:** {'on' if s.get('songstatus', True) else 'off'}\n"
                f"**Default volume:** {s.get('default_volume', 100)}%"
            ),
        )
        await ctx.send(embed=e)

    # ------------------------------------------------------------------ meta

    @commands.hybrid_command(name="help", aliases=["h"], description="Show all commands.")
    @commands.guild_only()
    async def help_cmd(self, ctx: commands.Context):
        e = embed(
            title=f"🎵 {config.BOT_NAME} — commands",
            description=(
                "**Playback**\n"
                "`play` `playskip` `playtop` `playfile` `search` `pause` `resume` `replay` `back` `addprevious`\n"
                "`skip` `skipto` `stop` `clear` `disconnect` `join` `moveme`\n\n"
                "**Queue & tuning**\n"
                "`queue` `nowplaying` `history` `shuffle` `move` `remove` `volume` `loop` `seek` `forward` `rewind` `filter`\n\n"
                "**Extras**\n"
                "`autoplay` `24-7` `lyrics` `radios` `panel` `clean` `ping`\n\n"
                "**Playlists**\n"
                "`playlist create / add / addtrack / addqueue / show / play / delete / share / lock / list / public`\n\n"
                "**Server settings**\n"
                "`prefix` `reset-prefix` `setdj` `controller` `songstatus` `announce` `autorejoin` `settings` `invite`"
            ),
        )
        e.set_footer(text="Everything works as /slash and !prefix — try /play to start")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="invite", aliases=["link"], description="Get my invite link.")
    @commands.guild_only()
    async def invite(self, ctx: commands.Context):
        perms = discord.Permissions(
            send_messages=True, embed_links=True, attach_files=True, read_message_history=True,
            manage_messages=True, add_reactions=True, view_audit_log=True, create_instant_invite=True,
            manage_channels=True, manage_guild=True, manage_roles=True,
            connect=True, speak=True, move_members=True, use_voice_activation=True, priority_speaker=True,
        )
        url = discord.utils.oauth_url(config.CLIENT_ID, permissions=perms, scopes=("bot", "applications.commands"))
        await ctx.send(embed=embed(title="🔗 Invite me", description=f"[Click here to invite {config.BOT_NAME}]({url})"))

    @commands.hybrid_command(name="updateytdlp", description="Update yt-dlp (bot owner only).")
    @commands.is_owner()
    async def updateytdlp(self, ctx: commands.Context):
        msg = await ctx.send(embed=embed(description="⏳ Updating **yt-dlp**…"))
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-U", "yt-dlp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            text = out.decode(errors="replace")
            tail = "\n".join(text.strip().splitlines()[-6:])[:1500]
            await msg.edit(embed=embed(title="📦 yt-dlp update finished", description=f"```\n{tail}\n```\nRestart the bot to apply."))
        except Exception as exc:  # noqa: BLE001
            await msg.edit(embed=embed(title="❌ Update failed", description=sanitize(str(exc), 400)))


async def setup(bot):
    await bot.add_cog(Settings(bot))
