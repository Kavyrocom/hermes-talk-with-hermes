"""Gemini Live duplex session. One socket, one conversation, one tool.

Wire facts this module is written against:
- Live probe, 2026-08-28 (quarry `talk_gemini_realtime.py`): setup is the first
  message and is acked by `setupComplete`; tool schema types are UPPERCASE;
  `functionCalls[].args` is a parsed dict; output audio is
  `serverContent.modelTurn.parts[].inlineData` at 24 kHz; JSON can arrive in
  BINARY frames; one malformed frame must not kill the call.
- Reference-verified: `toolCallCancellation.ids` results must never go
  upstream; `resumable: false` invalidates the stored handle;
  `contextWindowCompression.slidingWindow` lifts the ~15 minute audio cap;
  `goAway` announces termination, the socket closes after `timeLeft`.
- Refusals usually arrive as a close frame (1007/1008 plus a reason), not as a
  JSON `error` message. Both are read.
- cruzlxyz/livetalk (MIT): the setup shape, transcription language hints and
  the redial-with-handle policy.

The API key travels in the `x-goog-api-key` header, never in the URL.
Live-proven 2026-11-20: a fake key in that header is refused with "API key
not valid", so the header is read and the refusal path works end to end.
Not live-proven yet: `languageCodes`, the model id, and whether a resumed
session accepts a toolResponse for a call made before the drop.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from talk_with_hermes import persona

log = logging.getLogger(__name__)

WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
DEFAULT_MODEL = "gemini-3.8-live"
DEFAULT_VOICE = "Puck"
#: The 30 prebuilt voices Gemini Live accepts in `voice_name`, with Google's
#: one-word character (Google Cloud "Configure language and voice", read
#: 2026-11-21). The page translates the trait. The Extended Voice Library is
#: documented for TTS, not Live.
VOICES: tuple[tuple[str, str], ...] = (
    ("Zephyr", "bright"), ("Puck", "upbeat"), ("Charon", "informative"), ("Kore", "firm"),
    ("Fenrir", "excitable"), ("Leda", "youthful"), ("Orus", "firm"), ("Aoede", "breezy"),
    ("Callirrhoe", "easygoing"), ("Autonoe", "bright"), ("Enceladus", "breathy"), ("Iapetus", "clear"),
    ("Umbriel", "easygoing"), ("Algieba", "smooth"), ("Despina", "smooth"), ("Erinome", "clear"),
    ("Algenib", "gravelly"), ("Rasalgethi", "informative"), ("Laomedeia", "upbeat"), ("Achernar", "soft"),
    ("Alnilam", "firm"), ("Schedar", "even"), ("Gacrux", "mature"), ("Pulcherrima", "forward"),
    ("Achird", "friendly"), ("Zubenelgenubi", "casual"), ("Vindemiatrix", "gentle"),
    ("Sadachbia", "lively"), ("Sadaltager", "knowledgeable"), ("Sulafat", "warm"),
)


def voices() -> list[dict[str, str]]:
    """What this service can speak with: the adapter's contract for the page."""

    return [{"id": name, "label": name, "trait": trait} for name, trait in VOICES]


INPUT_MIME = "audio/pcm;rate=16000"
SETUP_TIMEOUT_S = 10.0
#: A frame the socket has not taken in this long means a half-open socket
#: with a full buffer: it is closed, and the call redials or ends as for any
#: drop, instead of the microphone pump waiting on it forever.
SEND_TIMEOUT_S = 10.0
#: Reconnections in a row that did not hold before the call ends: a redial
#: that failed, or a connection that dropped within STABLE_AFTER_S.
MAX_REDIALS = 2
REDIAL_DELAY_S = 1.5
#: A connection that stayed up this long earns a fresh redial budget when it
#: drops, and a goAway rotation (the server's own notice) always does: a long
#: call survives any number of them. One that drops sooner spends the budget,
#: so a server that accepts then closes at once ends the call instead of
#: being redialed every REDIAL_DELAY_S for as long as it accepts.
STABLE_AFTER_S = 30.0
MAX_REQUEST_CHARS = 2000
MAX_ANSWER_CHARS = 8000
MAX_CANCELLED_IDS = 1024
#: Close codes that mean "this request is refused": retrying gives the same answer.
REFUSAL_CLOSE_CODES = frozenset({1007, 1008})

