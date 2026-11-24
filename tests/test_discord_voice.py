"""Discord voice: host contract, audio both ways, delegation, auto join and leave."""

import asyncio
import dataclasses
import enum
import inspect
import json
import logging
import re
import threading
import types
from collections import defaultdict

import numpy as np
import pytest

from talk_with_hermes import discord_call, discord_host, discord_presence, persona
from talk_with_hermes.discord_call import FRAME_BYTES, SILENCE, ApiDelegate, DiscordCall, Playback
from talk_with_hermes.gemini_live import LiveConfig
from talk_with_hermes.prefs import Word


# -- the host internals we rely on still exist ---------------------------------

def host_module(name):
    """Hermes's own module. Outside Hermes the contract test is skipped, unless
    TWH_HOST_CONTRACT=1 asks for it: then a missing host is a failure, never a
    green suite (run it with the gateway's interpreter, see the README)."""

    import importlib
    import os

    if os.environ.get("TWH_HOST_CONTRACT") == "1":
        return importlib.import_module(name)
    return pytest.importorskip(name)


def test_host_contract_on_the_installed_hermes():
    adapter_mod = host_module("plugins.platforms.discord.adapter")
    source = open(adapter_mod.__file__, encoding="utf-8").read()
    for name in discord_host.ADAPTER_ATTRS:  # methods on the class, state set in __init__
        assert hasattr(adapter_mod.DiscordAdapter, name) or re.search(rf"self\.{name}\s*[:=]", source), name
    receiver = adapter_mod.VoiceReceiver(types.SimpleNamespace(), allowed_user_ids=set())
    for name in discord_host.RECEIVER_ATTRS:
        assert hasattr(receiver, name), name
    assert isinstance(receiver._buffers, defaultdict)
    run = host_module("gateway.run")
    assert callable(run._async_profile_runtime_scope) and callable(run._gateway_runner_ref)
    # Voice reads the routes the runner parsed at startup: runner.config.profile_routes.
    assert re.search(r"self\.config\s*=", inspect.getsource(run))
    assert "profile_routes" in {f.name for f in dataclasses.fields(host_module("gateway.config").GatewayConfig)}
    hermes_constants = host_module("hermes_constants")
    assert callable(hermes_constants.get_default_hermes_root) and hermes_constants.PROFILE_ID_RE.fullmatch("orion")
    assert callable(host_module("hermes_yaml").safe_load)
    config = host_module("hermes_cli.config")
    assert callable(config.get_env_value) and callable(config.save_env_value)
    assert callable(config.require_env_writable)


# -- which speaker is heard -----------------------------------------------------

class FakeReceiver:
    def __init__(self, mapping):
        self._lock = threading.Lock()
        self._buffers = defaultdict(bytearray)
        self._last_packet_time = {}
        self._ssrc_to_user = dict(mapping)  # Hermes' map: what Discord said and what it guessed, alike
        self._running = True

    def map_ssrc(self, ssrc, user_id):  # where Hermes' SPEAKING hook lands
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def push(self, ssrc, data):
        self._buffers[ssrc].extend(data)
        self._last_packet_time[ssrc] = 1.0


def announced_receiver(mapping):
    """Bound the way join() binds it, then Discord names each SSRC (SPEAKING)."""
    receiver = FakeReceiver({})
    discord_host.announced(receiver)
    for ssrc, user in mapping.items():
        receiver.map_ssrc(ssrc, user)
    return receiver


class FakeAdapter:
    def __init__(self, allowed, client=None):
        self.allowed = set(allowed)
        self._client = client

    def _is_allowed_user(self, user_id, guild=None, is_dm=False):
        return user_id in self.allowed


def test_only_an_allowed_speaker_is_heard_and_keeps_the_floor():
    receiver = announced_receiver({1: 111, 2: 222, 3: 333})
    adapter = FakeAdapter({"111", "333"})
    receiver.push(2, b"stranger")
    receiver.push(1, b"camille")
    speaker, pcm = discord_host.drain(receiver, adapter, None, None)
    assert (speaker, pcm) == (111, b"camille")
    assert 2 not in receiver._buffers  # the stranger's audio is dropped, never queued
    receiver.push(3, b"other")
    receiver.push(1, b"again")
    assert discord_host.drain(receiver, adapter, None, speaker) == (111, b"again")
    assert discord_host.drain(receiver, adapter, None, speaker) == (111, b"")  # the other was dropped too
    receiver.push(3, b"next")
    assert discord_host.drain(receiver, adapter, None, speaker) == (333, b"next")  # floor free: next speaker


