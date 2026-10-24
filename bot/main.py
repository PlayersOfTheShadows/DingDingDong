"""Entry point — run with:  python -m bot.main  (or run.bat)."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from . import config
from .player import GuildPlayer, UserError
from .store import guild_prefix, guild_settings
from .utils import error_embed
from .ytdl import Resolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


def _prefix_callable(bot: commands.Bot, message: discord.Message):
    if message.guild is not None:
        return commands.when_mentioned_or(guild_prefix(message.guild.id))(bot, message)
    return commands.when_mentioned_or(config.DEFAULT_PREFIX)(bot, message)


class DingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # prefix commands + request channel
        intents.members = True
        super().__init__(
            command_prefix=_prefix_callable,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.players: dict[int, GuildPlayer] = {}
        self.resolver = Resolver()
        self._synced = False

    # ------------------------------------------------------------------ startup

    async def setup_hook(self):
        for ext in ("bot.cogs.music", "bot.cogs.playlists", "bot.cogs.settings"):
            await self.load_extension(ext)
            log.info("loaded extension %s", ext)

    async def on_ready(self):
        log.info("logged in as %s (%s) | %d guild(s)", self.user, self.user.id, len(self.guilds))
        if not self._synced:
            self._synced = True
            await self._sync_commands()

    async def _sync_commands(self):
        try:
            if len(self.guilds) <= 3:
                for guild in list(self.guilds):
                    synced = await self._sync_guild(guild)
                    log.info("synced %d slash commands to guild %s (%s)", len(synced), guild.id, guild.name)
            else:
                synced = await self.tree.sync()
                log.info("synced %d slash commands globally", len(synced))
        except Exception:
            log.exception("command sync failed")

    async def _sync_guild(self, guild: discord.Guild):
        obj = discord.Object(id=guild.id)
        # A guild sync only sends guild-scoped commands — global commands must be
        # copied into the guild first (they are global by default in the cogs).
        self.tree.copy_global_to(guild=obj)
        return await self.tree.sync(guild=obj)

    async def on_guild_join(self, guild: discord.Guild):
        try:
            synced = await self._sync_guild(guild)
            log.info("synced %d slash commands to new guild %s", len(synced), guild.id)
        except Exception:
            log.exception("guild command sync failed")

    # ------------------------------------------------------------------ players

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = GuildPlayer(self, guild)
            self.players[guild.id] = player
            try:
                dv = int(guild_settings(guild.id).get("default_volume", 100) or 100)
                player._volume = max(0.01, min(2.0, dv / 100.0))
            except Exception:
                pass
        return player

    # ------------------------------------------------------------------ events

    async def on_voice_state_update(self, member, before, after):
        if self.user is None or member.id != self.user.id:
            return
        # Kicked from voice: rejoin when 24/7 + auto-rejoin are enabled.
        if before.channel is not None and after.channel is None:
            try:
                settings = guild_settings(member.guild.id)
            except Exception:
                return
            if settings.get("stay247") and settings.get("autorejoin"):
                channel = before.channel

                async def _rejoin(ch, tries: int = 3):
                    for _ in range(tries):
                        await asyncio.sleep(5)
                        try:
                            if member.guild.voice_client is None:
                                await ch.connect(self_deaf=True)
                            return
                        except Exception:
                            continue

                self.loop.create_task(_rejoin(channel))
                log.info("auto-rejoining %s in guild %s", channel, member.guild.id)

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        original: Exception = error
        for _ in range(5):
            nxt = getattr(original, "original", None)
            if nxt is None:
                break
            original = nxt
        if isinstance(original, commands.CommandNotFound):
            return
        if isinstance(original, UserError):
            await self._safe_reply(ctx, error_embed(str(original)))
            return
        if isinstance(original, commands.MissingRequiredArgument):
            await self._safe_reply(ctx, error_embed(f"Missing argument `{original.param.name}` — check `/help`."))
            return
        if isinstance(original, commands.BadArgument):
            await self._safe_reply(ctx, error_embed("I couldn't understand one of those arguments."))
            return
        if isinstance(original, commands.CheckFailure):
            await self._safe_reply(ctx, error_embed("You don't have permission to use that command."))
            return
        log.error("command error (%s): %r", getattr(ctx, "command", None), error)
        await self._safe_reply(ctx, error_embed("Something went wrong — check the bot console for details."))

    async def _safe_reply(self, ctx: commands.Context, e: discord.Embed):
        try:
            await ctx.send(embed=e, ephemeral=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ shutdown

    async def close(self):
        for player in list(self.players.values()):
            try:
                vc = player.vc
                if vc is not None:
                    await vc.disconnect(force=True)
            except Exception:
                pass
        try:
            await self.resolver.close()
        finally:
            await super().close()


async def main() -> int:
    if not config.TOKEN:
        print("ERROR: DISCORD_TOKEN is missing — put it in .env next to this project.")
        return 1
    bot = DingBot()
    try:
        await bot.start(config.TOKEN)
    except discord.LoginFailure:
        print("ERROR: Discord rejected the token (LoginFailure). Check DISCORD_TOKEN in .env.")
        return 1
    except discord.PrivilegedIntentsRequired:
        print(
            "ERROR: Privileged intents are disabled. Enable Message Content + Server Members "
            "on the Bot tab of the Developer Portal, then start again."
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
