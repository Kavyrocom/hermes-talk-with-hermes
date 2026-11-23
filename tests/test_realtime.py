"""OpenAI Realtime / Grok Voice wire, against a scripted server.

Frames follow the docs (see talk_with_hermes/realtime.py). No live receipt
yet: these prove our side of the protocol, not the services' answers."""

import asyncio
import base64
import json
import types

import numpy as np
import pytest

from talk_with_hermes import persona, realtime, services
from talk_with_hermes.audio import Upsample16To24
from talk_with_hermes.gemini_live import LiveConfig, LiveError, LiveHandlers
from talk_with_hermes.prefs import Word
from talk_with_hermes.realtime import OPENAI, XAI, RealtimeSession

from test_gemini_live import Closed, Connector, FakeSocket, StuckSocket, settle

KEY = "test-key-not-real"
READY = json.dumps({"type": "session.updated", "session": {}})


def cfg(model="grok-voice-latest", **extra):
    return LiveConfig(api_key=KEY, model=model, voice="rex", **extra)


def session_for(*outcomes, dialect=XAI, handlers=None, **config):
    connect = Connector(*outcomes)
    model = "gpt-realtime-2.1" if dialect is OPENAI else "grok-voice-latest"
    return RealtimeSession(cfg(model, **config), handlers, dialect=dialect, connect=connect), connect


def kinds(socket):
    return [frame["type"] for frame in socket.sent]


# -- setup -------------------------------------------------------------------