def test_an_unannounced_voice_counts_only_for_someone_alone_with_the_bot():
    """Hermes guesses a voice Discord never announced (no SPEAKING) as the sole
    *allowed* member, even with a guest in the channel, and keeps the guess:
    the guest would speak as the operator."""

    class Guessing(FakeReceiver):
        def _infer_user_for_ssrc(self, ssrc):  # the host's guess, remembered
            self._ssrc_to_user[ssrc] = 111
            return 111

    def member(uid, bot=False):
        return types.SimpleNamespace(id=uid, bot=bot)

    adapter = FakeAdapter({"111"}, client=types.SimpleNamespace(user=member(1, bot=True)))
    receiver = Guessing({})
    with_guest = types.SimpleNamespace(guild=None, members=[member(1, True), member(111), member(222)])
    receiver.push(9, b"guest")
    assert discord_host.drain(receiver, adapter, with_guest, None) == (None, b"")
    assert 9 not in receiver._buffers and 9 not in receiver._ssrc_to_user  # dropped, never taken for the operator
    alone = types.SimpleNamespace(guild=None, members=[member(1, True), member(111)])
    receiver.push(9, b"camille")
    assert discord_host.drain(receiver, adapter, alone, None) == (111, b"camille")
    with_a_bot = types.SimpleNamespace(guild=None, members=[member(1, True), member(111), member(5, True)])
    receiver.push(7, b"music")
    assert discord_host.drain(receiver, adapter, with_a_bot, None) == (None, b"")  # another bot is someone too


def test_a_guess_hermes_made_on_its_own_never_names_the_speaker():
    """With a DAVE session, Hermes also guesses in its packet path, before drain
    runs: a voice Discord never announced lands in its map as the sole allowed
    member, guest or not, and a frame in clear gets through. Only Discord's own
    word (SPEAKING, through map_ssrc) names a speaker."""

    def member(uid, bot=False):
        return types.SimpleNamespace(id=uid, bot=bot)

    adapter = FakeAdapter({"111"}, client=types.SimpleNamespace(user=member(1, bot=True)))
    with_guest = types.SimpleNamespace(guild=None, members=[member(1, True), member(111), member(222)])
    receiver = FakeReceiver({9: 111})  # Hermes' guess for the guest's voice, already in its map
    receiver.push(9, b"guest")
    assert discord_host.drain(receiver, adapter, with_guest, None) == (None, b"")
    receiver.map_ssrc(7, 111)  # Discord names the operator's voice
    receiver.push(7, b"camille")
    receiver.push(9, b"guest again")
    assert discord_host.drain(receiver, adapter, with_guest, None) == (111, b"camille")
    assert 9 not in receiver._buffers  # the guess stays dropped, never queued


def test_on_the_installed_hermes_its_own_guess_never_names_the_speaker():
    """The same, with Hermes' real receiver: its guess, then its SPEAKING entry point."""

    adapter_mod = host_module("plugins.platforms.discord.adapter")

    def member(uid, bot=False):
        return types.SimpleNamespace(id=uid, bot=bot)

    bot = member(1, True)
    channel = types.SimpleNamespace(guild=None, members=[bot, member(111), member(222)])
    receiver = adapter_mod.VoiceReceiver(types.SimpleNamespace(channel=channel, user=bot), allowed_user_ids={"111"})
    adapter = FakeAdapter({"111"}, client=types.SimpleNamespace(user=bot))
    assert receiver._infer_user_for_ssrc(9) == 111  # the guest's voice, guessed as the operator
    receiver._buffers[9].extend(b"guest")
    assert discord_host.drain(receiver, adapter, channel, None) == (None, b"")
    receiver.map_ssrc(7, 111)
    receiver._buffers[7].extend(b"camille")
    assert discord_host.drain(receiver, adapter, channel, None) == (111, b"camille")
    # Every SPEAKING event goes through the receiver's map_ssrc, where join() records it.
    assert re.search(r"\.map_ssrc\(", inspect.getsource(adapter_mod.VoiceReceiver._install_speaking_hook))


def test_no_allowlist_means_nobody_and_a_broken_check_says_so(caplog):
    class Closed:
        def _is_allowed_user(self, *a, **k):
            raise RuntimeError("no gates")

    discord_host._WARNED.clear()
    with caplog.at_level(logging.WARNING, logger=discord_host.__name__):
        assert discord_host.allowed(Closed(), 1, None) is False
        assert discord_host.allowed(Closed(), 2, None) is False
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "allowlist" in warned[0].getMessage()  # once, not every 1.5 s


def test_a_gateway_client_without_a_loop_is_reported_not_fatal(caplog):
    discord_host._WARNED.clear()
    adapter = types.SimpleNamespace(_client=types.SimpleNamespace())
    with caplog.at_level(logging.WARNING, logger=discord_presence.__name__):
        assert discord_presence.client_loop(adapter) is None
    assert any("Discord voice is off" in r.getMessage() for r in caplog.records)
    loop = object()
    assert discord_presence.client_loop(types.SimpleNamespace(_client=types.SimpleNamespace(loop=loop))) is loop


def test_the_agent_door_is_the_gateway_api_server_on_loopback(tmp_path, monkeypatch, host_yaml):
    (tmp_path / "config.yaml").write_text(
        "platforms:\n  api_server:\n    extra: {host: 0.0.0.0, port: 9000}\n", encoding="utf-8")
    monkeypatch.setattr(discord_host, "profile_home", lambda name: tmp_path)
    monkeypatch.setattr(discord_host, "runner", lambda: None)  # outside the gateway: config.yaml
    assert discord_host.api_server_base() == "http://127.0.0.1:9000"


