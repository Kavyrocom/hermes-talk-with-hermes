import asyncio
import base64
import os
import stat
from dataclasses import replace
from typing import Any
from pathlib import Path

import pytest

from talk_with_hermes.prefs import Prefs, PrefsStore
from talk_with_hermes import desktop_bridge, persona, realtime, services
from talk_with_hermes.desktop_bridge import DesktopCall, HandleStore, config_from
from talk_with_hermes.gemini_live import LiveError


class FakeSession:
    """Stands in for GeminiLiveSession: records what the bridge does with it
    and lets the test drive the callbacks."""

    instances = []

    def __init__(self, config, handlers, *, handle=None, save_handle=None, fail=None):
        self.config, self.handlers, self.handle = config, handlers, handle
        self.save_handle = save_handle
        self.fail = fail
        self.audio = []
        self.closed = False
        FakeSession.instances.append(self)

    async def start(self):
        if self.fail is not None:
            raise self.fail

    async def send_audio(self, pcm):
        self.audio.append(pcm)

    async def end_audio(self):
        self.audio.append("END")

    async def close(self):
        self.closed = True


def make_call(tmp_path, *, key="k", settings=None, fail=None, timeout=5.0):
    FakeSession.instances = []

    def factory(config, handlers, **kwargs):
        return FakeSession(config, handlers, fail=fail, **kwargs)

    return DesktopCall(
        settings=lambda: dict(settings or {}),
        api_key=lambda: key,
        handles=HandleStore(tmp_path / "resume.json"),
        prefs=PrefsStore(tmp_path / "prefs.json"),
        session_factory=factory,
        delegation_timeout=timeout,
    )


def kinds(poll):
    return [e["kind"] for e in poll["events"]]


# -- settings ---------------------------------------------------------------


def test_settings_become_a_live_config():
    config = config_from({"model": "gemini-3.1-flash-live-preview", "voice": "Kore",
                          "spoken_answers": "brief", "languages": "fr-FR, en-US"}, "k")
    assert config.model == "gemini-3.1-flash-live-preview"
    assert config.voice == "Kore"
    assert config.spoken_answers == "brief"
    assert config.languages == ("fr-FR", "en-US")


def test_settings_are_read_where_the_desktop_form_writes_them(monkeypatch):
    import sys
    import types

    config: dict = {"plugins": {"entries": {"talk-with-hermes": {"settings": {"voice": "Kore"}}}}}
    fake: Any = types.ModuleType("hermes_cli.config")
    fake.load_config_readonly = lambda: config
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)
    assert desktop_bridge.load_settings() == {"voice": "Kore"}
    config["plugins"] = {"entries": {"talk-with-hermes": "garbage"}}
    assert desktop_bridge.load_settings() == {}


def test_unknown_spoken_mode_falls_back_to_full():
    assert config_from({"spoken_answers": "shout"}, "k").spoken_answers == "full"


def test_api_key_comes_from_the_environment_and_is_trimmed(monkeypatch):
    # A test service: the real profile key must never be read, let alone shown.
    fake = replace(services.SERVICES[services.GROK], key_env=("TWH_TEST_KEY_A", "TWH_TEST_KEY_B"))
    monkeypatch.delenv("TWH_TEST_KEY_A", raising=False)
    monkeypatch.setenv("TWH_TEST_KEY_B", "  abc  ")
    assert desktop_bridge.load_api_key(fake) == "abc"


def test_the_key_read_is_the_configured_services_own(monkeypatch):
    seen = []
    monkeypatch.setattr(desktop_bridge, "load_settings", lambda: {"model": "grok-voice-latest"})
    monkeypatch.setitem(services.SERVICES, services.GROK,
                        replace(services.SERVICES[services.GROK], key_env=("TWH_TEST_XAI",)))
    monkeypatch.setattr(services, "current", lambda settings: seen.append(settings) or services.SERVICES[services.GROK])
    monkeypatch.setenv("TWH_TEST_XAI", "x1")
    assert desktop_bridge.load_api_key() == "x1" and seen == [{"model": "grok-voice-latest"}]


def test_config_picks_the_service_from_the_model_and_keeps_the_voice_valid():
    gemini = config_from({}, "k")
    assert gemini.model == "gemini-3.8-live" and gemini.voice == "Puck"
    grok = config_from({"model": "grok-voice-latest", "voice": "Puck"}, "k")
    assert grok.model == "grok-voice-latest" and grok.voice == "eve"  # a Gemini voice falls back
    assert config_from({"model": "grok-voice-latest"}, "k", Prefs(voice="REX")).voice == "rex"
    openai = config_from({"model": "gpt-realtime-2.1"}, "k", Prefs(voice="cedar"))
    assert (openai.model, openai.voice) == ("gpt-realtime-2.1", "cedar")
    assert config_from({"model": "whisper-1"}, "k").model == "gemini-3.8-live"  # unknown: the proven one


