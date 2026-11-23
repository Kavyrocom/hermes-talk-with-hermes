import asyncio
import base64
import json
import types

import pytest

from talk_with_hermes import gemini_live, persona
from talk_with_hermes.gemini_live import GeminiLiveSession, LiveConfig, LiveError, LiveHandlers

KEY = "test-key-not-real"


class FakeSocket:
    """Scripted server. `frames` are what recv() returns, in order.
    An exception in the inbox is raised by recv(), like a dying socket."""

    def __init__(self, frames=None):
        self.sent = []
        self.closed = False
        self.inbox = asyncio.Queue()
        for frame in frames or []:
            self.inbox.put_nowait(frame)

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True

    def push(self, message):
        self.inbox.put_nowait(json.dumps(message))

    def responses(self):
        return [f for f in self.sent if "toolResponse" in f]


class StuckSocket(FakeSocket):
    """Takes the setup, then never takes an audio frame: a half-open socket
    with a full buffer. Closing it ends recv(), as websockets does."""

    async def send(self, text):
        message = json.loads(text)
        if "realtimeInput" in message or message.get("type") == "input_audio_buffer.append":
            await asyncio.Event().wait()
        await super().send(text)

    async def close(self):
        await super().close()
        self.inbox.put_nowait(Closed(1006, "abnormal closure"))


class Closed(Exception):
    """Shaped like websockets.ConnectionClosed: the close frame is in .rcvd."""

    def __init__(self, code, reason=""):
        super().__init__(f"{code} {reason}")
        self.rcvd = types.SimpleNamespace(code=code, reason=reason)


class Connector:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, url, headers):
        self.calls.append((url, headers))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def setup_ok(**extra):
    return json.dumps({"setupComplete": {}, **extra})


def with_handle(handle):
    return setup_ok(sessionResumptionUpdate={"newHandle": handle, "resumable": True})


async def no_sleep(_seconds):
    return None


async def settle(rounds=10):
    for _ in range(rounds):
        await asyncio.sleep(0)


def session_for(*outcomes, handlers=None, **kwargs):
    connect = Connector(*outcomes)
    kwargs.setdefault("sleep", no_sleep)
    return GeminiLiveSession(LiveConfig(api_key=KEY), handlers, connect=connect, **kwargs), connect


# -- setup ------------------------------------------------------------------


def test_config_refuses_missing_key_and_extended_thinking():
    with pytest.raises(LiveError):
        LiveConfig(api_key="  ").validate()
    with pytest.raises(LiveError, match="déléguer"):
        LiveConfig(api_key=KEY, model="gemini-3.8-live-extended-thinking").validate()


def test_setup_carries_persona_tool_language_and_compression():
    session, _ = session_for()
    setup = session.setup_message(resume=None)["setup"]
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["tools"] == [{"functionDeclarations": [persona.TALK_TOOL]}]
    assert persona.TALK_TOOL["parameters"]["type"] == "OBJECT"
    assert setup["inputAudioTranscription"] == {"languageCodes": ["fr-FR"]}
    assert setup["contextWindowCompression"] == {"slidingWindow": {}}
    assert setup["sessionResumption"] == {}
    # Without this the server never ends the user's turn (measured live).
    assert setup["realtimeInputConfig"]["automaticActivityDetection"]["silenceDurationMs"] == 800
    assert "Tu es Hermes" in setup["systemInstruction"]["parts"][0]["text"]


def test_key_goes_in_header_never_in_url():
    async def run():
        session, connect = session_for(FakeSocket([setup_ok()]))
        await session.start()
        await session.close()
        return connect.calls

    (url, headers), = asyncio.run(run())
    assert KEY not in url
    assert headers == {"x-goog-api-key": KEY}


def test_refused_setup_message_is_speakable_and_closes_the_socket():
    async def run():
        socket = FakeSocket([json.dumps({"error": {"message": "API key not valid"}})])
        session, _ = session_for(socket)
        with pytest.raises(LiveError, match="refusé") as caught:
            await session.start()
        return socket, caught.value

    socket, error = asyncio.run(run())
    assert socket.closed and error.refused