# -- which voice channel is whose -----------------------------------------------

#: Discord rows of `gateway.profile_routes`, in file order.
ROUTE_ROWS = [
    {"name": "any-server", "platform": "discord", "chat_id": "14", "profile": "cephalon"},
    {"name": "vocal", "platform": "discord", "guild_id": "1", "chat_id": "10", "profile": "iris"},
    {"name": "off", "platform": "discord", "guild_id": "1", "chat_id": "11", "profile": "lyra", "enabled": False},
    {"name": "other-bot", "platform": "discord", "guild_id": "1", "chat_id": "12", "profile": "orion",
     "bot_profile": "orion"},
    {"name": "one-user", "platform": "discord", "guild_id": "1", "chat_id": "13", "profile": "vega", "user_id": "7"},
    {"name": "this-server", "platform": "discord", "guild_id": "1", "chat_id": "14", "profile": "nova"},
    {"name": "other-server", "platform": "discord", "guild_id": "2", "chat_id": "16", "profile": "orion"},
    {"name": "text", "platform": "discord", "guild_id": "1", "chat_id": "15", "profile": "iris"},
]


def server_channels():
    guild = types.SimpleNamespace(id=1)
    return {cid: types.SimpleNamespace(id=cid, guild=guild, members=[], kind="text" if cid == 15 else "voice")
            for cid in (10, 11, 12, 13, 14, 15, 16)}


def run_gateway_with(monkeypatch, tmp_path, routes):
    """A gateway as it runs: the routes it parsed at startup on its runner, the
    same rows still in its config.yaml."""

    monkeypatch.setattr(discord_host, "runner",
                        lambda: types.SimpleNamespace(config=types.SimpleNamespace(profile_routes=routes)))
    (tmp_path / "config.yaml").write_text(json.dumps({"gateway": {"profile_routes": ROUTE_ROWS}}), encoding="utf-8")
    monkeypatch.setattr(discord_host, "profile_home", lambda name: tmp_path)
    monkeypatch.setattr(discord_host, "_is_voice", lambda channel: channel.kind == "voice")


def test_a_voice_channel_goes_to_the_agent_the_gateway_routes_it_to(tmp_path, monkeypatch, host_yaml):
    """The gateway's own routes and its own matcher: a disabled row, a row for
    another profile's bot, a row pinned to one person or to another server
    never opens a call, and of two rows for one channel the most specific wins."""

    routing = host_module("gateway.profile_routing")
    # What the host's parser hands the runner: most specific first.
    routes = sorted((routing.ProfileRoute(**row) for row in ROUTE_ROWS), key=lambda r: r.specificity, reverse=True)
    run_gateway_with(monkeypatch, tmp_path, routes)
    channels = server_channels()
    adapter = types.SimpleNamespace(_client=types.SimpleNamespace(get_channel=channels.get))

    served = sorted((channel.id, profile) for channel, profile in discord_host.voice_routes(adapter))
    assert served == [(10, "iris"), (14, "nova")]
    assert [discord_host.route_of(channels[cid]) for cid in (10, 11, 12, 13, 14, 16)] == [
        "iris", None, None, None, "nova", None]


def test_voice_and_text_pick_the_same_route_on_the_installed_hermes(monkeypatch):
    """The host's own parser and matcher, end to end: for every channel, voice
    picks the route the gateway picks for a message from no one in particular.
    Needs the real Hermes: its parser checks profile names with
    `hermes_cli.profiles` and silently skips every route when that is missing."""

    host_module("hermes_cli.profiles")
    routing = host_module("gateway.profile_routing")
    routes = routing.parse_profile_routes(ROUTE_ROWS)
    assert len(routes) == len(ROUTE_ROWS)  # none skipped, or the comparison below proves nothing
    monkeypatch.setattr(discord_host, "runner",
                        lambda: types.SimpleNamespace(config=types.SimpleNamespace(profile_routes=routes)))
    for channel in server_channels().values():
        expected = routing.match_profile_route(routes, "discord", guild_id=str(channel.guild.id),
                                               chat_id=str(channel.id), adapter_profile=None, user_id=None)
        assert discord_host.route_of(channel) == (expected.profile if expected else None), channel.id


def test_routes_of_another_shape_turn_voice_off_with_one_log_line(monkeypatch, caplog):
    """A host whose routes changed shape: no call and no crash every 1.5 s, one log line."""

    discord_host._WARNED.clear()
    changed = types.SimpleNamespace(platform="discord", chat_id="10", matches=None)
    monkeypatch.setattr(discord_host, "runner",
                        lambda: types.SimpleNamespace(config=types.SimpleNamespace(profile_routes=[changed])))
    monkeypatch.setattr(discord_host, "_is_voice", lambda channel: channel.kind == "voice")
    channels = server_channels()
    adapter = types.SimpleNamespace(_client=types.SimpleNamespace(get_channel=channels.get))
    with caplog.at_level(logging.WARNING, logger=discord_host.__name__):
        for _ in range(3):
            assert discord_host.voice_routes(adapter) == []
            assert discord_host.route_of(channels[10]) is None
    assert len([r for r in caplog.records if "profile routes" in r.getMessage()]) == 1
    monkeypatch.setattr(discord_host, "runner", lambda: None)  # outside the gateway
    assert discord_host.profile_routes() == [] and discord_host.voice_routes(adapter) == []


