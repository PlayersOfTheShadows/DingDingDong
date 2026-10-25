"""Multi-provider AI helper — Groq (OpenAI-compatible) with Google Gemini fallback.

Both providers are tried in order; whichever answers first wins. Provider
problems are retried once and then failed over: capacity spikes, socket
timeouts, and reasoning models that burn the whole token budget and return
empty text (the big one for Groq's gpt-oss models) all get a second attempt
with a bigger budget instead of silently failing.
Keys come from .env (GROQ_API_KEY / GOOGLE_AI_API_KEY). Plain aiohttp.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

import aiohttp

from . import config

log = logging.getLogger("bot.ai")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

MUSIC_SYSTEM = (
    "You are the brain of a Discord music bot that also chats casually in its AI channel — "
    "like a friendly member of the server who happens to run the music. "
    "Decide what the user wants from their message and reply with STRICT JSON only — no prose, no code fences.\n"
    "Available actions:\n"
    '  {"action": "play", "query": "<song name or URL>"} — search and queue/play a song or link\n'
    '  {"action": "pause"} / {"action": "resume"} / {"action": "skip"} / {"action": "stop"}\n'
    '  {"action": "previous"} — go back to the previous track\n'
    '  {"action": "replay"} — restart the current track from 0:00\n'
    '  {"action": "seek", "seconds": <number>} — jump to a timestamp\n'
    '  {"action": "forward", "seconds": <number, default 15>} / {"action": "rewind", "seconds": <number, default 15>}\n'
    '  {"action": "shuffle"} / {"action": "queue"} / {"action": "nowplaying"}\n'
    '  {"action": "remove", "position": <queue position>} — remove one queued track\n'
    '  {"action": "move", "from": <position>, "to": <position>} — reorder the queue\n'
    '  {"action": "clear"} — empty the queue, keep playing the current song\n'
    '  {"action": "filter", "name": "<off|bassboost|bassboost+|nightcore|vaporwave|8d|speed|slowed|treble|lofi|karaoke|deep|chipmunk|earrape|dance|party|soft>"}\n'
    '  {"action": "volume", "level": <1-200>} / {"action": "loop", "mode": "off|track|queue"}\n'
    '  {"action": "jump", "position": <queue position from the state below>}\n'
    '  {"action": "autoplay", "state": true or false} / {"action": "stay", "state": true or false}  (stay = 24/7 mode)\n'
    '  {"action": "disconnect"}\n'
    '  {"action": "chat", "text": "<your reply>"} — for anything that is NOT a music command\n'
    "Chat style: casual, like a friend in a web chat — short (1-3 sentences), plain text, lowercase is fine, "
    "no markdown headers, no assistant-speak. Use the recent conversation to keep context. "
    "Answer questions about the current song/queue from the given state; never invent queued songs. "
    "If the user asks to play something, always use \"play\" with their exact words as the query."
)

CHAT_SYSTEM = (
    "You are {bot} — a Discord music bot chatting in its AI channel like a real server member. "
    "Casual, friendly, short (1-3 sentences), plain text, lowercase is fine. Mirror the user's vibe, "
    "no assistant-speak, no markdown. Reply in the user's language. You can also play music, skip, "
    "change filters and volume — the person just has to ask in this channel."
)


def _describe(exc: BaseException) -> str:
    """One-line, never-empty description of an exception (bare TimeoutError has no text)."""
    name = type(exc).__name__
    text = str(exc).strip()
    if not text:
        return f"{name} (no message)"
    return text if name.lower() in text.lower() else f"{name}: {text}"


class AIError(Exception):
    """No provider could answer."""


class AI:
    # substrings / type names that are worth one immediate retry
    TRANSIENT_HINTS = (
        "http 5", "timeout", "timed out", "connector", "serverdisconnected",
        "clientconnection", "connection reset", "read error", "temporarily",
        "service unavailable", "overloaded", "rate limit", "http 429",
    )

    def __init__(self) -> None:
        self.groq_key = config.GROQ_API_KEY
        self.google_key = config.GOOGLE_AI_API_KEY
        self.groq_model = config.AI_GROQ_MODEL
        self.google_model = config.AI_GOOGLE_MODEL
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_label = ""

    # ------------------------------------------------------------------ plumbing

    @property
    def available(self) -> bool:
        return bool(self.groq_key or self.google_key)

    def provider_labels(self) -> list[str]:
        out = []
        if self.groq_key:
            out.append(f"Groq AI · {self.groq_model}")
        if self.google_key:
            out.append(f"Google AI · {self.google_model}")
        return out

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": _UA},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @classmethod
    def _is_transient(cls, exc: BaseException) -> bool:
        """Capacity spikes, socket hiccups, timeouts — retry those once."""
        if isinstance(exc, asyncio.TimeoutError):
            return True
        name = type(exc).__name__.lower()
        if any(h in name for h in ("timeout", "connector", "disconnected", "reset")):
            return True
        low = str(exc).lower()
        if not low.strip():
            return True  # bare exceptions (e.g. TimeoutError) carry no message
        return any(h in low for h in cls.TRANSIENT_HINTS)

    # ------------------------------------------------------------------ providers

    async def _groq(self, system: str, messages: list[dict], max_tokens: int, json_mode: bool) -> str:
        payload: dict = {
            "model": self.groq_model,
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        sess = await self._sess()
        async with sess.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.groq_key}"},
            timeout=aiohttp.ClientTimeout(total=50),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise AIError(f"groq http {resp.status}: {str(data)[:180]}")
            choice = (data.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            if not text.strip() and choice.get("finish_reason") == "length":
                log.warning("groq hit the token cap before producing text (max_tokens=%s)", max_tokens)
            return text

    async def _google(self, system: str, messages: list[dict], max_tokens: int, json_mode: bool) -> str:
        contents = []
        for m in messages:
            role = "model" if m.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})
        gen: dict = {"maxOutputTokens": max_tokens, "temperature": 0.7}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": gen,
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.google_model}:generateContent"
        sess = await self._sess()
        async with sess.post(
            url,
            json=payload,
            headers={"x-goog-api-key": self.google_key},
            timeout=aiohttp.ClientTimeout(total=80),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise AIError(f"google http {resp.status}: {str(data)[:180]}")
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as exc:
                detail = str(data.get("promptFeedback") or data)[:180]
                raise AIError(f"google returned no text: {detail}") from exc

    def _providers(self):
        if self.groq_key:
            yield "groq", self._groq
        if self.google_key:
            yield "google", self._google

    # ------------------------------------------------------------------ core calls

    async def _provider_text(
        self, name: str, fn, system: str, messages: list[dict], max_tokens: int, json_mode: bool
    ) -> str:
        """One provider, with the recovery ladder:
        json-mode hiccup → plain text · transient error → retry ·
        empty text (reasoning ate the budget) → retry with a bigger budget."""

        async def _call(tokens: int, jm: bool) -> str:
            return await fn(system, messages, tokens, jm)

        try:
            text = await _call(max_tokens, json_mode)
        except AIError as exc:
            low = str(exc).lower()
            if json_mode and "json_validate" in low:
                log.warning("provider %s json mode failed — retrying as plain text", name)
                text = await _call(max_tokens, False)
            elif self._is_transient(exc):
                log.warning("provider %s transient failure (%s) — retrying once", name, _describe(exc))
                await asyncio.sleep(1.5)
                text = await _call(max_tokens, json_mode)
            else:
                raise
        except Exception as exc:  # noqa: BLE001 — timeouts / socket noise are not AIError
            if self._is_transient(exc):
                log.warning("provider %s transient failure (%s) — retrying once", name, _describe(exc))
                await asyncio.sleep(1.5)
                text = await _call(max_tokens, json_mode)
            else:
                raise
        if not (text or "").strip():
            bigger = min(max(max_tokens * 2, 900), 2400)
            log.warning("provider %s returned empty text — retrying with max_tokens=%d", name, bigger)
            text = await _call(bigger, False)
        if not (text or "").strip():
            raise AIError(f"{name} returned an empty response twice")
        return text

    async def chat(
        self,
        *,
        system: str,
        message: str,
        max_tokens: int = 400,
        json_mode: bool = False,
    ) -> tuple[str, str]:
        """Returns (text, provider_label). Tries each configured provider in order."""
        messages = [{"role": "user", "content": message}]
        last: Optional[BaseException] = None
        for name, fn in self._providers():
            try:
                text = await self._provider_text(name, fn, system, messages, max_tokens, json_mode)
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("ai provider %s failed: %s", name, _describe(exc))
                continue
            model = self.groq_model if name == "groq" else self.google_model
            label = f"{'Groq AI' if name == 'groq' else 'Google AI'} · {model}"
            self.last_label = label
            return text, label
        raise AIError(_describe(last) if last else "no AI providers configured")

    @staticmethod
    def _loose_json(text: str) -> dict:
        t = (text or "").strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE)
        try:
            return json.loads(t)
        except Exception:  # noqa: BLE001
            m = re.search(r"\{.*\}", t, flags=re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:  # noqa: BLE001
                    pass
        raise AIError("AI returned unparseable JSON")

    async def interpret_music(self, text: str, *, state: str, history: str = "") -> Optional[dict]:
        """Natural language → action dict (or a 'chat' action). None on failure."""
        msg = f"Server state:\n{state}\n"
        if history:
            msg += f"\nRecent conversation (oldest → newest):\n{history}\n"
        msg += f"\nUser message: {text}"
        # Generous budget: reasoning models (groq gpt-oss) can otherwise spend
        # every token thinking and return an empty string.
        raw, _ = await self.chat(
            system=MUSIC_SYSTEM,
            message=msg,
            max_tokens=900,
            json_mode=True,
        )
        try:
            data = self._loose_json(raw)
        except AIError:
            return None
        if isinstance(data, dict) and data.get("action"):
            return data
        return None

    async def suggest_track(
        self, *, recent: list[str], playlists: list[str], current: str
    ) -> Optional[str]:
        """One query string for autoplay, based on the listener's recent taste."""
        system = (
            "You pick the NEXT song for a Discord music bot's autoplay. "
            'Reply with STRICT JSON: {"query": "<artist - song title>"} — exactly one real, findable song. '
            "Match the vibe of the recent tracks; prefer the user's playlist songs when they fit; never repeat a recent track."
        )
        msg = (
            f"Current: {current or '(none)'}\n"
            f"Recent: {', '.join(x for x in recent if x)[:1200] or '(none)'}\n"
            f"Playlist picks: {', '.join(x for x in playlists if x)[:1200] or '(none)'}\n"
            "Pick the next song."
        )
        raw, _ = await self.chat(system=system, message=msg, max_tokens=500, json_mode=True)
        try:
            data = self._loose_json(raw)
        except AIError:
            return None
        q = str(data.get("query") or "").strip()[:150]
        return q or None

    async def recommend(
        self, *, user_name: str, recent: list[str], playlist: list[str], count: int = 5
    ) -> tuple[list[str], str]:
        """(songs, provider_label) list of 'artist - title' suggestions."""
        system = (
            "You recommend music for one Discord user. "
            f'Reply with STRICT JSON: {{"songs": ["artist - title", ...]}} — up to {count} real, findable songs. '
            "Match the taste of their recent tracks and playlist; no duplicates of the recent list."
        )
        msg = (
            f"User: {user_name or 'them'}\n"
            f"Recent: {', '.join(x for x in recent if x)[:1200] or '(none)'}\n"
            f"From their playlists: {', '.join(x for x in playlist if x)[:1200] or '(none)'}"
        )
        raw, label = await self.chat(system=system, message=msg, max_tokens=700, json_mode=True)
        try:
            data = self._loose_json(raw)
        except AIError:
            return [], label
        songs = [str(s).strip()[:110] for s in (data.get("songs") or []) if str(s).strip()]
        return songs[:count], label