def test_the_session_class_follows_the_model():
    from talk_with_hermes.gemini_live import GeminiLiveSession
    from talk_with_hermes.realtime import RealtimeSession

    assert isinstance(services.open_session(config_from({}, "k")), GeminiLiveSession)
    grok = services.open_session(config_from({"model": "grok-voice-latest"}, "k"), handle="h", save_handle=print)
    assert isinstance(grok, RealtimeSession) and grok.dialect is realtime.XAI
    assert services.open_session(config_from({"model": "gpt-realtime-2"}, "k")).dialect is realtime.OPENAI


# -- start / stop -----------------------------------------------------------


def test_start_goes_live_with_the_settings_and_the_stored_handle(tmp_path):
    async def run():
        call = make_call(tmp_path, settings={"voice": "Kore"})
        HandleStore(tmp_path / "resume.json").save("h1")
        result = await call.start()
        return result, FakeSession.instances[0]

    result, session = asyncio.run(run())
    assert result["status"] == "live" and result["has_key"] is True
    assert session.config.voice == "Kore"
    assert session.handle == "h1"


def test_missing_key_is_a_speakable_error_not_a_crash(tmp_path):
    async def run():
        call = DesktopCall(settings=dict, api_key=lambda: "", handles=HandleStore(tmp_path / "r.json"))
        return await call.start()

    result = asyncio.run(run())
    assert result["status"] == "error"
    assert "GEMINI_API_KEY" in result["error"]
    assert result["has_key"] is False


def test_a_refusal_is_shown_and_the_next_start_retries(tmp_path):
    async def run():
        call = make_call(tmp_path, fail=LiveError("Gemini a refusé : API key not valid", refused=True))
        first = await call.start()
        call._session_factory = lambda config, handlers, **kw: FakeSession(config, handlers, **kw)
        second = await call.start()
        return first, second, call.session

    first, second, session = asyncio.run(run())
    assert first["status"] == "error" and "API key not valid" in first["error"]
    assert second["status"] == "live" and session is not None


def test_a_network_failure_names_the_error_without_leaking_details(tmp_path):
    async def run():
        call = make_call(tmp_path, fail=OSError("getaddrinfo failed for secret-host"))
        return await call.start()

    result = asyncio.run(run())
    assert result["status"] == "error"
    assert "OSError" in result["error"] and "secret-host" not in result["error"]