# -- audio both ways ------------------------------------------------------------

def test_playback_never_ends_and_clears_on_interrupt():
    playback = Playback()
    assert playback.read() == SILENCE
    playback.feed(b"\x01\x00" * (FRAME_BYTES // 2 + 10))
    assert len(playback.read()) == FRAME_BYTES
    tail = playback.read()
    assert len(tail) == FRAME_BYTES and tail[:20] == b"\x01\x00" * 10 and tail[20:] == SILENCE[20:]
    playback.feed(b"\x01" * FRAME_BYTES * 3)
    playback.clear()
    assert playback.read() == SILENCE


class FakeSession:
    def __init__(self, config, handlers):
        self.config, self.handlers = config, handlers
        self.sent = []
        self.closed = False

    async def start(self):
        pass

    async def send_audio(self, pcm):
        self.sent.append(pcm)

    async def close(self):
        self.closed = True


def cfg():
    return LiveConfig(api_key="k", model="gemini-3.8-live", voice="Puck")


def test_call_moves_audio_both_ways_and_delegates_with_the_dictionary():
    asked = []

    async def delegate(prompt):
        asked.append(prompt)
        return "Trois ventes."

    chunks = [np.full(4800 * 2, 1000, dtype="<i2").tobytes()]  # 50 ms of 48 kHz stereo

    async def run():
        made = {}

        def factory(config, handlers):
            made["s"] = FakeSession(config, handlers)
            return made["s"]

        call = DiscordCall(config=cfg(), delegate=delegate, drain=lambda: chunks.pop() if chunks else b"",
                           words=(Word("Kavyro", ("cavyro",)),), session_factory=factory, pump_seconds=0.001)
        await call.start()
        await asyncio.sleep(0.02)
        session = made["s"]
        session.handlers.on_audio(np.full(2400, 500, dtype="<i2").tobytes())  # 100 ms at 24 kHz
        queued = call.playback.queued
        answer = await session.handlers.on_tool_call("c1", "ventes cavyro ?")
        session.handlers.on_interrupted()
        after = call.playback.queued
        await call.stop("fin")
        return session, queued, answer, after, call

    session, queued, answer, after, call = asyncio.run(run())
    assert sum(len(p) for p in session.sent) == 1600 * 2  # 48 kHz stereo -> 16 kHz mono
    assert queued == 4800 * 2 * 2  # 24 kHz mono -> 48 kHz stereo
    assert answer == "Trois ventes." and after == 0
    assert asked == [persona.delegated_prompt("ventes Kavyro ?", "tu")]
    assert session.closed and call.ended == "fin"


def test_a_receiver_that_cannot_be_read_ends_the_call_with_a_reason(caplog):
    """A host rename turns drain() into an AttributeError: the call must not
    stay up hearing nothing, with nothing logged above DEBUG."""

    def drain():
        raise AttributeError("'VoiceReceiver' object has no attribute '_buffers'")

    ended = []

    async def unused(prompt):
        return ""

    async def run():
        call = DiscordCall(config=cfg(), delegate=unused, drain=drain, session_factory=FakeSession,
                           pump_seconds=0.0005, on_end=ended.append)
        await call.start()
        for _ in range(400):
            if call.ended:
                break
            await asyncio.sleep(0.001)
        await call.stop("fin")
        return call

    with caplog.at_level(logging.WARNING, logger=discord_call.__name__):
        call = asyncio.run(run())
    assert "réception audio" in call.ended and ended == [call.ended]
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and warned[0].exc_info  # once, with the traceback


def test_a_turn_with_many_talk_calls_runs_at_most_three_agent_requests():
    """As on Desktop: past three running requests, and for an empty or repeated
    call id, the voice gets a spoken refusal, never another agent run."""

    from talk_with_hermes import desktop_bridge

    started = []

    async def run():
        release = asyncio.Event()

        async def delegate(prompt):
            started.append(prompt)
            await release.wait()
            return "fait"

        call = DiscordCall(config=cfg(), delegate=delegate, drain=lambda: b"", session_factory=FakeSession)
        first = [asyncio.create_task(call._on_tool_call(f"c{i}", f"demande {i}")) for i in range(3)]
        await asyncio.sleep(0)
        refused = []
        for call_id in ("c3", "", "c0"):
            with pytest.raises(RuntimeError) as err:
                await asyncio.wait_for(call._on_tool_call(call_id, "encore"), 1)
            refused.append(str(err.value))
        release.set()
        answers = await asyncio.gather(*first)
        return answers, refused, await call._on_tool_call("c3", "après")  # the budget frees up

    answers, refused, again = asyncio.run(run())
    assert answers == ["fait"] * 3 and again == "fait" and len(started) == 4
    assert "trop de demandes" in refused[0] and "déjà utilisé" in refused[1] and "déjà utilisé" in refused[2]
    assert discord_call.MAX_RUNNING == desktop_bridge.MAX_WAITING


def test_api_delegate_keeps_one_session_and_speaks_refusals():
    seen = []

    async def post(url, headers, body, timeout):
        seen.append((url, dict(headers), body))
        if len(seen) == 3:
            return 401, {}, {}
        return 200, {"X-Hermes-Session-Id": "s1"}, {"choices": [{"message": {"content": f"r{len(seen)}"}}]}

    delegate = ApiDelegate("http://127.0.0.1:8642", "iris", "key", post=post)

    async def run():
        first, second = await delegate("a"), await delegate("b")
        with pytest.raises(RuntimeError, match="porte locale"):
            await delegate("c")
        return first, second

    assert asyncio.run(run()) == ("r1", "r2")
    assert seen[0][0] == "http://127.0.0.1:8642/p/iris/v1/chat/completions"
    assert "X-Hermes-Session-Id" not in seen[0][1] and seen[1][1]["X-Hermes-Session-Id"] == "s1"
    assert seen[0][2]["messages"] == [{"role": "user", "content": "a"}] and seen[0][2]["stream"] is False
    assert ApiDelegate("http://h:1", "default", "k").url == "http://h:1/v1/chat/completions"


# -- walk in, the agent comes; walk out, it goes ---------------------------------

class Guild:
    id = 7


class Channel:
    def __init__(self, cid, members):
        self.id, self.guild, self.members = cid, Guild(), members


class VC:
    def __init__(self):
        self.played = []
        self.connected = True

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return bool(self.played)

    def stop(self):
        pass

    def play(self, source):
        self.played.append(source)


class FakeCall:
    def __init__(self):
        self.playback = Playback()
        self.ended = ""
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self, why=""):
        self.ended = self.ended or why


class Host:
    def __init__(self, channel):
        self.channel = channel
        self.adapter = types.SimpleNamespace(_voice_clients={})
        self.log = []
        self.present = [1]

    def discord_adapter(self):
        return self.adapter

    def voice_routes(self, adapter):
        return [(self.channel, "iris")]

    def allowed_members(self, adapter, channel):
        return list(self.present)

    async def join(self, adapter, channel):
        self.log.append("join")
        vc = VC()
        adapter._voice_clients[channel.guild.id] = vc
        return vc, FakeReceiver({})

    async def leave(self, adapter, guild_id):
        self.log.append("leave")
        adapter._voice_clients.pop(guild_id, None)

    def drain(self, receiver, adapter, guild, speaker):
        return speaker, b""


def test_presence_joins_on_entry_and_leaves_after_a_grace(monkeypatch):
    monkeypatch.setattr(discord_call, "discord_source", lambda playback: ("source", playback))
    host = Host(Channel(10, []))
    calls = []

    async def opener(profile, drain):
        calls.append(profile)
        return FakeCall()

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=opener)
        await presence.tick()
        entry = presence.calls[7]
        host.present = []
        for _ in range(discord_presence.EMPTY_TICKS - 1):
            await presence.tick()
        still = 7 in presence.calls
        await presence.tick()
        return entry, still, presence

    entry, still, presence = asyncio.run(run())
    assert calls == ["iris"] and entry["call"].started and still
    assert host.log == ["join", "leave"] and 7 not in presence.calls
    assert entry["call"].ended == "plus personne dans le salon"


