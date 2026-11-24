"""Everything this plugin touches inside the Hermes host for Discord voice.

One module on purpose: these are host internals (private attributes of the
Discord adapter and the gateway runner). `tests/test_discord_host.py` checks
they still exist on the installed host, so a host update breaks a test, not
a live call.
"""

from __future__ import annotations

import logging
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from talk_with_hermes.salon import _host_discord_adapter, gateway_runner

log = logging.getLogger(__name__)

#: Attributes of the Discord adapter this plugin relies on.
ADAPTER_ATTRS = ("_client", "_voice_clients", "_voice_receivers", "_voice_listen_tasks",
                 "join_voice_channel", "leave_voice_channel", "discard_pending_voice_input",
                 "_cancel_voice_timeout", "_is_allowed_user")
#: Attributes of the host's VoiceReceiver this plugin relies on.
RECEIVER_ATTRS = ("_lock", "_buffers", "_last_packet_time", "map_ssrc", "_running")


def runner() -> Any:
    return gateway_runner()


def discord_adapter() -> Any:
    return _host_discord_adapter()


def client_ready(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    try:
        return client is not None and client.is_ready()
    except Exception:
        return False


# -- which agent speaks in which voice channel --------------------------------

def profile_home(profile: str) -> Path:
    """A profile's home, laid out as Hermes lays it out: the root for
    `default`, `<root>/profiles/<id>` otherwise. A name that is not a valid
    profile id raises instead of becoming a path."""

    from hermes_constants import PROFILE_ID_RE, get_default_hermes_root

    root = Path(get_default_hermes_root())
    name = (profile or "").strip().lower()
    if not name or name == "default":
        return root
    if not PROFILE_ID_RE.fullmatch(name):
        raise ValueError(f"not a profile id: {profile!r}")
    return root / "profiles" / name


#: The gateway config last parsed: (path, mtime_ns, size) -> its contents.
_PARSED: dict[str, Any] = {}


def gateway_config() -> dict[str, Any]:
    """The gateway's own config.yaml, parsed again only when the file changes:
    it is read on the gateway's event loop. {} when unreadable."""

    path = profile_home("default") / "config.yaml"
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        if _PARSED.get("stamp") != stamp:
            from hermes_yaml import safe_load

            config = safe_load(path.read_text(encoding="utf-8")) or {}
            _PARSED.update(stamp=stamp, config=config if isinstance(config, dict) else {})
        return _PARSED["config"]
    except Exception:
        log.debug("talk-with-hermes: gateway config unreadable", exc_info=True)
        _PARSED.clear()
        return {}


def profile_routes() -> list[Any]:
    """The routes the gateway parsed at startup, most specific first: the very
    list that sends each text channel to its agent. [] outside the gateway."""

    routes = getattr(getattr(runner(), "config", None), "profile_routes", None)
    return routes if isinstance(routes, list) else []


def _route(routes: list[Any], channel: Any) -> Any:
    """The route the gateway applies to this channel, with the gateway's own
    rule (`ProfileRoute.matches`): enabled, same server, same bot. A call serves
    everyone in the channel, so it matches as no one in particular: a route
    pinned to one `user_id` never opens one. The presence speaks through the
    gateway's own bot, the default profile's (`adapter_profile=None`)."""

    guild = getattr(getattr(channel, "guild", None), "id", None)
    for route in routes:
        if route.matches("discord", guild_id=None if guild is None else str(guild), chat_id=str(channel.id),
                         adapter_profile=None, user_id=None):
            return route
    return None


def voice_routes(adapter: Any) -> list[tuple[Any, str]]:
    """(voice channel, profile) for every voice channel the gateway routes to an agent."""

    client = getattr(adapter, "_client", None)
    if client is None:
        return []
    try:
        routes = profile_routes()
        found, seen = [], set()
        for route in routes:
            if route.platform != "discord" or not route.chat_id or route.chat_id in seen:
                continue
            seen.add(route.chat_id)
            try:
                channel = client.get_channel(int(route.chat_id))
            except (TypeError, ValueError):
                continue
            if channel is None or not hasattr(channel, "members") or not _is_voice(channel):
                continue
            chosen = _route(routes, channel)
            if chosen is not None:
                found.append((channel, chosen.profile))
        return found
    except Exception:
        warn_once("routes", "talk-with-hermes: the gateway's profile routes cannot be read; Discord voice is off")
        return []


def route_of(channel: Any) -> str | None:
    """The agent the gateway routes this channel to, for the whole channel."""

    try:
        route = _route(profile_routes(), channel)
    except Exception:
        warn_once("routes", "talk-with-hermes: the gateway's profile routes cannot be read; Discord voice is off")
        return None
    return route.profile if route is not None else None


def _is_voice(channel: Any) -> bool:
    try:
        import discord

        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
    except Exception:
        return type(channel).__name__ in {"VoiceChannel", "StageChannel"}


# -- who may drive the agent ---------------------------------------------------

#: Host-contract breaks already reported: each is logged once, not every tick.
_WARNED: set[str] = set()


def warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(message, *args, exc_info=True)


def allowed(adapter: Any, member_id: int, guild: Any) -> bool:
    """The host's own allowlist (users or roles). No allowlist: nobody, for voice.
    A check that raises also means nobody, and says why once."""

    try:
        return adapter._is_allowed_user(str(member_id), guild=guild, is_dm=False) is True
    except Exception:
        warn_once("allowed", "talk-with-hermes: the Discord allowlist check failed, nobody can call by voice")
        return False


def _others(adapter: Any, channel: Any) -> list[Any]:
    """The channel's members other than this bot, people and other bots."""
    bot_id = getattr(getattr(getattr(adapter, "_client", None), "user", None), "id", None)
    return [m for m in getattr(channel, "members", []) or [] if m.id != bot_id]


def allowed_members(adapter: Any, channel: Any) -> list[int]:
    return [m.id for m in _others(adapter, channel)
            if not getattr(m, "bot", False) and allowed(adapter, m.id, channel.guild)]


# -- profile scope (its secrets, its settings) ---------------------------------

@asynccontextmanager
async def profile_scope(profile: str) -> AsyncIterator[Path]:
    """The profile's runtime scope (its secrets, its settings), the default
    profile included: on a multiplexed gateway a secret read outside a scope
    raises, and Hermes binds its own default root the same way."""

    home = profile_home(profile)
    from gateway.run import _async_profile_runtime_scope

    async with _async_profile_runtime_scope(home):
        yield home


def _loaded_api_server_extra() -> dict[str, Any] | None:
    """The API server settings of the config the gateway loaded at startup,
    API_SERVER_HOST and API_SERVER_PORT applied. None outside the gateway."""

    platforms = getattr(getattr(runner(), "config", None), "platforms", None)
    if not isinstance(platforms, dict):
        return None
    for platform, platform_config in platforms.items():
        if getattr(platform, "value", platform) == "api_server":
            extra = getattr(platform_config, "extra", None)
            return extra if isinstance(extra, dict) else {}
    return None


def _listen_address(extra: dict[str, Any]) -> tuple[Any, Any]:
    """Hermes' own reading of the settings, the one its API server binds with,
    taken from the loaded module (never imported from here). The same rule
    when the API server is not running."""

    import os
    import sys

    listen_address: Any = getattr(sys.modules.get("gateway.platforms.api_server"), "listen_address", None)
    if listen_address is not None:
        host, port = listen_address(extra)
        return host, port
    port = extra.get("port")
    return (extra.get("host", os.getenv("API_SERVER_HOST", "127.0.0.1")),
            os.getenv("API_SERVER_PORT", "8642") if port is None else port)


def api_server_base() -> str:
    """Where the gateway's API server listens, read as the gateway binds it.
    config.yaml alone only outside the gateway."""

    try:
        extra = _loaded_api_server_extra()
        if extra is None:
            config = gateway_config()
            extra = ((config.get("platforms") or {}).get("api_server") or {}).get("extra") or {}
        host, port = _listen_address(extra)
        host, port = str(host or ""), int(port)
    except Exception:
        host, port = "127.0.0.1", 8642
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


# -- the voice connection ------------------------------------------------------

async def join(adapter: Any, channel: Any) -> tuple[Any, Any]:
    """Join with the host (it owns the connection and the decryption), then
    take the audio: its own speech-to-text loop and inactivity timer stop,
    or the agent would answer twice and the bot leave mid-call."""

    joined = await adapter.join_voice_channel(channel)
    if joined is False:
        raise RuntimeError("le host n'a pas pu rejoindre le salon vocal")
    guild_id = channel.guild.id
    task = adapter._voice_listen_tasks.pop(guild_id, None)
    if task is not None:
        task.cancel()
    adapter._cancel_voice_timeout(guild_id)
    receiver = adapter._voice_receivers.get(guild_id)
    if receiver is None:
        await adapter.leave_voice_channel(guild_id)
        raise RuntimeError("le host n'a pas démarré la réception audio")
    announced(receiver)
    return adapter._voice_clients[guild_id], receiver


async def leave(adapter: Any, guild_id: int) -> None:
    """Drop what the receiver still holds first: on leave, the host would
    transcribe it and start an agent turn nobody asked for. Without the
    host's helper, the buffers are emptied here and the miss is logged."""

    try:
        adapter.discard_pending_voice_input(guild_id)
    except Exception as exc:
        log.warning("talk-with-hermes: host discard_pending_voice_input failed (%s), "
                    "emptying the voice buffers directly", type(exc).__name__)
        receiver = getattr(adapter, "_voice_receivers", {}).get(guild_id)
        if receiver is not None:
            try:
                with receiver._lock:
                    receiver._buffers.clear()
                    receiver._last_packet_time.clear()
            except Exception:
                log.warning("talk-with-hermes: could not empty the voice buffers", exc_info=True)
    await adapter.leave_voice_channel(guild_id)


_ANNOUNCED: "weakref.WeakKeyDictionary[Any, dict[int, int]]" = weakref.WeakKeyDictionary()


def announced(receiver: Any) -> dict[int, int]:
    """The SSRCs Discord itself named (SPEAKING), recorded as Hermes' hook hands
    them to the receiver's `map_ssrc`. Bound at join, before the call starts: a
    SPEAKING sent earlier is missed, and until Discord sends one again that
    voice counts only for someone alone with the bot."""

    seen = _ANNOUNCED.get(receiver)
    if seen is not None:
        return seen
    seen = _ANNOUNCED[receiver] = {}
    original = getattr(receiver, "map_ssrc", None)
    if not callable(original):
        warn_once("map_ssrc", "talk-with-hermes: Hermes no longer reports SPEAKING through map_ssrc, "
                  "a voice counts only for someone alone with the bot")
        return seen

    lock = receiver._lock  # drain copies `seen` under it; Hermes' map_ssrc takes it after us

    def map_ssrc(ssrc: int, user_id: int) -> None:
        with lock:
            seen[int(ssrc)] = int(user_id)
        original(ssrc, user_id)

    receiver.map_ssrc = map_ssrc
    return seen


def drain(receiver: Any, adapter: Any, channel: Any, speaker: int | None) -> tuple[int | None, bytes]:
    """Take the 48 kHz stereo PCM gathered since the last call, for one allowed
    speaker. The first allowed speaker heard keeps the floor until they stop
    sending; others are dropped (one operator drives one agent).

    A voice counts when Discord named it (SPEAKING, see `announced`), or for
    someone alone with the bot. Hermes' own guess (`_infer_user_for_ssrc`) picks
    the sole allowed member even with a guest in the channel and keeps it in the
    receiver's map; with a DAVE session it guesses in the packet path too, and a
    frame in clear gets through. That map is never read here."""

    others = _others(adapter, channel)
    alone = others[0].id if len(others) == 1 else 0
    guild = getattr(channel, "guild", None)
    named = announced(receiver)
    with receiver._lock:
        items = list(receiver._buffers.items())
        mapping = dict(named)
    chosen: tuple[int, int] | None = None
    for ssrc, buf in items:
        if not buf:
            continue
        user = mapping.get(ssrc) or alone
        if not user or not allowed(adapter, user, guild):
            with receiver._lock:
                receiver._buffers.pop(ssrc, None)
                receiver._last_packet_time.pop(ssrc, None)
            continue
        if chosen is None or user == speaker:
            chosen = (ssrc, user)
    if chosen is None:
        return speaker, b""
    ssrc, user = chosen
    with receiver._lock:
        pcm = bytes(receiver._buffers.pop(ssrc, b""))
        receiver._last_packet_time.pop(ssrc, None)
        for other, _ in items:  # one floor: the others' audio is not queued for later
            if other != ssrc:
                receiver._buffers.pop(other, None)
                receiver._last_packet_time.pop(other, None)
    return user, pcm