def test_refusal_by_close_frame_keeps_the_reason():
    async def run():
        socket = FakeSocket([Closed(1008, "API key not valid. Please pass a valid API key.")])
        session, _ = session_for(socket)
        with pytest.raises(LiveError) as caught:
            await session.start()
        return caught.value

    error = asyncio.run(run())
    assert error.refused
    assert "API key not valid" in str(error)


def test_close_during_setup_does_not_leak_the_socket():
    async def run():
        socket = FakeSocket()  # never answers setup
        session, _ = session_for(socket)
        starting = asyncio.ensure_future(session.start())
        await settle()
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        return socket

    assert asyncio.run(run()).closed


# -- streaming ----------------------------------------------------------------


def test_audio_out_is_base64_pcm_at_16k():
    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket)
        await session.start()
        await session.send_audio(b"\x01\x02\x03\x04")
        await session.close()
        return socket.sent

    audio = asyncio.run(run())[1]["realtimeInput"]["audio"]
    assert audio["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(audio["data"]) == b"\x01\x02\x03\x04"


def test_server_frames_reach_the_handlers_even_in_binary():
    got = {"audio": [], "in": [], "out": [], "interrupted": 0, "turns": 0}
    handlers = LiveHandlers(
        on_audio=got["audio"].append,
        on_input_text=got["in"].append,
        on_output_text=got["out"].append,
        on_interrupted=lambda: got.__setitem__("interrupted", got["interrupted"] + 1),
        on_turn_complete=lambda: got.__setitem__("turns", got["turns"] + 1),
    )

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, handlers=handlers)
        await session.start()
        socket.inbox.put_nowait(b"not json")
        socket.inbox.put_nowait(json.dumps({"serverContent": {
            "inputTranscription": {"text": "bonjour"},
            "modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(b"\x00\x01").decode()}}]},
            "outputTranscription": {"text": "salut"},
            "interrupted": True,
            "turnComplete": True,
        }}).encode())
        await settle()
        await session.close()

    asyncio.run(run())
    assert got == {"audio": [b"\x00\x01"], "in": ["bonjour"], "out": ["salut"], "interrupted": 1, "turns": 1}


def test_a_field_of_the_wrong_type_drops_its_frame_not_the_call():
    """Wire fact: one malformed frame must not kill the call. A field of the
    wrong type is a malformed frame too, not a dead socket."""

    got = {"audio": [], "turns": 0, "closed": []}
    handlers = LiveHandlers(
        on_audio=got["audio"].append,
        on_turn_complete=lambda: got.__setitem__("turns", got["turns"] + 1),
        on_closed=got["closed"].append,
    )

    async def run():
        socket = FakeSocket([setup_ok()])
        session, connect = session_for(socket, handlers=handlers)
        await session.start()
        for frame in (
            {"serverContent": {"modelTurn": ["not", "an", "object"]}},
            {"serverContent": {"inputTranscription": "bonjour"}},
            {"serverContent": {"modelTurn": {"parts": 5}}},
            {"serverContent": {"modelTurn": {"parts": [{"inlineData": "AAE="}]}}},
            {"toolCall": {"functionCalls": 7}},
            {"toolCallCancellation": {"ids": 3}},
            {"serverContent": {
                "modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(b"\x00\x01").decode()}}]},
                "turnComplete": True,
            }},
        ):
            socket.push(frame)
        await settle()
        live = session.live
        await session.close()
        return live, len(connect.calls)

    live, dials = asyncio.run(run())
    assert got == {"audio": [b"\x00\x01"], "turns": 1, "closed": []}
    assert live is True and dials == 1  # the call went on, never redialed