def test_presence_leaves_a_host_voice_join_alone_and_retries_a_failure_later(monkeypatch):
    host = Host(Channel(10, []))
    host.adapter._voice_clients[7] = VC()  # /voice join already running

    async def failing(profile, drain):
        raise RuntimeError("il manque la clé")

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=failing)
        await presence.tick()
        assert host.log == []
        host.adapter._voice_clients.clear()
        await presence.tick()
        await presence.tick()  # inside the retry delay: no second attempt
        return presence

    presence = asyncio.run(run())
    assert host.log == ["join", "leave"] and presence.calls == {}


def test_summon_takes_over_from_the_cascade(monkeypatch):
    monkeypatch.setattr(discord_call, "discord_source", lambda playback: playback)
    host = Host(Channel(10, []))
    host.present = []
    host.adapter._voice_clients[7] = VC()

    async def opener(profile, drain):
        return FakeCall()

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=opener)
        said = await presence.summon(host.channel, "orion")
        again = await presence.summon(host.channel, "orion")
        return said, again, presence

    said, again, presence = asyncio.run(run())
    assert said == "J'arrive dans ton salon vocal." and again == "Je suis déjà dans ton salon vocal."
    assert host.log == ["leave", "join"] and presence.calls[7]["profile"] == "orion"


def test_talk_asks_the_voice_allowlist_not_just_who_may_type_in_the_channel(monkeypatch):
    """With DISCORD_ALLOWED_CHANNELS alone, Hermes lets anyone type a command in
    that channel, never speak: a guest must not bring the agent (a paid session,
    the operator's call ended) nor hang up someone else's call."""

    import talk_with_hermes as plugin

    voice = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
    log = []

    class Adapter(FakeAdapter):
        async def get_user_voice_channel(self, guild_id, user_id):
            return voice

    class Presence:
        def __init__(self):
            self.adapter, self.calls = Adapter({"111"}), {}

        async def summon(self, channel, profile):
            log.append(("summon", channel.id, profile))
            self.calls[1] = {"channel": channel, "profile": profile}
            return "J'arrive dans ton salon vocal."

        async def end(self, guild_id, why):
            log.append(("end", guild_id))
            self.calls.pop(guild_id, None)

    presence = Presence()
    monkeypatch.setitem(discord_presence.PRESENCE, "presence", presence)
    monkeypatch.setattr(discord_host, "route_of", lambda channel: None)
    session = {"HERMES_SESSION_SCOPE_ID": "1"}
    monkeypatch.setattr(plugin, "session_value", lambda name: session.get(name, ""))

    def talk(user, args=""):
        session["HERMES_SESSION_USER_ID"] = user
        return asyncio.run(plugin._talk_on_gateway_loop(args, "iris"))

    said = talk("222")
    assert log == [], said  # the guest typed /talk: no call
    assert talk("111") == "J'arrive dans ton salon vocal." and log == [("summon", 10, "iris")]
    refused = talk("222", "leave")
    assert 1 in presence.calls and len(log) == 1, refused  # the guest typed /talk leave: the call goes on
    assert said == refused == plugin.NOT_ALLOWED
    assert talk("", "leave") == plugin.NOT_ALLOWED and 1 in presence.calls  # no user id: nobody
    assert talk("111", "leave") == "Raccroché. Le salon est à toi." and log[-1] == ("end", 1)


