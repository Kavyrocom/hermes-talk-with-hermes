"""One voice call in a Discord voice channel: the salon's audio in, the
voice model's audio out, and the agent of that channel behind the `talk` tool.

The agent is reached through its own API server route (`/p/<profile>/`), the
same door any client uses: its tools, memory and approvals stay the host's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

from talk_with_hermes import identity as identity_mod
from talk_with_hermes import persona, services
from talk_with_hermes.audio import DiscordToGemini, GeminiToDiscord
from talk_with_hermes.gemini_live import LiveConfig, LiveHandlers
from talk_with_hermes.prefs import Prefs, PrefsStore, apply_words

log = logging.getLogger(__name__)

FRAME_BYTES = 3840  # 20 ms of 48 kHz stereo s16le, what discord.py reads per frame
SILENCE = b"\x00" * FRAME_BYTES
#: Spoken-ahead budget: past this the oldest audio goes (a stuck reader must not grow memory).
MAX_QUEUED_BYTES = FRAME_BYTES * 50 * 120
PUMP_SECONDS = 0.04
#: Consecutive failed reads of the host's voice receiver (2 s at PUMP_SECONDS)
#: before the call ends with a reason instead of staying up deaf.
DRAIN_FAILURES_TO_END = 50
DELEGATION_TIMEOUT = 600.0
#: Agent requests one call may have running at once, as on Desktop
#: (desktop_bridge.MAX_WAITING): a turn with more parallel `talk` calls gets a
#: spoken refusal for the extra ones instead of starting them all.
MAX_RUNNING = 3

Delegate = Callable[[str], Awaitable[str]]


class Playback:
    """The bot's voice: a never-ending source, silence when nothing is queued,
    so discord.py keeps one stream for the whole call. `read` runs on
    discord.py's sender thread; `feed` and `clear` on the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()

    def feed(self, pcm48_stereo: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm48_stereo)
            over = len(self._buf) - MAX_QUEUED_BYTES
            if over > 0:
                del self._buf[:over + (-over % FRAME_BYTES)]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self) -> bytes:
        with self._lock:
            if len(self._buf) >= FRAME_BYTES:
                frame = bytes(self._buf[:FRAME_BYTES])
                del self._buf[:FRAME_BYTES]
                return frame
            if self._buf:  # the tail of a sentence, padded
                frame = bytes(self._buf) + SILENCE[len(self._buf):]
                self._buf.clear()
                return frame
        return SILENCE

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.clear()


def discord_source(playback: Playback) -> Any:
    """Wrap Playback as a discord.AudioSource (discord imported only here)."""

    import discord

    class _Source(discord.AudioSource):
        def read(self) -> bytes:
            return playback.read()

        def is_opus(self) -> bool:
            return False

        def cleanup(self) -> None:
            playback.cleanup()

    return _Source()


def settings_of(home: Path) -> dict[str, Any]:
    """`plugins.entries.talk-with-hermes.settings` of one profile's config.yaml."""

    try:
        from hermes_yaml import safe_load

        config = safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    node: Any = config
    for key in ("plugins", "entries", "talk-with-hermes", "settings"):
        node = node.get(key) if isinstance(node, dict) else None
    return dict(node) if isinstance(node, dict) else {}


def build_config(settings: dict[str, Any], api_key: str, prefs: Prefs, who: identity_mod.Identity) -> LiveConfig:
    from talk_with_hermes.desktop_bridge import config_from

    return config_from(settings, api_key, prefs, who, context=DISCORD_CONTEXT)


DISCORD_CONTEXT = ("Tu parles dans un salon vocal Discord. L'opérateur t'entend par son casque ; "
                   "le son passe par Discord, il peut y avoir un léger décalage.")


class ApiDelegate:
    """`talk` calls sent to the agent's API server route, one session per call."""

    def __init__(self, base: str, profile: str, key: str, *, timeout: float = DELEGATION_TIMEOUT,
                 post: Callable[..., Awaitable[tuple[int, dict, dict]]] | None = None,
                 get: Callable[..., Awaitable[int]] | None = None) -> None:
        prefix = "" if profile in ("", "default") else f"/p/{profile}"
        self.url = f"{base.rstrip('/')}{prefix}/v1/chat/completions"
        self._key = key
        self._timeout = timeout
        self._post = post or _aiohttp_post
        self._get = get or _aiohttp_get
        self.session_id = ""

    async def probe(self) -> int:
        """HTTP status of the agent's door with this key (401 = not open yet)."""

        return await self._get(self.url.replace("/chat/completions", "/models"),
                               {"Authorization": f"Bearer {self._key}"}, 10.0)

    async def __call__(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        if self.session_id:
            headers["X-Hermes-Session-Id"] = self.session_id
        body = {"model": "hermes-agent", "stream": False, "messages": [{"role": "user", "content": prompt}]}
        status, reply_headers, payload = await self._post(self.url, headers, body, self._timeout)
        if status == 401:
            raise RuntimeError("l'agent refuse l'accès à sa porte locale")
        if status >= 400:
            message = ((payload or {}).get("error") or {}).get("message") if isinstance(payload, dict) else ""
            raise RuntimeError(f"l'agent a répondu {status}" + (f" : {message}" if message else ""))
        self.session_id = reply_headers.get("X-Hermes-Session-Id") or self.session_id
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("réponse de l'agent illisible") from None
        return str(text or "").strip() or "L'agent n'a rien répondu."


async def _aiohttp_post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict, dict]:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.post(url, headers=headers, data=json.dumps(body)) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = {}
            return response.status, dict(response.headers), payload


