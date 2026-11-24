"""Regressions for the security review of 2026-11-20 (one test per finding)."""

import asyncio
import base64
import os
import stat
import time

import pytest

from talk_with_hermes import desktop_bridge
from talk_with_hermes.desktop_bridge import DesktopCall, HandleStore
from talk_with_hermes.prefs import MAX_HEARD, PrefsError, PrefsStore, Word, apply_words, parse, write_private


class Session:
    instances = []

    def __init__(self, config, handlers, **_):
        self.config, self.handlers, self.audio, self.closed = config, handlers, [], False
        Session.instances.append(self)

    async def start(self):
        return None

    async def send_audio(self, pcm):
        if self.closed:
            raise ConnectionError("closed")
        self.audio.append(pcm)

    async def close(self):
        self.closed = True


def make(tmp_path, **kw):
    Session.instances = []
    return DesktopCall(settings=dict, api_key=lambda _service: "k", handles=HandleStore(tmp_path / "r.json"),
                       prefs=PrefsStore(tmp_path / "p.json"), session_factory=Session, **kw)


PCM = base64.b64encode(b"\0\0" * 400).decode()


# -- one owner per call ---------------------------------------------------------


def test_a_second_window_takes_over_and_the_first_gets_nothing(tmp_path):
    async def run():
        call = make(tmp_path)
        first = (await call.start())["owner"]
        session = Session.instances[0]
        task = asyncio.ensure_future(session.handlers.on_tool_call("c1", "ventes ?"))
        await asyncio.sleep(0)
        second = (await call.start())["owner"]  # reload or a second window
        await asyncio.sleep(0)
        old = call.poll(0, first)
        new = call.poll(0, second)
        results = (await call.audio(PCM, first), await call.audio(PCM, second),
                   call.answer("c1", "trois", owner=first), await call.stop(first))
        with pytest.raises(RuntimeError, match="changé de fenêtre"):
            await task  # failed with a reason, so the session answers the model
        return first, second, old, new, results, call

    first, second, old, new, results, call = asyncio.run(run())
    assert first != second and len(second) >= 16
    assert old["owner_ok"] is False and old["events"] == []
    # The new window never receives the old request: it would run it a second time.
    assert "owner_ok" not in new and [e["kind"] for e in new["events"]] == []
    assert results[:3] == (False, True, "unknown")
    assert results[3]["status"] == "live"  # a stale window cannot hang up the new owner's call


def test_a_takeover_mid_delegation_still_answers_the_voice_model_once(tmp_path):
    """Real sessions on a fake socket: the model gets one spoken failure, not silence."""
    import json

    from talk_with_hermes import realtime
    from talk_with_hermes.desktop_bridge import DesktopCall, HandleStore
    from talk_with_hermes.gemini_live import GeminiLiveSession
    from talk_with_hermes.prefs import PrefsStore
    from talk_with_hermes import identity as identity_mod
    from test_gemini_live import Connector, FakeSocket, settle, setup_ok

    async def run(kind):
        ready = setup_ok() if kind == "gemini" else json.dumps({"type": "session.updated", "session": {}})
        socket = FakeSocket([ready])
        connect = Connector(socket)

        def factory(cfg, handlers, **kw):
            if kind == "gemini":
                return GeminiLiveSession(cfg, handlers, connect=connect, **kw)
            return realtime.RealtimeSession(cfg, handlers, dialect=realtime.XAI, connect=connect, **kw)

        model = "gemini-3.8-live" if kind == "gemini" else "grok-voice-latest"
        call = DesktopCall(settings=lambda: {"model": model}, api_key=lambda _service: "k",
                           handles=HandleStore(tmp_path / f"{kind}.json"), prefs=PrefsStore(tmp_path / "p.json"),
                           session_factory=factory, identity=identity_mod.Identity)
        await call.start()
        if kind == "gemini":
            socket.push({"toolCall": {"functionCalls": [{"id": "c1", "name": "talk", "args": {"request": "ventes ?"}}]}})
        else:
            socket.push({"type": "response.function_call_arguments.done", "call_id": "c1", "name": "talk",
                         "arguments": json.dumps({"request": "ventes ?"})})
        await settle(20)
        second = (await call.start())["owner"]
        await settle(20)
        events = [e["kind"] for e in call.poll(0, second)["events"]]
        await call.stop(second)
        if kind == "gemini":
            return events, [r["functionResponses"][0]["response"] for f in socket.sent
                            if (r := f.get("toolResponse"))]
        return events, [f["item"]["output"] for f in socket.sent if f.get("type") == "conversation.item.create"]

    for kind in ("gemini", "grok"):
        events, replies = asyncio.run(run(kind))
        assert "delegate" not in events, kind
        assert len(replies) == 1 and "délégation a échoué" in json.dumps(replies[0], ensure_ascii=False), (kind, replies)


def test_an_empty_owner_is_never_any_owner(tmp_path):
    async def run():
        call = make(tmp_path)
        await call.start()
        return call.poll(0, ""), await call.audio(PCM, ""), call.answer("x", "y", owner="")

    poll, audio, answer = asyncio.run(run())
    assert poll["owner_ok"] is False and audio is False and answer == "unknown"


def test_a_call_nobody_polls_hangs_up_by_itself(tmp_path):
    async def run():
        call = make(tmp_path, idle_hangup=0.2)
        await call.start()
        await asyncio.sleep(0.6)
        return call, Session.instances[0]

    call, session = asyncio.run(run())
    assert call.status == "idle" and session.closed is True