def test_grok_dials_its_url_with_a_bearer_and_waits_for_the_ack():
    async def run():
        socket = FakeSocket([json.dumps({"type": "conversation.created"}), READY])
        session, connect = session_for(socket)
        await session.start()
        await session.close()
        return socket, connect

    socket, connect = asyncio.run(run())
    url, headers = connect.calls[0]
    assert url == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert headers == {"Authorization": f"Bearer {KEY}"}
    update = socket.sent[0]
    assert update["type"] == "session.update"
    sess = update["session"]
    assert sess["voice"] == "rex" and sess["turn_detection"] == {"type": "server_vad", "silence_duration_ms": 800}
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert sess["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    tool = sess["tools"][0]
    assert tool["type"] == "function" and tool["name"] == persona.TALK_TOOL_NAME
    assert tool["parameters"]["type"] == "object" and tool["parameters"]["properties"]["request"]["type"] == "string"
    assert "Hermes" in sess["instructions"]


def test_grok_gets_the_language_and_the_vocabulary_as_keyterms():
    session, _ = session_for(languages=("fr-FR",), words=(Word("Kavyro", ("Kavirro",)), Word("Hermes")))
    transcription = session.session_updates()[0]["session"]["audio"]["input"]["transcription"]
    assert transcription == {"language_hint": "fr", "keyterms": ["Kavyro", "Hermes"]}


def test_openai_session_shape_and_its_own_transcription_update():
    session, _ = session_for(dialect=OPENAI, words=(Word("Kavyro"),))
    first, second = session.session_updates()
    sess = first["session"]
    assert sess["type"] == "realtime" and sess["model"] == "gpt-realtime-2.1"
    assert sess["output_modalities"] == ["audio"] and sess["audio"]["output"]["voice"] == "rex"
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    spec = second["session"]["audio"]["input"]["transcription"]
    assert spec["language"] == "fr" and "Kavyro" in spec["prompt"]


def test_the_agent_style_reaches_the_instructions():
    session, _ = session_for(name="Iris", style="Calme, direct, précis.")
    assert "Calme, direct, précis." in session.session_updates()[0]["session"]["instructions"]


def test_an_error_before_the_ack_is_a_refusal():
    async def run():
        socket = FakeSocket([json.dumps({"type": "error", "error": {"message": "Unknown voice"}})])
        session, _ = session_for(socket)
        with pytest.raises(LiveError) as caught:
            await session.start()
        return socket, caught.value

    socket, error = asyncio.run(run())
    assert error.refused and "Grok Voice a refusé la session : Unknown voice" in str(error) and socket.closed


def test_a_refused_handshake_says_the_status_and_the_detail():
    response = types.SimpleNamespace(status_code=402, body=json.dumps({"error": "used all available credits"}).encode())

    class Rejected(Exception):
        response = None

    exc = Rejected("server rejected WebSocket connection: HTTP 402")
    exc.response = response

    async def run():
        session, _ = session_for(exc)
        with pytest.raises(LiveError) as caught:
            await session.start()
        return caught.value

    error = asyncio.run(run())
    assert error.refused and "(402)" in str(error) and "used all available credits" in str(error)


def test_no_key_no_session():
    with pytest.raises(LiveError, match="Il manque la clé OpenAI Realtime"):
        RealtimeSession(LiveConfig(api_key=" "), dialect=OPENAI)


# -- audio -------------------------------------------------------------------


def pcm16k(n):
    return (np.arange(n) % 200 * 50).astype("<i2").tobytes()


def test_grok_takes_the_16k_microphone_as_is_and_openai_gets_24k():
    async def run(dialect):
        socket = FakeSocket([READY])
        session, _ = session_for(socket, dialect=dialect)
        await session.start()
        await session.send_audio(pcm16k(1600))
        await session.close()
        return [f for f in socket.sent if f["type"] == "input_audio_buffer.append"]

    grok = asyncio.run(run(XAI))
    assert base64.b64decode(grok[0]["audio"]) == pcm16k(1600)
    openai = asyncio.run(run(OPENAI))
    assert len(base64.b64decode(openai[0]["audio"])) // 2 == len(Upsample16To24().feed(pcm16k(1600))) // 2 == 2399


def test_mic_off_mid_sentence_commits_and_asks_for_the_answer():
    async def run(server_committed):
        socket = FakeSocket([READY])
        session, _ = session_for(socket, dialect=OPENAI)
        await session.start()
        await session.end_audio()  # nothing heard yet: nothing to close
        silent = list(kinds(socket))
        await session.send_audio(pcm16k(1600))
        if server_committed:
            socket.push({"type": "input_audio_buffer.committed"})
            await settle()
        await session.end_audio()
        await session.close()
        return silent, kinds(socket)

    silent, sent = asyncio.run(run(False))
    assert silent == ["session.update", "session.update"]  # session + transcription only
    assert sent[-2:] == ["input_audio_buffer.commit", "response.create"]  # a commit alone never answers
    _, sent = asyncio.run(run(True))
    assert sent[-1] == "input_audio_buffer.append"  # the server VAD already closed the turn


def test_parallel_calls_are_spoken_once_after_the_last_result():
    gate = {}

    async def agent(call_id, request):
        gate[call_id] = asyncio.get_running_loop().create_future()
        return await gate[call_id]

    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=LiveHandlers(on_tool_call=agent))
        await session.start()
        socket.push(call("a"))
        socket.push(call("b"))
        await settle()
        gate["a"].set_result("un")
        await settle()
        after_first = kinds(socket).count("response.create")
        gate["b"].set_result("deux")
        await settle()
        await session.close()
        return after_first, kinds(socket)

    after_first, sent = asyncio.run(run())
    assert after_first == 0 and sent.count("conversation.item.create") == 2 and sent.count("response.create") == 1


def test_xai_language_hint_keeps_the_region_it_requires():
    hint = lambda *codes: realtime._xai_language(cfg(languages=codes))  # noqa: E731
    assert (hint("fr-FR"), hint("es"), hint("es-mx"), hint("pt"), hint("en-US")) == ("fr", "es-ES", "es-MX", "pt-BR", "en")


def test_both_audio_event_names_play_and_transcripts_flow():
    heard, said, played, flushed = [], [], [], []
    handlers = LiveHandlers(on_audio=played.append, on_output_text=said.append,
                            on_input_text=heard.append, on_interrupted=lambda: flushed.append(1))

    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=handlers)
        await session.start()
        socket.push({"type": "response.output_audio.delta", "delta": base64.b64encode(b"ab").decode()})
        socket.push({"type": "response.audio.delta", "delta": base64.b64encode(b"cd").decode()})
        socket.push({"type": "response.output_audio_transcript.delta", "delta": "Salut"})
        socket.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "bonjour"})
        socket.push({"type": "input_audio_buffer.speech_started"})
        await settle()
        await session.close()

    asyncio.run(run())
    assert played == [b"ab", b"cd"] and said == ["Salut"] and heard == ["bonjour"] and flushed == [1]