def test_a_call_that_ends_by_itself_backs_off_before_the_bot_enters_again(monkeypatch):
    """The provider ended it (refused, dropped, out of redials) while someone is
    still in the channel: entering again at once would repeat the same end,
    every few seconds, for as long as they stay."""

    monkeypatch.setattr(discord_call, "discord_source", lambda playback: playback)
    host = Host(Channel(10, []))
    made = []

    async def opener(profile, drain):
        made.append(FakeCall())
        return made[-1]

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=opener)
        await presence.tick()
        made[0].ended = "Gemini a coupé la session : quota"
        for _ in range(5):
            await presence.tick()
        return presence

    presence = asyncio.run(run())
    assert host.log == ["join", "leave"] and len(made) == 1
    assert 10 in presence._failed and presence.calls == {}


def test_a_call_that_fails_to_stop_still_leaves_the_channel(monkeypatch):
    """The voice connection is the host's: a provider that errors on the way
    out must not keep the bot in the channel with nobody tracking it."""

    monkeypatch.setattr(discord_call, "discord_source", lambda playback: playback)
    host = Host(Channel(10, []))

    class BrokenStop(FakeCall):
        async def stop(self, why=""):
            raise RuntimeError("socket already closed")

    async def opener(profile, drain):
        return BrokenStop()

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=opener)
        await presence.tick()
        await presence.end(7, "test")
        return presence

    presence = asyncio.run(run())
    assert host.log == ["join", "leave"] and presence.calls == {}


def test_a_call_whose_playback_fails_is_stopped_and_leaves(monkeypatch):
    """Playback starts after the provider session: if Discord refuses it, the
    session is stopped and the channel left, and the next tick may retry."""

    def refuse(playback):
        raise RuntimeError("Not connected to voice.")

    monkeypatch.setattr(discord_call, "discord_source", refuse)
    host = Host(Channel(10, []))
    made = []

    async def opener(profile, drain):
        made.append(FakeCall())
        return made[-1]

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=opener)
        await presence.tick()
        return presence

    presence = asyncio.run(run())
    assert host.log == ["join", "leave"] and presence.calls == {}
    assert made[0].ended and 10 in presence._failed


def test_the_agent_receives_plain_json_never_the_key_in_the_body():
    captured = {}

    async def post(url, headers, body, timeout):
        captured["body"] = json.dumps(body)
        return 200, {}, {"choices": [{"message": {"content": "ok"}}]}

    asyncio.run(ApiDelegate("http://h:1", "iris", "secret-key", post=post)("x"))
    assert "secret-key" not in captured["body"]


def test_one_presence_per_process_even_when_several_profiles_load_the_plugin():
    adapter = types.SimpleNamespace()
    first = discord_presence.Presence(adapter, host=Host(Channel(10, [])), opener=None)
    second = discord_presence.Presence(adapter, host=Host(Channel(10, [])), opener=None)
    assert discord_presence.claim(adapter, first) is first
    assert discord_presence.claim(adapter, second) is first  # the second copy never runs
    assert vars(adapter) == {}  # nothing written onto the host's adapter (catalog rule 9)
    discord_presence.release(adapter, second)  # not the holder: no effect
    assert discord_presence.claim(adapter, second) is first
    discord_presence.release(adapter, first)
    assert discord_presence.claim(adapter, second) is second
    discord_presence.release(adapter, second)