def test_a_failing_handler_never_ends_the_call():
    chunks = []

    def on_audio(chunk):
        chunks.append(chunk)
        if len(chunks) == 1:
            raise RuntimeError("consumer bug")

    async def run():
        socket = FakeSocket([with_handle("h1")])
        session, connect = session_for(socket, handlers=LiveHandlers(on_audio=on_audio))
        await session.start()
        for data in (b"\x01", b"\x02"):
            socket.push({"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"data": base64.b64encode(data).decode()}}]}}})
        await settle()
        live = session.live
        await session.close()
        return live, len(connect.calls)

    assert asyncio.run(run()) == (True, 1)
    assert chunks == [b"\x01", b"\x02"]


# -- delegation ---------------------------------------------------------------


def test_talk_answer_is_sent_back_by_id():
    seen = []

    async def on_tool_call(call_id, request):
        seen.append((call_id, request))
        return "Trois ventes."

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, handlers=LiveHandlers(on_tool_call=on_tool_call))
        await session.start()
        socket.push({"toolCall": {"functionCalls": [
            {"id": "c1", "name": "talk", "args": {"request": "ventes de la semaine"}},
        ]}})
        await settle()
        await session.close()
        return socket.responses()

    responses = asyncio.run(run())
    assert seen == [("c1", "ventes de la semaine")]
    assert responses == [{"toolResponse": {"functionResponses": [
        {"id": "c1", "response": {"output": "Trois ventes."}},
    ]}}]


def test_a_failed_delegation_still_answers_so_the_model_does_not_hang():
    async def on_tool_call(call_id, request):
        raise RuntimeError("agent down")

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, handlers=LiveHandlers(on_tool_call=on_tool_call))
        await session.start()
        socket.push({"toolCall": {"functionCalls": [{"id": "c1", "name": "talk", "args": {"request": "x"}}]}})
        await settle()
        await session.close()
        return socket.responses()

    (response,) = asyncio.run(run())
    output = response["toolResponse"]["functionResponses"][0]
    assert output["id"] == "c1"
    assert output["response"]["output"] == gemini_live.DELEGATION_FAILED


def test_unknown_tool_gets_an_answer_so_the_model_does_not_hang():
    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket)
        await session.start()
        socket.push({"toolCall": {"functionCalls": [{"id": "x", "name": "shell", "args": {}}]}})
        await settle()
        await session.close()
        return socket.responses()

    (response,) = asyncio.run(run())
    output = response["toolResponse"]["functionResponses"][0]
    assert output["id"] == "x" and "talk" in output["response"]["output"]


def test_cancellation_stops_the_delegation_and_sends_nothing():
    stopped, notified = [], []

    async def on_tool_call(call_id, request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(call_id)
            raise

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, handlers=LiveHandlers(
            on_tool_call=on_tool_call, on_tool_cancelled=notified.append))
        await session.start()
        socket.push({"toolCall": {"functionCalls": [{"id": "c1", "name": "talk", "args": {"request": "x"}}]}})
        await settle()
        socket.push({"toolCallCancellation": {"ids": ["c1"]}})
        await settle()
        late = await session.send_tool_response("c1", "trop tard")
        await session.close()
        return late, socket.responses()

    late, responses = asyncio.run(run())
    assert stopped == ["c1"] and notified == ["c1"]
    assert late == gemini_live.CANCELLED
    assert responses == []


# -- resumption and redial ----------------------------------------------------


def test_resumption_handle_is_stored_and_invalidated():
    saved = []

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, save_handle=saved.append)
        await session.start()
        socket.push({"sessionResumptionUpdate": {"newHandle": "h1", "resumable": True}})
        await settle()
        first = session.handle
        socket.push({"sessionResumptionUpdate": {"resumable": False}})
        await settle()
        await session.close()
        return first, session.handle

    assert asyncio.run(run()) == ("h1", None)
    assert saved == ["h1", None]


def test_unexpected_drop_redials_with_the_handle():
    closed = []

    async def run():
        first, second = FakeSocket([with_handle("h1")]), FakeSocket([setup_ok()])
        session, _ = session_for(first, second, handlers=LiveHandlers(on_closed=closed.append))
        await session.start()
        first.inbox.put_nowait(ConnectionResetError("boom"))
        await settle()
        live = session.live
        await session.close()
        return live, second.sent

    live, resent = asyncio.run(run())
    assert live is True
    assert resent[0]["setup"]["sessionResumption"] == {"handle": "h1"}
    assert closed == []