async def _aiohttp_get(url: str, headers: dict, timeout: float) -> int:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.get(url, headers=headers) as response:
            return response.status


class DiscordCall:
    """Wires one voice session to a Discord voice connection.

    `drain()` returns the newest salon PCM (48 kHz stereo) of the allowed
    speaker; `play` receives the playback object to install on the voice
    client. Both come from discord_host in production, from fakes in tests.
    """

    def __init__(self, *, config: LiveConfig, delegate: Delegate, drain: Callable[[], bytes],
                 words: tuple = (), address: str = "tu",
                 session_factory: Callable[..., Any] = services.open_session,
                 pump_seconds: float = PUMP_SECONDS, on_end: Callable[[str], Any] | None = None) -> None:
        self.config = config
        self.playback = Playback()
        self._delegate_fn = delegate
        self._drain = drain
        self._words = words
        self._address = address
        self._factory = session_factory
        self._pump_seconds = pump_seconds
        self._on_end = on_end
        self._down = DiscordToGemini()
        self._up = GeminiToDiscord()
        self.session: Any = None
        self._pump: asyncio.Task | None = None
        self._drain_warned = False
        self._running: set[str] = set()
        self.ended = ""

    async def start(self) -> None:
        handlers = LiveHandlers(on_audio=self._on_audio, on_interrupted=self._on_interrupted,
                                on_tool_call=self._on_tool_call, on_closed=self._on_closed)
        self.session = self._factory(self.config, handlers)
        await self.session.start()
        self._pump = asyncio.ensure_future(self._pump_loop())

    async def stop(self, why: str = "") -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        session, self.session = self.session, None
        if session is not None:
            try:
                await session.close()
            except Exception:
                log.debug("talk-with-hermes: session close failed", exc_info=True)
        self.playback.clear()
        self.ended = self.ended or why or "stopped"

    @property
    def live(self) -> bool:
        return self.session is not None and not self.ended

    # -- audio -----------------------------------------------------------------

    async def _pump_loop(self) -> None:
        failures = 0
        try:
            while self.session is not None:
                await asyncio.sleep(self._pump_seconds)
                try:
                    pcm = self._drain()
                    failures = 0
                except Exception:
                    # The receiver is the host's: a rename upstream lands here.
                    if not self._drain_warned:
                        self._drain_warned = True
                        log.warning("talk-with-hermes: reading the Discord voice receiver failed", exc_info=True)
                    failures += 1
                    if failures >= DRAIN_FAILURES_TO_END:
                        self._on_closed("la réception audio de Discord ne répond plus")
                        return
                    continue
                if pcm and self.session is not None:
                    await self.session.send_audio(self._down.feed(pcm))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("talk-with-hermes: Discord audio pump stopped", exc_info=True)

    def _on_audio(self, pcm24_mono: bytes) -> None:
        self.playback.feed(self._up.feed(pcm24_mono))

    def _on_interrupted(self) -> None:
        self.playback.clear()
        self._up.reset()

    def _on_closed(self, reason: str) -> None:
        self.ended = reason or "closed"
        if self._on_end is not None:
            try:
                self._on_end(self.ended)
            except Exception:
                log.debug("talk-with-hermes: on_end failed", exc_info=True)

    # -- the agent ---------------------------------------------------------------

    async def _on_tool_call(self, call_id: str, request: str) -> str:
        # A raised error reaches the voice as the session's failure answer
        # (DELEGATION_FAILED), as on Desktop: never as a run of the agent.
        if not call_id or call_id in self._running:
            raise RuntimeError("identifiant de demande vide ou déjà utilisé")
        if len(self._running) >= MAX_RUNNING:
            raise RuntimeError("trop de demandes en cours, attends la réponse de l'agent")
        try:
            request = apply_words(request, self._words)
        except Exception:
            log.warning("talk-with-hermes: dictionary not applied", exc_info=True)
        self._running.add(call_id)
        try:
            return await self._delegate_fn(persona.delegated_prompt(request, self._address))
        finally:
            self._running.discard(call_id)


async def open_call(profile: str, *, drain: Callable[[], bytes], on_end: Callable[[str], Any] | None = None,
                    session_factory: Callable[..., Any] = services.open_session) -> DiscordCall:
    """The voice keys and voice settings come from one place, the profile
    where the plugin is installed (its Desktop settings page). Only who
    speaks (name, intro, style) comes from the channel's agent, and the
    agent's local door key is created there if it is missing."""

    from talk_with_hermes import discord_host, keys

    owner = keys.owner_profile()
    async with discord_host.profile_scope(owner) as owner_home:
        settings = settings_of(owner_home)
        service = services.current(settings)
        api_key = keys.voice_key(service.key_env)
        prefs = PrefsStore(owner_home / "plugin-state" / "talk-with-hermes" / "prefs.json").load()
    if not api_key:
        raise RuntimeError(f"il manque la clé {service.label} dans les réglages du plugin (profil {owner})")
    async with discord_host.profile_scope(profile) as home:
        server_key, created = keys.ensure_door_key()
    delegate = ApiDelegate(discord_host.api_server_base(), profile, server_key)
    if created and not await keys.wait_door_open(delegate.probe):
        raise RuntimeError(f"la porte locale de l'agent {profile} ne s'est pas ouverte")
    config = build_config(settings, api_key, prefs, identity_mod.load(home))
    return DiscordCall(config=config, delegate=delegate, drain=drain, words=prefs.words,
                       address=prefs.address, session_factory=session_factory, on_end=on_end)