# -- one place for the keys ----------------------------------------------------

def test_the_owner_is_the_profile_the_plugin_is_installed_in(tmp_path):
    from talk_with_hermes import keys

    install = tmp_path / "profiles" / "iris" / "plugins" / "talk-with-hermes" / "talk_with_hermes" / "keys.py"
    install.parent.mkdir(parents=True)
    install.write_text("")
    assert keys.owner_home(install) == tmp_path / "profiles" / "iris"
    assert keys.profile_of(tmp_path / "profiles" / "iris") == "iris" and keys.profile_of(tmp_path) == "default"


def test_the_gateway_config_is_parsed_again_only_when_it_changes(tmp_path, monkeypatch, host_yaml):
    """It is read on the gateway's own loop."""

    import os
    import sys

    config = tmp_path / "config.yaml"
    config.write_text("platforms:\n  api_server:\n    extra: {port: 9000}\n", encoding="utf-8")
    monkeypatch.setattr(discord_host, "profile_home", lambda name: tmp_path)
    monkeypatch.setattr(discord_host, "runner", lambda: None)  # outside the gateway: config.yaml
    parsed = []
    real = sys.modules["hermes_yaml"].safe_load
    monkeypatch.setattr(sys.modules["hermes_yaml"], "safe_load", lambda text: parsed.append(1) or real(text))
    for _ in range(5):
        assert discord_host.api_server_base() == "http://127.0.0.1:9000"
    assert len(parsed) == 1
    config.write_text(config.read_text(encoding="utf-8").replace("9000", "9001"), encoding="utf-8")
    os.utime(config, ns=(1, 2_000_000_000_000_000_000))
    assert discord_host.api_server_base() == "http://127.0.0.1:9001" and len(parsed) == 2
    config.unlink()
    assert discord_host.gateway_config() == {}