def test_a_long_call_survives_any_number_of_go_away_rotations():
    closed = []

    async def run():
        sockets = [FakeSocket([with_handle(f"h{i}")]) for i in range(6)]
        session, connect = session_for(*sockets, handlers=LiveHandlers(on_closed=closed.append))
        await session.start()
        for current in sockets[:5]:
            current.push({"goAway": {"timeLeft": "5s"}})
            current.inbox.put_nowait(ConnectionResetError("server closed after timeLeft"))
            await settle()
        live = session.live
        await session.close()
        return live, len(connect.calls)

    assert asyncio.run(run()) == (True, 6)
    assert closed == []


def test_a_server_that_drops_every_connection_at_once_ends_the_call():
    """Setup succeeds, the socket dies right after: the redials must run out
    instead of looping every 1.5 s for as long as the server accepts."""

    closed = []

    async def run():
        sockets = [FakeSocket([with_handle("h")]) for _ in range(8)]
        session, connect = session_for(*sockets, handlers=LiveHandlers(on_closed=closed.append))
        await session.start()
        for current in sockets:
            current.inbox.put_nowait(ConnectionResetError("dropped at once"))
            await settle()
        return session.live, len(connect.calls)

    live, dials = asyncio.run(run())
    assert live is False and dials == 1 + gemini_live.MAX_REDIALS
    assert len(closed) == 1 and "ConnectionResetError" in closed[0]


def test_a_connection_that_held_earns_a_fresh_redial_budget():
    closed = []
    now = [0.0]

    async def run():
        sockets = [FakeSocket([with_handle(f"h{i}")]) for i in range(6)]
        session, connect = session_for(*sockets, handlers=LiveHandlers(on_closed=closed.append),
                                       clock=lambda: now[0])
        await session.start()
        for current in sockets[:5]:
            now[0] += gemini_live.STABLE_AFTER_S
            current.inbox.put_nowait(ConnectionResetError("network blip"))
            await settle()
        live = session.live
        await session.close()
        return live, len(connect.calls)

    assert asyncio.run(run()) == (True, 6)
    assert closed == []


def test_a_socket_that_takes_no_frame_is_closed_and_redialed(monkeypatch):
    """Otherwise the microphone pump waits forever and the call goes deaf
    with nothing shown."""

    monkeypatch.setattr(gemini_live, "SEND_TIMEOUT_S", 0.05)

    async def run():
        stuck, fresh = StuckSocket([with_handle("h1")]), FakeSocket([setup_ok()])
        session, connect = session_for(stuck, fresh)
        await session.start()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(session.send_audio(b"\x00\x00"), 2)
        await settle()
        live = session.live
        await session.close()
        return stuck.closed, live, len(connect.calls), fresh.sent[0]["setup"]["sessionResumption"]

    assert asyncio.run(run()) == (True, True, 2, {"handle": "h1"})


def test_go_away_keeps_reading_until_the_server_closes():
    heard = []

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, handlers=LiveHandlers(on_output_text=heard.append))
        await session.start()
        socket.push({"goAway": {"timeLeft": "5s"}})
        socket.push({"serverContent": {"outputTranscription": {"text": "fin de phrase"}}})
        await settle()
        live = session.live
        await session.close()
        return live

    assert asyncio.run(run()) is True
    assert heard == ["fin de phrase"]


def test_go_away_without_handle_ends_the_call_with_an_awaited_reason():
    closed = []

    async def on_closed(reason):
        await asyncio.sleep(0)
        closed.append(reason)

    async def run():
        socket = FakeSocket([setup_ok()])
        session, _ = session_for(socket, handlers=LiveHandlers(on_closed=on_closed))
        await session.start()
        socket.push({"goAway": {"timeLeft": "5s"}})
        socket.inbox.put_nowait(ConnectionResetError("closed"))
        await settle()
        return session.live

    assert asyncio.run(run()) is False
    assert closed == ["goAway"]


