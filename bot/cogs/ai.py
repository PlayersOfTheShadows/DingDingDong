"""AI cog: a chat channel where the AI talks and controls the music, plus
/ai commands and /recommend. Provider failover lives in bot/ai.py."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..ai import CHAT_SYSTEM, AIError
from ..player import UserError
from ..store import get_history, guild_prefix, guild_settings, set_setting, user_playlists
from ..utils import embed, sanitize

log = logging.getLogger("bot.ai.cog")


class AICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ helpers

    def _ai(self):
        ai = getattr(self.bot, "ai", None)
        if ai is None or not ai.available:
            raise UserError("The AI isn't configured yet — no API keys found in .env.")
        return ai

    def _state(self, guild: discord.Guild) -> str:
        player = self.bot.get_player(guild)
        lines = []
        cur = player.current
        if cur is not None:
            who = f" (requested by {sanitize(cur.requester_name, 30)})" if cur.requester_name else ""
            state = "paused" if player.is_paused else "playing"
            lines.append(f"Now {state}: {sanitize(cur.title, 80)} [{sanitize(cur.uploader or '', 40)}]{who}")
        else:
            lines.append("Nothing is playing.")
        q = list(player.queue)
        if q:
            shown = "; ".join(f"{i}. {sanitize(t.title, 50)}" for i, t in enumerate(q[:10], 1))
            lines.append(f"Queue ({len(q)} total): {shown}")
        else:
            lines.append("Queue: empty")
        vc = player.vc
        listeners = len([m for m in vc.channel.members if not m.bot]) if vc and vc.channel else 0
        lines.append(
            f"Voice listeners: {listeners}. Volume {int(round(player.volume * 100))}%. "
            f"Filter {player.filter}. Loop {player.loop_mode}. Autoplay {'on' if player.autoplay else 'off'}."
        )
        return "\n".join(lines)

    def _reply_embed(self, text: str, label: str) -> discord.Embed:
        e = embed(description=text[:1900] or "…")
        e.set_footer(text=f"Using {label}" if label else config.BOT_NAME)
        return e

    # ------------------------------------------------------------------ action executor

    async def _execute(self, guild: discord.Guild, action: dict, member: discord.Member, channel) -> str:
        act = str(action.get("action") or "").lower()
        player = self.bot.get_player(guild)
        player._notice_channel = channel

        async def need_voice() -> None:
            if member.voice is None or member.voice.channel is None:
                raise UserError("Join a voice channel first.")
            if player.vc is None or not player.vc.is_connected():
                await player.ensure_voice(member.voice.channel)

        def as_int(value, minimum: int, maximum: int) -> Optional[int]:
            try:
                return max(minimum, min(maximum, int(value)))
            except (TypeError, ValueError):
                return None

        if act == "play":
            query = str(action.get("query") or "").strip()[:300]
            if not query:
                raise UserError("I need a song name to play.")
            await need_voice()
            kind, tracks = "track", []
            try:
                kind, tracks = await self.bot.resolver.resolve(query, requester=member)
            except Exception:  # noqa: BLE001
                sc = await self.bot.resolver.sc_search(query, requester=member)
                if sc is not None:
                    kind, tracks = "track", [sc]
            if not tracks:
                raise UserError(f"Couldn't find anything playable for `{sanitize(query, 60)}`.")
            if kind == "playlist":
                added = player.extend_queue(tracks)
                await player.start()
                return f"Queued {added} tracks from that playlist."
            track = tracks[0]
            was_idle = player.current is None and not player.is_playing and not player.is_paused
            pos = player.enqueue(track)
            await player.start()
            if was_idle:
                return f"Playing **{sanitize(track.title)}**."
            return f"Queued **{sanitize(track.title)}** at position #{pos}."

        if act == "pause":
            if not player.pause():
                raise UserError("Nothing is playing.")
            return "Paused."
        if act == "resume":
            if not player.resume():
                raise UserError("Nothing is paused.")
            return "Resumed."
        if act == "skip":
            if player.current is None:
                raise UserError("Nothing is playing.")
            await player.skip()
            return "Skipped to the next track."
        if act == "stop":
            await player.stop()
            return "Stopped and cleared the queue."
        if act == "shuffle":
            n = player.shuffle_queue()
            return f"Shuffled {n} track(s)." if n else "The queue is empty."
        if act == "queue":
            q = list(player.queue)
            if not q:
                return "The queue is empty."
            shown = "; ".join(f"{i}. {sanitize(t.title, 45)}" for i, t in enumerate(q[:8], 1))
            more = f" …and {len(q) - 8} more" if len(q) > 8 else ""
            return f"Queue ({len(q)}): {shown}{more}"
        if act == "nowplaying":
            if player.current is None:
                raise UserError("Nothing is playing right now.")
            cur = player.current
            return f"Now playing **{sanitize(cur.title)}** by {sanitize(cur.uploader or 'unknown', 40)}."
        if act == "filter":
            name = str(action.get("name") or "").strip().lower()
            await player.set_filter(name)
            return f"Filter set to **{player.filter}**."
        if act == "volume":
            level = as_int(action.get("level"), 1, 200)
            if level is None:
                raise UserError("Volume needs to be a number between 1 and 200.")
            await player.set_volume(level)
            return f"Volume set to {int(round(player.volume * 100))}%."
        if act == "loop":
            mode = str(action.get("mode") or "").strip().lower()
            if mode not in ("off", "track", "queue"):
                raise UserError("Loop mode must be off, track or queue.")
            player.loop_mode = mode
            return f"Loop mode set to {mode}."
        if act == "jump":
            pos = as_int(action.get("position"), 1, 100000)
            if pos is None:
                raise UserError("I need a queue position to jump to.")
            target = player.jump_to(pos)
            if player.is_playing or player.is_paused:
                await player.skip()
            else:
                await player.start()
            return f"Jumping to **{sanitize(target.title)}**."
        if act == "autoplay":
            state = action.get("state")
            player.autoplay = bool(state) if state is not None else (not player.autoplay)
            return f"Autoplay {'enabled' if player.autoplay else 'disabled'}."
        if act == "disconnect":
            if player.vc is None:
                raise UserError("I'm not in a voice channel.")
            await player.disconnect()
            set_setting(guild.id, "stay247", False)
            return "Left the voice channel."
        raise UserError("I'm not sure how to do that yet — try asking differently.")

    # ------------------------------------------------------------------ passive chat channel

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        try:
            cid = guild_settings(message.guild.id).get("ai_channel")
        except Exception:  # noqa: BLE001
            return
        if not cid or message.channel.id != int(cid):
            return
        content = (message.content or "").strip()
        if not content or content.startswith(guild_prefix(message.guild.id)):
            return
        ai = getattr(self.bot, "ai", None)
        if ai is None or not ai.available:
            return
        async with message.channel.typing():
            action = None
            try:
                action = await ai.interpret_music(content, state=self._state(message.guild))
            except Exception:  # noqa: BLE001
                log.warning("ai interpret failed", exc_info=True)
            label = ai.last_label
            if not action:
                try:
                    text, label = await ai.chat(
                        system=CHAT_SYSTEM.format(bot=config.BOT_NAME),
                        message=content,
                        max_tokens=400,
                    )
                except Exception:  # noqa: BLE001
                    await message.reply(
                        "I couldn't reach the AI just now — try again in a minute.",
                        mention_author=False,
                    )
                    return
                await message.reply(embed=self._reply_embed(text, label), mention_author=False)
                return
            act = str(action.get("action") or "chat").lower()
            if act == "chat":
                await message.reply(
                    embed=self._reply_embed(str(action.get("text") or "…")[:1500], label),
                    mention_author=False,
                )
                return
            try:
                note = await self._execute(message.guild, action, message.author, message.channel)
            except UserError as exc:
                note = str(exc)
            except Exception:  # noqa: BLE001
                log.exception("ai action failed")
                note = "Something went wrong running that — try again."
            await message.reply(embed=self._reply_embed(note, label), mention_author=False)

    # ------------------------------------------------------------------ commands

    @commands.hybrid_group(name="ai", description="AI assistant: chat channel, questions, models.", invoke_without_command=True)
    @commands.guild_only()
    async def ai_cmd(self, ctx: commands.Context):
        s = guild_settings(ctx.guild.id)
        chan = f"<#{s['ai_channel']}>" if s.get("ai_channel") else "*not set*"
        ai = getattr(self.bot, "ai", None)
        providers = ai.provider_labels() if ai is not None else []
        e = embed(title="AI assistant")
        e.description = (
            f"**Chat channel:** {chan}\n"
            f"**Providers:** {', '.join(providers) if providers else 'none configured'}\n\n"
            "`/ai channel` — set the channel where you just type naturally\n"
            "`/ai ask` — ask a one-off question anywhere\n"
            "`/ai model` — show the models in use\n"
            "`/recommend` — AI picks songs from your recent tracks & playlists\n\n"
            "In the AI channel try: *play darkside* · *skip* · *set filter nightcore* · *volume 40* · *what's queued?* — or just chat."
        )
        await ctx.send(embed=e)

    @ai_cmd.command(name="channel", description="Set the AI chat channel ('off' clears it).")
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(action="'off' to disable, or omit", channel="Channel to use (omit for the current one)")
    async def ai_channel(self, ctx: commands.Context, action: Optional[str] = None, channel: Optional[discord.TextChannel] = None):
        if (action or "").strip().lower() == "off":
            set_setting(ctx.guild.id, "ai_channel", None)
            await ctx.send(embed=embed(description="AI chat channel cleared."))
            return
        target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if target is None:
            raise UserError("Pass a channel or run this inside one.")
        set_setting(ctx.guild.id, "ai_channel", target.id)
        await target.send(
            embed=embed(
                title="AI is here",
                description=(
                    "Talk to me naturally in this channel.\n"
                    "• *play <song or link>* — I search and play it\n"
                    "• *skip / pause / resume / stop / shuffle*\n"
                    "• *set filter bassboost* · *volume 50* · *loop track*\n"
                    "• *jump to 3* · *what's queued?* · *what's playing?*\n"
                    "• or just ask me anything."
                ),
            )
        )
        await ctx.send(embed=embed(description=f"AI chat channel set to {target.mention}."))

    @ai_cmd.command(name="ask", description="Ask the AI anything.")
    @app_commands.describe(question="Your question")
    async def ai_ask(self, ctx: commands.Context, *, question: str):
        ai = self._ai()
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer()
        try:
            text, label = await ai.chat(
                system=CHAT_SYSTEM.format(bot=config.BOT_NAME),
                message=question,
                max_tokens=600,
            )
        except AIError:
            raise UserError("The AI providers didn't answer — try again in a minute.")
        await ctx.send(embed=self._reply_embed(text, label))

    @ai_cmd.command(name="model", description="Show which AI models are in use.")
    async def ai_model(self, ctx: commands.Context):
        ai = getattr(self.bot, "ai", None)
        providers = ai.provider_labels() if ai is not None else []
        lines = [f"• {p}" for p in providers] or ["• none configured — add GROQ_API_KEY / GOOGLE_AI_API_KEY to .env"]
        if ai is not None and ai.last_label:
            lines.append(f"\nLast reply used: **{ai.last_label}**")
        await ctx.send(embed=embed(title="AI models", description="\n".join(lines)))

    @commands.hybrid_command(name="recommend", aliases=["rec"], description="AI recommendations from your recent tracks and playlists.")
    @commands.guild_only()
    @app_commands.describe(user="Whose taste to use (default: you)", count="How many suggestions (1-8)")
    async def recommend(self, ctx: commands.Context, user: Optional[discord.Member] = None, count: int = 5):
        ai = self._ai()
        target = user or ctx.author
        count = max(1, min(8, int(count)))
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.interaction.response.defer()
        hist = get_history(ctx.guild.id, 80)
        recent = [h.get("title") or "" for h in hist if h.get("requester_id") == target.id][:10]
        if not recent and target.id == ctx.author.id:
            recent = [h.get("title") or "" for h in hist][:10]
        pl_songs: list[str] = []
        for pl in list(user_playlists(target.id).values())[:5]:
            pl_songs += [t.get("title") or "" for t in (pl.get("tracks") or [])][:20]
        try:
            songs, label = await ai.recommend(
                user_name=getattr(target, "display_name", None) or target.name,
                recent=recent,
                playlist=pl_songs,
                count=count,
            )
        except AIError:
            raise UserError("The AI couldn't think of anything right now — try again later.")
        if not songs:
            raise UserError("No recommendations yet — play a few songs first, or build a playlist.")
        lines = [f"`{i}.` {sanitize(s, 90)}" for i, s in enumerate(songs, 1)]
        e = embed(
            title=f"Recommendations for {sanitize(getattr(target, 'display_name', None) or target.name, 40)}",
            description="\n".join(lines) + "\n\n-# Play any of these with `/play <name>`",
        )
        e.set_footer(text=f"Using {label}")
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(AICog(bot))