def test_profile_homes_are_laid_out_as_hermes_lays_them_out(tmp_path, monkeypatch):
    import sys

    constants = types.ModuleType("hermes_constants")
    setattr(constants, "PROFILE_ID_RE", re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$"))
    setattr(constants, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    for name in ("default", "Default", "", " default "):
        assert discord_host.profile_home(name) == tmp_path, name
    assert discord_host.profile_home("Orion") == tmp_path / "profiles" / "orion"
    for bad in ("../orion", "orion/x", "-orion", "a b", "e" * 65):
        with pytest.raises(ValueError):
            discord_host.profile_home(bad)


def test_the_default_profile_enters_its_scope_like_any_other(tmp_path, monkeypatch):
    """On a multiplexed gateway a secret read outside a profile scope raises,
    so the default profile must enter its scope too, or its keys read as unset."""
    import sys
    import types
    from contextlib import asynccontextmanager

    entered = []

    @asynccontextmanager
    async def runtime_scope(home):
        entered.append(home)
        yield

    run = types.ModuleType("gateway.run")
    run._async_profile_runtime_scope = runtime_scope
    gateway = types.ModuleType("gateway")
    gateway.run = run
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.run", run)
    monkeypatch.setattr(discord_host, "profile_home", lambda profile: tmp_path / (profile or "default"))

    async def enter(profile):
        async with discord_host.profile_scope(profile) as home:
            return home

    for profile in ("default", "", "orion"):
        entered.clear()
        home = asyncio.run(enter(profile))
        assert entered == [home], profile


def test_the_door_key_is_created_once_and_never_asked_for(monkeypatch):
    from talk_with_hermes import keys

    store = {}
    monkeypatch.setattr(keys, "env_value", lambda name, **_: store.get(name, ""))
    key, created = keys.ensure_door_key(save=store.__setitem__)
    assert created and len(key) >= 32 and store == {"API_SERVER_KEY": key}
    assert keys.ensure_door_key(save=store.__setitem__) == (key, False)


def test_an_unreadable_door_key_is_never_replaced(monkeypatch):
    """A read Hermes refuses is not an unset key: writing a new one would
    rotate the key every other local client of that agent already uses."""
    import sys
    import types

    from talk_with_hermes import keys

    def refuse(name):
        raise RuntimeError("no profile scope")

    config = types.ModuleType("hermes_cli.config")
    config.get_env_value = refuse
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.config = config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    store = {}
    with pytest.raises(RuntimeError, match="no profile scope"):
        keys.ensure_door_key(save=store.__setitem__)
    assert store == {}


def test_waiting_for_a_new_door_stops_when_it_opens():
    from talk_with_hermes import keys

    answers = [401, 401, 200]

    async def probe():
        return answers.pop(0)

    async def nap(_):
        pass

    assert asyncio.run(keys.wait_door_open(probe, sleep=nap)) is True
    assert asyncio.run(keys.wait_door_open(lambda: _status(401), seconds=2, sleep=nap)) is False


async def _status(code):
    return code


def test_a_call_in_any_agent_channel_uses_the_plugin_keys_and_the_agent_identity(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from talk_with_hermes import keys

    owner, agent = tmp_path / "iris", tmp_path / "orion"
    for home in (owner, agent):
        home.mkdir()
    (owner / "config.yaml").write_text(
        "plugins:\n  entries:\n    talk-with-hermes:\n      settings: {model: gemini-3.8-live}\n", encoding="utf-8")
    (agent / "SOUL.md").write_text("# Orion\n\nJe suis Orion.\n", encoding="utf-8")
    secrets_of = {"iris": {"GEMINI_API_KEY": "voice-key"}, "orion": {"API_SERVER_KEY": "door-key"}}
    scope = {"now": None}

    @asynccontextmanager
    async def profile_scope(name):
        scope["now"] = name
        yield {"iris": owner, "orion": agent}[name]
        scope["now"] = None

    monkeypatch.setattr(keys, "owner_profile", lambda: "iris")
    monkeypatch.setattr(keys, "env_value", lambda name, **_: secrets_of.get(scope["now"], {}).get(name, ""))
    monkeypatch.setattr(discord_host, "profile_scope", profile_scope)
    monkeypatch.setattr(discord_host, "api_server_base", lambda: "http://127.0.0.1:8642")

    call = asyncio.run(discord_call.open_call("orion", drain=lambda: b"", session_factory=FakeSession))
    assert call.config.api_key == "voice-key"  # from the plugin's own profile, never copied to Orion
    assert call.config.name == "Orion"  # who speaks is the channel's agent
    assert call._delegate_fn.url.endswith("/p/orion/v1/chat/completions") and call._delegate_fn._key == "door-key"
    assert "GEMINI_API_KEY" not in secrets_of["orion"]

    secrets_of["iris"] = {}
    with pytest.raises(RuntimeError, match="réglages du plugin"):
        asyncio.run(discord_call.open_call("orion", drain=lambda: b"", session_factory=FakeSession))


def test_failed_starts_back_off_and_a_new_entry_retries_at_once():
    assert [discord_presence.retry_delay(n) for n in (1, 2, 3, 6, 9)] == [30, 60, 120, 600, 600]
    host = Host(Channel(10, []))
    attempts = []

    async def failing(profile, drain):
        attempts.append(profile)
        raise RuntimeError("Timed out")

    async def run():
        presence = discord_presence.Presence(host.adapter, host=host, opener=failing)
        await presence.tick()
        await presence.tick()  # still inside the delay
        host.present = []
        await presence.tick()  # he walked out: the failure is forgotten
        host.present = [1]
        await presence.tick()

    asyncio.run(run())
    assert attempts == ["iris", "iris"]


def test_leaving_drops_the_held_audio_even_without_the_host_helper(caplog):
    """The host would transcribe on leave what the receiver still holds, and
    start an agent turn nobody asked for. Without its discard helper (renamed
    or failing), the buffers are emptied here and the miss is logged."""

    class Adapter:
        def __init__(self, receiver, helper):
            self._voice_receivers = {7: receiver}
            self.left = []
            if helper is not None:
                self.discard_pending_voice_input = helper

        async def leave_voice_channel(self, guild_id):
            receiver = self._voice_receivers[guild_id]
            self.left.append((bytes(receiver._buffers.get(9, b"")), dict(receiver._last_packet_time)))

    def broken(guild_id):
        raise RuntimeError("helper changed")

    for helper in (None, broken):
        receiver = FakeReceiver({})
        receiver.push(9, b"half a sentence")
        adapter = Adapter(receiver, helper)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=discord_host.__name__):
            asyncio.run(discord_host.leave(adapter, 7))
        assert adapter.left == [(b"", {})], helper
        assert "discard" in caplog.text


def test_the_agent_door_is_where_the_gateway_bound_its_api_server(tmp_path, monkeypatch, host_yaml):
    """The gateway binds the API server from the config it loaded at startup,
    with API_SERVER_HOST and API_SERVER_PORT applied. A port given only there
    must not send the call, and its door key, to whatever listens on 8642."""

    (tmp_path / "config.yaml").write_text("platforms:\n  api_server:\n    extra: {}\n", encoding="utf-8")
    monkeypatch.setattr(discord_host, "profile_home", lambda name: tmp_path)
    for name in ("API_SERVER_HOST", "API_SERVER_PORT"):
        monkeypatch.delenv(name, raising=False)

    def loaded(extra):
        api_server = enum.Enum("Platform", {"API_SERVER": "api_server"}).API_SERVER
        config = types.SimpleNamespace(platforms={api_server: types.SimpleNamespace(extra=extra)})
        return lambda: types.SimpleNamespace(config=config)

    monkeypatch.setattr(discord_host, "runner", loaded({"host": "127.0.0.1", "port": 9100}))
    assert discord_host.api_server_base() == "http://127.0.0.1:9100"
    monkeypatch.setattr(discord_host, "runner", loaded({"host": "::1", "port": 9101}))
    assert discord_host.api_server_base() == "http://[::1]:9101"

    monkeypatch.setattr(discord_host, "runner", lambda: None)  # outside the gateway: Hermes' rule
    monkeypatch.setenv("API_SERVER_PORT", "9102")
    assert discord_host.api_server_base() == "http://127.0.0.1:9102"