DELEGATION_FAILED = "La délégation a échoué, l'agent n'a pas pu répondre. Dis-le à l'opérateur."
UNKNOWN_TOOL = "Outil inconnu. Seul talk existe."
EMPTY_ANSWER = "L'agent n'a rien renvoyé."

SENT = "sent"
QUEUED = "queued"
CANCELLED = "cancelled"
DROPPED = "dropped"


class LiveError(Exception):
    """A speakable reason the session cannot start or continue.

    `refused` is True only when Gemini itself rejected the request, as opposed
    to the network failing. Only a refusal may invalidate a resumption handle.
    """

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


@dataclass(frozen=True)
class LiveConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    languages: tuple[str, ...] = ("fr-FR",)
    spoken_answers: str = "full"
    address: str = "tu"
    words: tuple = ()
    name: str = "Hermes"
    intro: str = ""
    #: The agent's own `## Style` section (identity.style_from_soul).
    style: str = ""
    #: A recalled conversation or session, already fenced (persona.context_block).
    context: str = ""

    def validate(self) -> None:
        if not (self.api_key or "").strip():
            raise LiveError("Il manque la clé Gemini (GEMINI_API_KEY).")
        if "extended-thinking" in self.model:
            # livetalk, Nov 2026: these models never emit the tool call, so
            # the voice could talk but never hand work to the agent.
            raise LiveError(f"Le modèle {self.model} ne sait pas déléguer. Choisis un modèle Live standard.")


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass
class LiveHandlers:
    """Callbacks. A handler that raises is logged and never ends the call.

    `on_tool_call(call_id, request)` returns the agent's answer, or an
    awaitable of it. The session sends it upstream; a failure sends a
    speakable error instead, so the model never waits forever. When Gemini
    cancels the call, the awaitable is cancelled: the delegation code must
    stop the agent run on `CancelledError`.

    `on_closed(reason)` may be async; it is awaited.
    """

    on_audio: Callable[[bytes], Any] = _noop
    on_interrupted: Callable[[], Any] = _noop
    on_tool_call: Callable[[str, str], Any] = _noop
    on_tool_cancelled: Callable[[str], Any] = _noop
    on_input_text: Callable[[str], Any] = _noop
    on_output_text: Callable[[str], Any] = _noop
    on_turn_complete: Callable[[], Any] = _noop
    on_context_lost: Callable[[], Any] = _noop
    on_closed: Callable[[str], Any] = _noop


Connector = Callable[[str, dict[str, str]], Awaitable[Any]]


async def websocket_connect(url: str, headers: dict[str, str]) -> Any:
    import websockets

    return await websockets.connect(
        url, additional_headers=headers, open_timeout=10, close_timeout=5, max_size=None
    )


def _decode(frame: Any) -> dict | None:
    if isinstance(frame, (bytes, bytearray)):
        frame = bytes(frame).decode("utf-8", "replace")
    try:
        message = json.loads(frame)
    except (TypeError, ValueError):
        return None
    return message if isinstance(message, dict) else None


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        error = error.get("message") or error
    return str(error)[:200]


def _close_frame(exc: BaseException) -> tuple[int | None, str]:
    """Code and reason of a websockets ConnectionClosed, or (None, "")."""

    received = getattr(exc, "rcvd", None)
    code = getattr(received, "code", None)
    reason = str(getattr(received, "reason", "") or "")[:200]
    return (code if isinstance(code, int) else None), reason


@dataclass
class _State:
    socket: Any = None
    reader: asyncio.Task | None = None
    manual: bool = False
    redials: int = 0
    opened_at: float = 0.0
    going_away: bool = False
    cancelled: deque = field(default_factory=lambda: deque(maxlen=MAX_CANCELLED_IDS))
    tool_tasks: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)