def test_a_second_start_returns_the_call_in_progress(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        await call.start()
        return len(FakeSession.instances)

    assert asyncio.run(run()) == 1


def test_stop_closes_the_session_and_goes_idle(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        result = await call.stop()
        return result, FakeSession.instances[0]

    result, session = asyncio.run(run())
    assert result["status"] == "idle" and session.closed


# -- audio ------------------------------------------------------------------


def test_audio_is_decoded_and_forwarded_only_while_live(tmp_path):
    async def run():
        call = make_call(tmp_path)
        before = await call.audio(base64.b64encode(b"\x01\x02").decode())
        await call.start()
        ok = await call.audio(base64.b64encode(b"\x01\x02").decode())
        return before, ok, FakeSession.instances[0].audio

    before, ok, sent = asyncio.run(run())
    assert before is False and ok is True
    assert sent == [b"\x01\x02"]


@pytest.mark.parametrize("data", [None, 12, "not base64!!", base64.b64encode(b"\x01").decode(),
                                  base64.b64encode(b"\x00" * (desktop_bridge.MAX_AUDIO_CHUNK_BYTES + 2)).decode()])
def test_bad_audio_is_refused(tmp_path, data):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        return await call.audio(data), FakeSession.instances[0].audio

    assert asyncio.run(run()) == (False, [])


# -- event feed ---------------------------------------------------------------


def test_session_callbacks_become_an_ordered_feed(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        h = FakeSession.instances[0].handlers
        h.on_input_text("bonjour")
        h.on_audio(b"\x00\x01")
        h.on_output_text("salut")
        h.on_interrupted()
        h.on_turn_complete()
        return call.poll(0)

    poll = asyncio.run(run())
    assert kinds(poll) == ["in", "audio", "out", "interrupted", "turn"]
    assert [e["seq"] for e in poll["events"]] == [1, 2, 3, 4, 5]
    assert poll["events"][1]["data"] == base64.b64encode(b"\x00\x01").decode()
    assert poll["next"] == 5


def test_polling_acknowledges_what_the_renderer_already_has(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        h = FakeSession.instances[0].handlers
        h.on_input_text("a")
        h.on_input_text("b")
        first = call.poll(0)
        h.on_input_text("c")
        second = call.poll(first["next"])
        return second, len(call._events)

    second, kept = asyncio.run(run())
    assert [e["text"] for e in second["events"]] == ["c"]
    assert kept == 1


def test_the_feed_is_bounded(tmp_path):
    async def run():
        call = DesktopCall(settings=dict, api_key=lambda: "k", handles=HandleStore(tmp_path / "r.json"),
                           session_factory=lambda c, h, **kw: FakeSession(c, h, **kw), max_events=3)
        await call.start()
        for i in range(10):
            FakeSession.instances[-1].handlers.on_input_text(str(i))
        return call.poll(0)

    poll = asyncio.run(run())
    assert [e["text"] for e in poll["events"]] == ["7", "8", "9"]


def test_a_full_feed_drops_audio_then_text_but_never_a_delegate_or_a_cancel(tmp_path):
    """A lost chunk is a blip; a lost `delegate` or `cancel` is a request that
    never runs or never stops."""

    async def run():
        call = DesktopCall(settings=dict, api_key=lambda: "k", handles=HandleStore(tmp_path / "r.json"),
                           session_factory=lambda c, h, **kw: FakeSession(c, h, **kw), max_events=4)
        await call.start()
        handlers = FakeSession.instances[-1].handlers
        task = asyncio.ensure_future(handlers.on_tool_call("c1", "ventes ?"))
        await asyncio.sleep(0)
        handlers.on_tool_cancelled("c0")  # the renderer must still stop c0
        handlers.on_audio(b"\0\0")
        for i in range(10):
            handlers.on_output_text(str(i))
        poll = call.poll(0)
        call.answer("c1", "Trois ventes.")
        await task
        return poll

    poll = asyncio.run(run())
    assert [(e["kind"], e.get("id", e.get("text"))) for e in poll["events"]] == [
        ("delegate", "c1"), ("cancel", "c0"), ("out", "8"), ("out", "9")]


def test_an_unexpected_end_surfaces_as_an_error_with_the_reason(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        FakeSession.instances[0].handlers.on_closed("quota exceeded")
        return call.poll(0), call.session

    poll, session = asyncio.run(run())
    assert poll["status"] == "error" and "quota exceeded" in poll["error"]
    assert kinds(poll) == ["closed"] and session is None


# -- delegation ---------------------------------------------------------------


def test_a_delegation_waits_for_the_renderer_answer(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        task = asyncio.ensure_future(FakeSession.instances[0].handlers.on_tool_call("c1", "ventes ?"))
        await asyncio.sleep(0)
        event = call.poll(0)["events"][0]
        result = call.answer("c1", "Trois ventes.")
        return event, result, await task

    event, result, spoken = asyncio.run(run())
    assert event["kind"] == "delegate" and event["id"] == "c1" and event["request"] == "ventes ?"
    assert event["prompt"] == persona.delegated_prompt("ventes ?")
    assert result == "accepted" and spoken == "Trois ventes."


def test_a_renderer_error_makes_the_delegation_fail(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        task = asyncio.ensure_future(FakeSession.instances[0].handlers.on_tool_call("c1", "x"))
        await asyncio.sleep(0)
        call.answer("c1", error="agent down")
        with pytest.raises(RuntimeError, match="agent down"):
            await task

    asyncio.run(run())


def test_a_silent_agent_times_out_and_the_renderer_is_told_to_stop_it(tmp_path):
    async def run():
        call = make_call(tmp_path, timeout=0.01)
        await call.start()
        with pytest.raises(RuntimeError, match="timed out"):
            await FakeSession.instances[0].handlers.on_tool_call("c1", "x")
        return call.poll(0), call.answer("c1", "trop tard")

    poll, late = asyncio.run(run())
    assert kinds(poll) == ["cancel"]  # a request nobody awaits any more is never handed out
    assert poll["events"][0] == {"seq": 2, "kind": "cancel", "id": "c1", "why": "timeout"}
    assert late == "unknown"


def test_a_gemini_cancellation_reaches_the_renderer_and_late_answers_are_ignored(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        h = FakeSession.instances[0].handlers
        task = asyncio.ensure_future(h.on_tool_call("c1", "x"))
        await asyncio.sleep(0)
        task.cancel()
        h.on_tool_cancelled("c1")
        with pytest.raises(asyncio.CancelledError):
            await task
        return call.poll(0), call.answer("c1", "trop tard")

    poll, late = asyncio.run(run())
    assert kinds(poll) == ["cancel"] and poll["events"][0]["why"] == "gemini"
    assert late == "unknown"


def test_an_answered_delegation_never_runs_again_in_the_next_window(tmp_path):
    """The window that ran it closed before acknowledging it: the window that
    takes over starts its feed at 0 and must not run the request a second time."""

    async def run():
        call = make_call(tmp_path)
        first = (await call.start())["owner"]
        task = asyncio.ensure_future(FakeSession.instances[0].handlers.on_tool_call("c1", "envoie le devis"))
        await asyncio.sleep(0)
        assert kinds(call.poll(0, owner=first)) == ["delegate"]
        assert call.answer("c1", "Envoyé.", owner=first) == "accepted"
        await task
        second = (await call.start())["owner"]  # another window takes the call over
        return call.poll(0, owner=second)

    assert "delegate" not in kinds(asyncio.run(run()))


def test_a_new_call_starts_with_an_empty_feed(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        h = FakeSession.instances[0].handlers
        h.on_audio(b"\x01\x00")
        h.on_output_text("fin de l'appel précédent")
        await call.stop()
        owner = (await call.start())["owner"]
        return call.poll(0, owner=owner)

    poll = asyncio.run(run())
    assert poll["events"] == [] and poll["next"] >= 2  # the cursor stays monotonic


def test_stop_releases_a_pending_delegation(tmp_path):
    async def run():
        call = make_call(tmp_path)
        await call.start()
        task = asyncio.ensure_future(FakeSession.instances[0].handlers.on_tool_call("c1", "x"))
        await asyncio.sleep(0)
        await call.stop()
        with pytest.raises(RuntimeError):  # released with a reason, never left hanging
            await task

    asyncio.run(run())


def test_unknown_ids_are_refused():
    assert DesktopCall(settings=dict, api_key=lambda: "k", handles=HandleStore(Path(os.devnull))).answer("nope", "x") == "unknown"


# -- handle store -------------------------------------------------------------


def test_handle_store_round_trip_is_private(tmp_path):
    store = HandleStore(tmp_path / "state" / "resume.json")
    assert store.load() is None
    store.save("h1")
    assert store.load() == "h1"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    store.save(None)
    assert store.load() is None and not store.path.exists()


def test_a_corrupt_handle_file_is_ignored(tmp_path):
    path = tmp_path / "resume.json"
    path.write_text("{not json")
    assert HandleStore(path).load() is None


def test_state_lives_under_the_profile_home_not_the_install(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = desktop_bridge.default_state_dir()
    assert "plugin-state" in path.parts and path.name == "talk-with-hermes"


def test_muted_drops_late_chunks_and_tells_gemini_once(tmp_path):
    async def go():
        call = make_call(tmp_path)
        owner = (await call.start())["owner"]
        chunk = base64.b64encode(b"\1\0" * 160).decode()
        assert await call.audio(chunk, owner)
        assert (await call.mute(True, "someone-else"))["ok"] is False  # not your call
        assert (await call.mute(True, owner))["muted"] is True
        await call.mute(True, owner)  # twice: one audioStreamEnd only
        assert await call.audio(chunk, owner) is False  # in flight when the mic was cut
        session = FakeSession.instances[-1]
        assert session.audio == [b"\1\0" * 160, "END"]
        assert (await call.mute(False, owner))["muted"] is False
        assert await call.audio(chunk, owner)
        await call.mute(True, owner)
        await call.stop(owner)
        assert call.muted is False  # a new call never starts muted
        again = (await call.start())["owner"]
        assert await call.audio(chunk, again)
        assert (await call.mute("yes", again))["muted"] is False  # only a real true mutes

    asyncio.run(go())


def test_a_long_task_gets_the_same_generous_ceiling_on_both_sides():
    """Measured on the live wire: an unanswered tool call keeps the turn open
    for at least 240 s and the late answer is still spoken. The 150 s cut was
    ours, so it is gone: ten minutes, and the renderer must not be stricter."""

    import re

    js = (Path(__file__).parent.parent / "desktop" / "plugin.js").read_text()
    renderer_ms = int(re.search(r"ANSWER_TIMEOUT_MS = (\d+)", js).group(1))

    assert desktop_bridge.DELEGATION_TIMEOUT_S >= 600
    assert renderer_ms == int(desktop_bridge.DELEGATION_TIMEOUT_S * 1000)