def test_a_socket_that_takes_no_audio_is_closed_and_the_call_ends(monkeypatch):
    monkeypatch.setattr(realtime, "SEND_TIMEOUT_S", 0.05)
    reasons = []

    async def run():
        stuck = StuckSocket([READY])
        session, _ = session_for(stuck, handlers=LiveHandlers(on_closed=reasons.append))
        await session.start()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(session.send_audio(b"\x00\x00" * 160), 2)
        await settle()
        return stuck.closed, session.live

    assert asyncio.run(run()) == (True, False)
    assert len(reasons) == 1


# -- the talk tool -----------------------------------------------------------


def call(call_id="c1", request="Quel temps ?", name=persona.TALK_TOOL_NAME):
    return {"type": "response.function_call_arguments.done", "call_id": call_id, "name": name,
            "arguments": json.dumps({"request": request})}


def test_a_tool_call_is_delegated_once_and_answered_after_the_response_ends():
    asked = []

    async def agent(call_id, request):
        asked.append((call_id, request))
        return "Il fait beau."

    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=LiveHandlers(on_tool_call=agent))
        await session.start()
        socket.push({"type": "response.created"})
        socket.push(call())
        socket.push(call())  # echoed again: run once
        await settle()
        before = kinds(socket)
        socket.push({"type": "response.done"})
        await settle()
        await session.close()
        return socket, before

    socket, before = asyncio.run(run())
    assert asked == [("c1", "Quel temps ?")]
    outputs = [f for f in socket.sent if f["type"] == "conversation.item.create"]
    assert outputs == [{"type": "conversation.item.create",
                        "item": {"type": "function_call_output", "call_id": "c1", "output": "Il fait beau."}}]
    assert "response.create" not in before  # the server refuses one while a response runs
    assert kinds(socket).count("response.create") == 1


def test_an_answer_after_the_response_ended_asks_for_speech_at_once():
    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=LiveHandlers(on_tool_call=lambda c, r: "ok"))
        await session.start()
        socket.push({"type": "response.created"})
        socket.push({"type": "response.done"})
        socket.push(call())
        await settle()
        await session.close()
        return socket

    sent = kinds(asyncio.run(run()))
    assert sent[-2:] == ["conversation.item.create", "response.create"]


def test_an_unknown_tool_and_a_failing_agent_are_answered_not_dropped():
    def boom(call_id, request):
        raise RuntimeError("down")

    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=LiveHandlers(on_tool_call=boom))
        await session.start()
        socket.push(call("x", name="shell"))
        socket.push(call("y"))
        await settle()
        await session.close()
        return {f["item"]["call_id"]: f["item"]["output"] for f in socket.sent if f["type"] == "conversation.item.create"}

    outputs = asyncio.run(run())
    assert outputs["x"].startswith("Outil inconnu") and outputs["y"].startswith("La délégation a échoué")


# -- end ---------------------------------------------------------------------


def test_a_dropped_socket_ends_the_call_with_its_reason():
    reasons = []

    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=LiveHandlers(on_closed=reasons.append))
        await session.start()
        socket.inbox.put_nowait(Closed(1011, "internal"))
        await settle()
        return session

    session = asyncio.run(run())
    assert reasons == ["Grok Voice a fermé la session : internal"] and not session.live


def test_a_manual_close_says_nothing():
    reasons = []

    async def run():
        socket = FakeSocket([READY])
        session, _ = session_for(socket, handlers=LiveHandlers(on_closed=reasons.append))
        await session.start()
        await session.close()
        return socket

    assert asyncio.run(run()).closed and reasons == []


def test_ollama_is_not_a_voice_service():
    """Ollama only synthesises (no listening, no conversation): not offered."""

    assert set(services.SERVICES) == {"gemini", "openai", "grok"}
    assert all(not m.startswith(("phonon", "ollama")) for m in services.all_models())