def test_a_polled_call_stays_up(tmp_path):
    async def run():
        call = make(tmp_path, idle_hangup=0.3)
        owner = (await call.start())["owner"]
        for _ in range(8):
            await asyncio.sleep(0.1)
            call.poll(0, owner)
        status = call.status
        await call.stop(owner)
        return status

    assert asyncio.run(run()) == "live"


# -- limits -------------------------------------------------------------------


def test_oversized_audio_is_refused_before_decoding(tmp_path, monkeypatch):
    decoded = []
    real = base64.b64decode
    monkeypatch.setattr(desktop_bridge.base64, "b64decode", lambda *a, **k: decoded.append(1) or real(*a, **k))

    async def run():
        call = make(tmp_path)
        await call.start()
        return await call.audio("A" * (desktop_bridge.MAX_AUDIO_B64_CHARS + 4))

    assert asyncio.run(run()) is False and decoded == []


def test_audio_on_a_closing_socket_is_a_false_not_a_500(tmp_path):
    async def run():
        call = make(tmp_path)
        await call.start()
        Session.instances[0].closed = True
        return await call.audio(PCM)

    assert asyncio.run(run()) is False


def test_delegations_are_capped_and_ids_must_be_fresh(tmp_path):
    async def run():
        call = make(tmp_path)
        await call.start()
        h = Session.instances[0].handlers
        tasks = [asyncio.ensure_future(h.on_tool_call(f"c{i}", "x")) for i in range(desktop_bridge.MAX_WAITING)]
        await asyncio.sleep(0)
        errors = []
        for call_id in ("c0", "", "extra"):
            try:
                await h.on_tool_call(call_id, "x")
            except RuntimeError as exc:
                errors.append(str(exc))
        for i in range(desktop_bridge.MAX_WAITING):
            call.answer(f"c{i}", "ok")
        await asyncio.gather(*tasks)
        return errors

    errors = asyncio.run(run())
    assert len(errors) == 3 and "déjà" in errors[0] and "trop" in errors[2]


def test_an_answer_is_bounded(tmp_path):
    async def run():
        call = make(tmp_path)
        await call.start()
        task = asyncio.ensure_future(Session.instances[0].handlers.on_tool_call("c1", "x"))
        await asyncio.sleep(0)
        call.answer("c1", "a" * 100_000)
        return await task

    assert len(asyncio.run(run())) == desktop_bridge.MAX_ANSWER_CHARS


def test_control_events_survive_when_audio_floods_the_feed(tmp_path):
    async def run():
        call = make(tmp_path, max_events=10)
        await call.start()
        h = Session.instances[0].handlers
        task = asyncio.ensure_future(h.on_tool_call("c1", "x"))
        await asyncio.sleep(0)
        for _ in range(50):
            h.on_audio(b"\0\0")
        events = call.poll(0)["events"]
        call.answer("c1", "ok")
        await task
        return events

    events = asyncio.run(run())
    assert len(events) == 10 and [e["kind"] for e in events].count("delegate") == 1


def test_a_huge_variant_list_is_refused_at_once():
    started = time.perf_counter()
    with pytest.raises(PrefsError):
        parse({"words": [{"word": "a", "heard": [f"v{i}" for i in range(20_000)]}]})
    with pytest.raises(PrefsError):
        parse({"words": [{"word": "a", "heard": ",".join(f"v{i}" for i in range(20_000))}]})
    assert time.perf_counter() - started < 0.2
    assert len(parse({"words": [{"word": "a", "heard": [f"v{i}" for i in range(MAX_HEARD)]}]}).words[0].heard) == MAX_HEARD


# -- the dictionary is text, never a pattern ---------------------------------


@pytest.mark.parametrize("word", [r"C:\Users\x", r"a\1", "US$&x$$y", r"\n"])
def test_a_replacement_word_is_literal(word):
    assert apply_words("dis caviro stp", (Word(word, ("caviro",)),)) == f"dis {word} stp"


def test_a_broken_dictionary_never_costs_the_request(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_bridge, "apply_words", lambda *_: (_ for _ in ()).throw(ValueError("boom")))

    async def run():
        call = make(tmp_path)
        await call.start()
        task = asyncio.ensure_future(Session.instances[0].handlers.on_tool_call("c1", "fichiers caviro"))
        await asyncio.sleep(0)
        event = call.poll(0)["events"][0]
        call.answer("c1", "ok")
        return event, await task

    event, spoken = asyncio.run(run())
    assert event["request"] == "fichiers caviro" and spoken == "ok"


# -- files and secrets -------------------------------------------------------------


def test_private_files_are_never_readable_by_others(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))  # the temp file, before it lands
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    old = os.umask(0o022)
    try:
        (tmp_path / "state").mkdir(mode=0o755)  # an older version left it open
        write_private(tmp_path / "state" / "resume.json", '{"handle": "secret"}')
    finally:
        os.umask(old)
    assert seen == [0o600]
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700
    assert [p.name for p in (tmp_path / "state").iterdir()] == ["resume.json"]


def test_a_disk_refusal_is_a_french_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(PrefsError, match="Enregistrement impossible"):
        PrefsStore(blocker / "prefs.json").save(parse({}))


def test_the_key_is_never_borrowed_from_the_raw_environment(monkeypatch):
    import sys
    import types

    hermes_cli = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.get_env_value = lambda name: None  # Hermes says: no key in this profile's scope
    hermes_cli.config = config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setenv("GEMINI_API_KEY", "inherited")
    assert desktop_bridge.load_api_key() == ""
    config.get_env_value = lambda name: "scoped" if name == "GEMINI_API_KEY" else None
    assert desktop_bridge.load_api_key() == "scoped"
