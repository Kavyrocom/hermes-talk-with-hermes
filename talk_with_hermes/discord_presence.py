"""Walk into the agent's voice channel, the agent comes. Walk out, it goes.

A voice channel listed in `gateway.profile_routes` (the table that already
sends each text channel to its agent) is that agent's voice channel. When an
allowed member is in it, the bot joins and the call starts; when no allowed
member is left for a few seconds, the call ends and the bot leaves.

One voice connection per server (a Discord limit): the first routed channel
with someone in it wins, and a host `/voice join` already running is left
alone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

POLL_SECONDS = 1.5
#: Ticks with nobody allowed in the channel before leaving (covers a brief drop).
EMPTY_TICKS = 3


def retry_delay(failures: int) -> float:
    return min(30.0 * 2 ** max(failures - 1, 0), 600.0)


class Presence:
    def __init__(self, adapter: Any, *, host: Any = None, opener: Callable[..., Any] | None = None) -> None:
        from talk_with_hermes import discord_call, discord_host

        self.adapter = adapter
        self.host = host or discord_host
        self.opener = opener or discord_call.open_call
        self.calls: dict[int, dict[str, Any]] = {}  # guild id -> {call, channel, profile, empty}
        #: channel id -> (monotonic time of the last failed start, consecutive failures)
        self._failed: dict[int, tuple[float, int]] = {}
        self._busy: set[int] = set()

    async def tick(self) -> None:
        self.adapter = self.host.discord_adapter() or self.adapter  # a reconnect may rebuild it
        routes = self.host.voice_routes(self.adapter)
        by_guild: dict[int, list[tuple[Any, str]]] = {}
        for channel, profile in routes:
            by_guild.setdefault(channel.guild.id, []).append((channel, profile))
        for guild_id, entry in list(self.calls.items()):
            await self._keep_or_end(guild_id, entry)
        for guild_id, candidates in by_guild.items():
            if guild_id in self.calls or guild_id in self._busy:
                continue
            if self._host_in_voice(guild_id):
                continue  # someone used /voice join: not ours to take over
            for channel, profile in candidates:
                if not self.host.allowed_members(self.adapter, channel):
                    self._failed.pop(channel.id, None)  # walking in again retries at once
                    continue
                if self._may_retry(channel.id):
                    await self._begin(channel, profile)
                    break

    async def summon(self, channel: Any, profile: str) -> str:
        """`/talk`: this agent, in the caller's voice channel, now."""

        guild_id = channel.guild.id
        entry = self.calls.get(guild_id)
        if entry is not None:
            if entry["channel"].id == channel.id and entry["profile"] == profile:
                return "Je suis déjà dans ton salon vocal."
            await self.end(guild_id, "remplacé par /talk")
        elif self._host_in_voice(guild_id):
            await self.host.leave(self.adapter, guild_id)  # /voice join's cascade gives way
        self._failed.pop(channel.id, None)
        await self._begin(channel, profile)
        if guild_id in self.calls:
            return "J'arrive dans ton salon vocal."
        return "Je n'ai pas pu démarrer la voix. Le détail est dans les logs du gateway."

    def _host_in_voice(self, guild_id: int) -> bool:
        clients = getattr(self.adapter, "_voice_clients", {}) or {}
        vc = clients.get(guild_id)
        try:
            return vc is not None and vc.is_connected()
        except Exception:
            return False

    def _may_retry(self, channel_id: int) -> bool:
        """30 s after a first failure, then twice as long each time, up to 10 min:
        a channel that cannot connect never makes the bot enter and leave in a loop."""

        failed = self._failed.get(channel_id)
        if failed is None:
            return True
        last, count = failed
        return time.monotonic() - last > retry_delay(count)

    def _record_failure(self, channel_id: int) -> None:
        self._failed[channel_id] = (time.monotonic(), self._failed.get(channel_id, (0.0, 0))[1] + 1)

    async def _begin(self, channel: Any, profile: str) -> None:
        guild_id = channel.guild.id
        self._busy.add(guild_id)
        try:
            vc, receiver = await self.host.join(self.adapter, channel)
            state: dict[str, Any] = {"speaker": None}

            def drain() -> bytes:
                state["speaker"], pcm = self.host.drain(receiver, self.adapter, channel, state["speaker"])
                return pcm

            call = None
            try:
                call = await self.opener(profile, drain=drain)
                await call.start()
                self._play(vc, call)
            except Exception:
                if call is not None:
                    await self._stop_quietly(call, "la voix n'a pas démarré")
                await self.host.leave(self.adapter, guild_id)
                raise
            self.calls[guild_id] = {"call": call, "channel": channel, "profile": profile, "empty": 0}
            self._failed.pop(channel.id, None)
            log.info("talk-with-hermes: voice call started (profile=%s)", profile)
        except Exception as exc:
            self._record_failure(channel.id)
            log.warning("talk-with-hermes: voice call not started (profile=%s): %s: %s", profile, type(exc).__name__, exc)
        finally:
            self._busy.discard(guild_id)

    @staticmethod
    def _play(vc: Any, call: Any) -> None:
        from talk_with_hermes.discord_call import discord_source

        try:
            if vc.is_playing():
                vc.stop()
        except Exception:
            pass
        vc.play(discord_source(call.playback))

    async def _keep_or_end(self, guild_id: int, entry: dict[str, Any]) -> None:
        call, channel = entry["call"], entry["channel"]
        if not self._host_in_voice(guild_id):
            await self.end(guild_id, "la connexion vocale est tombée", leave=False)
            return
        if call.ended:
            # The call ended by itself with someone still there: entering again at
            # once would repeat the same end. The usual backoff applies (/talk skips it).
            self._record_failure(channel.id)
            await self.end(guild_id, call.ended)
            return
        if self.host.allowed_members(self.adapter, channel):
            entry["empty"] = 0
            return
        entry["empty"] += 1
        if entry["empty"] >= EMPTY_TICKS:
            await self.end(guild_id, "plus personne dans le salon")

    @staticmethod
    async def _stop_quietly(call: Any, why: str) -> None:
        """A provider that errors on the way out never keeps the bot in the channel."""

        try:
            await call.stop(why)
        except Exception:
            log.warning("talk-with-hermes: voice call did not stop cleanly", exc_info=True)

    async def end(self, guild_id: int, why: str, *, leave: bool = True) -> None:
        entry = self.calls.pop(guild_id, None)
        if entry is None:
            return
        await self._stop_quietly(entry["call"], why)
        if leave:
            try:
                await self.host.leave(self.adapter, guild_id)
            except Exception:
                log.debug("talk-with-hermes: leave failed", exc_info=True)
        log.info("talk-with-hermes: voice call ended (profile=%s): %s", entry["profile"], why)

    async def stop_all(self, why: str = "stopped") -> None:
        for guild_id in list(self.calls):
            await self.end(guild_id, why)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.warning("talk-with-hermes: presence tick failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
        await self.stop_all("arrêt du plugin")


PRESENCE: dict[str, Any] = {}
#: The presence running for each Discord adapter, by the adapter's id. The
#: package is imported once per process (by its top-level name), so this
#: registry is process-wide; nothing is written onto Hermes's own objects.
_CLAIMS: dict[int, "Presence"] = {}


def claim(adapter: Any, presence: "Presence") -> "Presence":
    """The presence that runs for this adapter: an existing one wins. Only
    called on the adapter's own loop, so check-then-set cannot race."""

    running = _CLAIMS.get(id(adapter))
    if running is not None and running.adapter is adapter:
        return running
    _CLAIMS[id(adapter)] = presence
    return presence


def release(adapter: Any, presence: "Presence") -> None:
    """Forget the claim, if `presence` is the one holding it."""

    if _CLAIMS.get(id(adapter)) is presence:
        del _CLAIMS[id(adapter)]


def client_loop(adapter: Any) -> Any:
    """The gateway Discord client's event loop, or None (reported once) when
    the client no longer has the shape this plugin knows."""

    try:
        return adapter._client.loop
    except Exception:
        from talk_with_hermes.discord_host import warn_once

        warn_once("loop", "talk-with-hermes: the gateway's Discord client has no event loop, Discord voice is off")
        return None


def start_in_gateway() -> None:
    """Called at register(). The gateway's Discord client is not up yet: a
    daemon thread waits for it, then runs Presence on the client's own loop.
    Outside a gateway (Desktop, CLI) it gives up quietly."""

    if PRESENCE.get("thread"):
        return

    def wait_then_run() -> None:
        from talk_with_hermes import discord_host

        deadline = time.monotonic() + 600
        adapter = None
        while time.monotonic() < deadline:
            adapter = discord_host.discord_adapter()
            if adapter is not None and discord_host.client_ready(adapter):
                loop = client_loop(adapter)
                if loop is None:
                    PRESENCE.pop("thread", None)  # a plugin reload may try again
                    return
                presence = Presence(adapter)

                async def launch() -> None:
                    # One presence per adapter, even when several profiles enable the plugin.
                    running = claim(adapter, presence)
                    if running is not presence:
                        PRESENCE.update(presence=running)
                        return
                    stop = asyncio.Event()
                    PRESENCE.update(presence=presence, stop=stop)
                    try:
                        await presence.run(stop)
                    finally:
                        release(adapter, presence)

                asyncio.run_coroutine_threadsafe(launch(), loop)
                log.info("talk-with-hermes: Discord voice presence on")
                return
            if discord_host.runner() is None and time.monotonic() > deadline - 540:
                return  # not a gateway process
            time.sleep(2)
        if adapter is not None:  # a Discord adapter, never ready: say so, not nothing
            log.warning("talk-with-hermes: the gateway's Discord client was not ready after 10 min, Discord voice is off")
        PRESENCE.pop("thread", None)

    thread = threading.Thread(target=wait_then_run, name="talk-with-hermes-presence", daemon=True)
    PRESENCE["thread"] = thread
    thread.start()