def test_a_network_failure_on_redial_keeps_the_handle():
    saved = []

    async def run():
        first, third = FakeSocket([with_handle("h1")]), FakeSocket([setup_ok()])
        session, _ = session_for(first, OSError("network down"), third, save_handle=saved.append)
        await session.start()
        first.inbox.put_nowait(ConnectionResetError("boom"))
        await settle()
        live = session.live
        await session.close()
        return live, third.sent, session.handle

    live, sent, handle = asyncio.run(run())
    assert live is True
    assert sent[0]["setup"]["sessionResumption"] == {"handle": "h1"}
    assert handle == "h1" and None not in saved


def test_a_refused_handle_falls_back_to_a_fresh_session_and_says_so():
    lost = []

    async def run():
        refused = FakeSocket([json.dumps({"error": {"message": "handle expired"}})])
        fresh = FakeSocket([setup_ok()])
        session, _ = session_for(refused, fresh, handle="old",
                                 handlers=LiveHandlers(on_context_lost=lambda: lost.append(True)))
        await session.start()
        await session.close()
        return refused.sent, fresh.sent, session.handle

    refused_sent, fresh_sent, handle = asyncio.run(run())
    assert refused_sent[0]["setup"]["sessionResumption"] == {"handle": "old"}
    assert fresh_sent[0]["setup"]["sessionResumption"] == {}
    assert handle is None and lost == [True]


def test_refusal_mid_call_ends_it_without_redialing():
    closed = []

    async def run():
        socket = FakeSocket([with_handle("h1")])
        session, connect = session_for(socket, handlers=LiveHandlers(on_closed=closed.append))
        await session.start()
        socket.inbox.put_nowait(Closed(1008, "quota exceeded"))
        await settle()
        return session.live, len(connect.calls)

    assert asyncio.run(run()) == (False, 1)
    assert closed and "quota exceeded" in closed[0]


def test_an_answer_ready_during_redial_is_queued_then_sent_on_resume():
    async def run():
        gate = asyncio.Event()

        async def gated_sleep(_seconds):
            await gate.wait()

        first, second = FakeSocket([with_handle("h1")]), FakeSocket([setup_ok()])
        session, _ = session_for(first, second, sleep=gated_sleep)
        await session.start()
        first.inbox.put_nowait(ConnectionResetError("boom"))
        await settle()
        status = await session.send_tool_response("c1", "Trois ventes.")
        gate.set()
        await settle()
        await session.close()
        return status, second.responses()

    status, responses = asyncio.run(run())
    assert status == gemini_live.QUEUED
    assert responses == [{"toolResponse": {"functionResponses": [
        {"id": "c1", "response": {"output": "Trois ventes."}},
    ]}}]


def test_a_queued_answer_is_dropped_when_the_context_is_lost():
    async def run():
        gate = asyncio.Event()

        async def gated_sleep(_seconds):
            await gate.wait()

        first = FakeSocket([with_handle("h1")])
        refused = FakeSocket([json.dumps({"error": {"message": "handle expired"}})])
        fresh = FakeSocket([setup_ok()])
        session, _ = session_for(first, refused, fresh, sleep=gated_sleep)
        await session.start()
        first.inbox.put_nowait(ConnectionResetError("boom"))
        await settle()
        await session.send_tool_response("c1", "Trois ventes.")
        gate.set()
        await settle()
        live = session.live
        await session.close()
        return live, fresh.responses()

    assert asyncio.run(run()) == (True, [])


def test_manual_close_never_redials_and_drops_late_answers():
    closed = []

    async def run():
        socket = FakeSocket([with_handle("h1")])
        session, connect = session_for(socket, handlers=LiveHandlers(on_closed=closed.append))
        await session.start()
        await session.close()
        await settle()
        late = await session.send_tool_response("c1", "trop tard")
        return len(connect.calls), session.live, late

    assert asyncio.run(run()) == (1, False, gemini_live.DROPPED)
    assert closed == []