class GeminiLiveSession:
    def __init__(
        self,
        config: LiveConfig,
        handlers: LiveHandlers | None = None,
        *,
        connect: Connector = websocket_connect,
        handle: str | None = None,
        save_handle: Callable[[str | None], Any] = _noop,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self.handlers = handlers or LiveHandlers()
        self._connect = connect
        self._handle = handle
        self._save_handle = save_handle
        self._sleep = sleep
        self._clock = clock
        self._state = _State()

    # -- public -----------------------------------------------------------

    @property
    def live(self) -> bool:
        return self._state.socket is not None

    @property
    def handle(self) -> str | None:
        return self._handle

    async def start(self) -> None:
        self._state.manual = False
        self._state.redials = 0
        await self._dial()

    async def send_audio(self, pcm16k_mono: bytes) -> None:
        if not pcm16k_mono or self._state.socket is None:
            return
        payload = base64.b64encode(pcm16k_mono).decode("ascii")
        await self._send({"realtimeInput": {"audio": {"data": payload, "mimeType": INPUT_MIME}}})

    async def end_audio(self) -> None:
        """The microphone is off: Gemini closes the turn it was hearing now,
        instead of gluing it to whatever comes after the mute."""

        if self._state.socket is None:
            return
        await self._send({"realtimeInput": {"audioStreamEnd": True}})

    async def send_tool_response(self, call_id: str, answer: str) -> str:
        """SENT, QUEUED (socket down, replayed after a resumed redial),
        CANCELLED (Gemini withdrew the call) or DROPPED (session over)."""

        if not call_id:
            return DROPPED
        if call_id in self._state.cancelled:
            return CANCELLED
        output = (answer or "").strip()[:MAX_ANSWER_CHARS] or EMPTY_ANSWER
        if self._state.socket is None:
            if self._state.manual:
                return DROPPED
            self._state.pending.append((call_id, output))
            return QUEUED
        await self._send_response(call_id, output)
        return SENT

    async def close(self) -> None:
        self._state.manual = True
        self._state.pending.clear()
        reader = self._state.reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        for task in list(self._state.tool_tasks.values()):
            if task is not asyncio.current_task():
                task.cancel()
        await self._drop_socket()

    # -- dial ---------------------------------------------------------------

    def setup_message(self, *, resume: str | None) -> dict:
        cfg = self.config
        transcription: dict[str, Any] = {}
        if cfg.languages:
            transcription["languageCodes"] = list(cfg.languages)
        return {
            "setup": {
                "model": cfg.model if cfg.model.startswith("models/") else f"models/{cfg.model}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg.voice}}},
                },
                "systemInstruction": {"parts": [{"text": persona.system_instruction(cfg.spoken_answers, cfg.address, cfg.words, cfg.name, cfg.intro, cfg.context, cfg.style)}]},
                "tools": [{"functionDeclarations": [persona.TALK_TOOL]}],
                "inputAudioTranscription": transcription,
                # Measured live on gemini-3.8-live (21/11/2026): with no VAD
                # config the server emits ACTIVITY_START and then never
                # ACTIVITY_END, so it hears you forever and never answers.
                # Naming the silence budget restores the end of turn: the
                # transcript arrives ~5 s in and the model answers. Barge-in
                # stays on, automatic detection is still what drives it.
                "realtimeInputConfig": {"automaticActivityDetection": {"silenceDurationMs": 800}},
                "outputAudioTranscription": {},
                "sessionResumption": {"handle": resume} if resume else {},
                "contextWindowCompression": {"slidingWindow": {}},
            }
        }

    async def _dial(self) -> None:
        resume = self._handle
        resumed = bool(resume)
        try:
            await self._open(resume)
        except LiveError as exc:
            if not resume or not exc.refused:
                raise
            # Gemini refused the handle itself: start fresh once, same model.
            self._set_handle(None)
            await self._open(None)
            resumed = False
            self._emit("on_context_lost")
        self._state.opened_at = self._clock()
        self._state.going_away = False
        await self._flush_pending(resumed)
        self._state.reader = asyncio.ensure_future(self._read())

    async def _open(self, resume: str | None) -> None:
        try:
            socket = await self._connect(WS_URL, {"x-goog-api-key": self.config.api_key})
        except Exception as exc:  # network or handshake, any type
            code, reason = _close_frame(exc)
            if code in REFUSAL_CLOSE_CODES:
                raise LiveError(f"Gemini a refusé la session : {reason or code}", refused=True) from None
            raise LiveError(f"Connexion à Gemini impossible ({type(exc).__name__}).") from None
        try:
            await socket.send(json.dumps(self.setup_message(resume=resume)))
            await asyncio.wait_for(self._await_setup(socket), timeout=SETUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            await _quiet_close(socket)
            raise LiveError("Gemini n'a pas confirmé la session à temps.") from None
        except LiveError:
            await _quiet_close(socket)
            raise
        except Exception as exc:
            await _quiet_close(socket)
            code, reason = _close_frame(exc)
            if code in REFUSAL_CLOSE_CODES:
                raise LiveError(f"Gemini a refusé la session : {reason or code}", refused=True) from None
            detail = f"{type(exc).__name__}{f', {reason}' if reason else ''}"
            raise LiveError(f"La session Gemini a échoué au démarrage ({detail}).") from None
        except BaseException:
            # close() cancelled us mid-setup: the socket must not leak.
            await _quiet_close(socket)
            raise
        self._state.socket = socket

    async def _await_setup(self, socket: Any) -> None:
        while True:
            message = _decode(await socket.recv())
            if message is None:
                continue
            self._apply_resumption(message)
            if "setupComplete" in message:
                return
            if "error" in message:
                raise LiveError(f"Gemini a refusé la session : {_error_text(message['error'])}", refused=True)

    async def _flush_pending(self, resumed: bool) -> None:
        pending, self._state.pending = self._state.pending, []
        if not pending:
            return
        if not resumed:
            # A fresh session never saw these calls: their ids mean nothing there.
            log.warning("talk: dropped %d delegated answer(s), the new session lost the context", len(pending))
            return
        for call_id, output in pending:
            if call_id not in self._state.cancelled:
                await self._send_response(call_id, output)

    # -- read ---------------------------------------------------------------

    async def _read(self) -> None:
        reason = "closed"
        fatal = False
        socket = self._state.socket
        try:
            while True:
                message = _decode(await socket.recv())
                if message is None:
                    log.warning("talk: dropped one malformed Gemini frame")
                    continue
                try:
                    stop = self._dispatch(message)
                except Exception as exc:  # a field of the wrong type: the frame goes, not the call
                    log.warning("talk: dropped one malformed Gemini frame (%s)", type(exc).__name__)
                    continue
                if stop:
                    reason, fatal = stop, True
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the socket died, any type
            code, close_reason = _close_frame(exc)
            if code in REFUSAL_CLOSE_CODES:
                reason, fatal = f"Gemini a coupé la session : {close_reason or code}", True
            elif self._state.going_away:
                reason = "goAway"
            else:
                reason = f"connexion perdue ({type(exc).__name__})"
        await self._drop_socket()
        await self._after_close(reason, fatal=fatal)

    def _dispatch(self, message: dict) -> str | None:
        """Apply one server message. A returned string ends the call for good."""

        self._apply_resumption(message)
        content = message.get("serverContent")
        if isinstance(content, dict):
            self._apply_content(content)
        tool_call = message.get("toolCall")
        if isinstance(tool_call, dict):
            self._apply_tool_call(tool_call)
        cancellation = message.get("toolCallCancellation")
        if isinstance(cancellation, dict):
            for call_id in cancellation.get("ids") or []:
                self._cancel_call(str(call_id))
        if "goAway" in message:
            # Keep reading: the current sentence finishes, the server closes
            # after timeLeft, and the redial picks up with the handle.
            self._state.going_away = True
        if "error" in message:
            return f"erreur Gemini : {_error_text(message['error'])}"
        return None

    def _apply_resumption(self, message: dict) -> None:
        update = message.get("sessionResumptionUpdate")
        if not isinstance(update, dict):
            return
        if update.get("resumable") is False:
            self._set_handle(None)
        elif update.get("resumable") and update.get("newHandle"):
            self._set_handle(str(update["newHandle"]))

    def _apply_content(self, content: dict) -> None:
        text = (content.get("inputTranscription") or {}).get("text")
        if text:
            self._emit("on_input_text", text)
        for part in (content.get("modelTurn") or {}).get("parts") or []:
            data = (part.get("inlineData") or {}).get("data") if isinstance(part, dict) else None
            if not data:
                continue
            try:
                chunk = base64.b64decode(data)
            except (ValueError, TypeError):
                log.warning("talk: dropped one undecodable audio chunk")
                continue
            self._emit("on_audio", chunk)
        text = (content.get("outputTranscription") or {}).get("text")
        if text:
            self._emit("on_output_text", text)
        if content.get("interrupted"):
            self._emit("on_interrupted")
        if content.get("turnComplete"):
            self._emit("on_turn_complete")

    def _apply_tool_call(self, tool_call: dict) -> None:
        for call in tool_call.get("functionCalls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if call.get("name") != persona.TALK_TOOL_NAME:
                self._spawn(call_id, self.send_tool_response(call_id, UNKNOWN_TOOL))
                continue
            raw_args = call.get("args")
            args = raw_args if isinstance(raw_args, dict) else {}
            request = str(args.get("request") or "")[:MAX_REQUEST_CHARS]
            self._spawn(call_id, self._delegate(call_id, request))

    async def _delegate(self, call_id: str, request: str) -> None:
        try:
            answer = self.handlers.on_tool_call(call_id, request)
            if inspect.isawaitable(answer):
                answer = await answer
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("talk: delegation %s failed", call_id)
            answer = DELEGATION_FAILED
        await self.send_tool_response(call_id, "" if answer is None else str(answer))

    def _cancel_call(self, call_id: str) -> None:
        self._state.cancelled.append(call_id)
        task = self._state.tool_tasks.pop(call_id, None)
        if task is not None:
            task.cancel()
        self._emit("on_tool_cancelled", call_id)

    # -- close and redial -----------------------------------------------------

    async def _after_close(self, reason: str, *, fatal: bool = False) -> None:
        if self._state.going_away or self._clock() - self._state.opened_at >= STABLE_AFTER_S:
            self._state.redials = 0
        while not fatal and not self._state.manual and self._handle and self._state.redials < MAX_REDIALS:
            self._state.redials += 1
            await self._sleep(REDIAL_DELAY_S)
            if self._state.manual:
                return
            try:
                await self._dial()
                return
            except LiveError as exc:
                reason = str(exc)
                fatal = exc.refused and not self._handle
        if self._state.manual:
            return
        self._state.pending.clear()
        await self._emit_async("on_closed", reason)

    async def _drop_socket(self) -> None:
        socket, self._state.socket = self._state.socket, None
        if socket is not None:
            await _quiet_close(socket)

    # -- helpers --------------------------------------------------------------

    def _emit(self, name: str, *args: Any) -> Any:
        try:
            return getattr(self.handlers, name)(*args)
        except Exception:
            log.exception("talk: handler %s failed", name)
            return None

    async def _emit_async(self, name: str, *args: Any) -> None:
        result = self._emit(name, *args)
        if inspect.isawaitable(result):
            try:
                await result
            except Exception:
                log.exception("talk: handler %s failed", name)

    def _set_handle(self, handle: str | None) -> None:
        self._handle = handle
        try:
            self._save_handle(handle)
        except Exception:
            log.warning("talk: could not store the resumption handle")

    def _spawn(self, call_id: str, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        if call_id:
            self._state.tool_tasks[call_id] = task

        def _done(done: asyncio.Task) -> None:
            if self._state.tool_tasks.get(call_id) is done:
                self._state.tool_tasks.pop(call_id, None)
            if not done.cancelled() and done.exception() is not None:
                log.error("talk: tool task %s failed: %r", call_id, done.exception())

        task.add_done_callback(_done)

    async def _send_response(self, call_id: str, output: str) -> None:
        await self._send(
            {"toolResponse": {"functionResponses": [{"id": call_id, "response": {"output": output}}]}}
        )

    async def _send(self, message: dict) -> None:
        socket = self._state.socket
        if socket is None:
            return
        await send_or_close(socket, json.dumps(message), "Gemini")


async def send_or_close(socket: Any, text: str, service: str, timeout: float | None = None) -> None:
    """Send one frame, or close a socket that does not take it in time (its
    reader then sees the close). Raises ConnectionError in that case, like a
    send on a dead socket raises."""

    timeout = SEND_TIMEOUT_S if timeout is None else timeout
    try:
        await asyncio.wait_for(socket.send(text), timeout)
    except asyncio.TimeoutError:
        log.warning("talk: %s took no frame for %.0f s, closing the socket", service, timeout)
        await _quiet_close(socket)
        raise ConnectionError(f"{service} ne prend plus les trames.") from None


async def _quiet_close(socket: Any) -> None:
    try:
        await socket.close()
    except Exception:
        pass
