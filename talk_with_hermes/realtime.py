"""Realtime duplex session: OpenAI Realtime and Grok Voice, one protocol.

Same public surface as `GeminiLiveSession` (start, send_audio, end_audio,
send_tool_response, close, live, handle) and the same `LiveHandlers`, so the
Desktop call and the Discord salon drive either service without knowing it.

Wire facts this module is written against (docs read 21/11/2026):
- OpenAI Realtime: `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`,
  `Authorization: Bearer`. `session.update` with `type: "realtime"`,
  `output_modalities: ["audio"]`, `audio.input.format` PCM 24 kHz only,
  `audio.output.voice`. The newer "GPT-Live" API (`/v1/live/sessions`,
  `session.start`) is a different protocol and is not what this speaks.
- Grok Voice: `wss://api.x.ai/v1/realtime?model=grok-voice-latest`, same
  Bearer header, OpenAI-compatible events. Takes PCM 16 kHz in natively,
  `audio.input.transcription.language_hint` and `.keyterms` (vocabulary).
  Output audio may arrive as `response.output_audio.delta` or
  `response.audio.delta`: both are read.
- Function calls arrive as `response.function_call_arguments.done`
  (`call_id`, `name`, JSON `arguments`). The result goes back as a
  `conversation.item.create` of type `function_call_output`, then
  `response.create`. A `response.create` while a response is still running
  is refused by the server, so it waits for `response.done`.
- Barge-in: with server VAD the server stops its own answer when the user
  speaks; `input_audio_buffer.speech_started` tells the page to flush.

Not live-proven: every frame above. Neither service had a usable key on
21/11/2026 (no OpenAI API key; the xAI team has no credit left). The refusal
path is the only part that can be exercised for free.

No resumption: a dropped socket ends the call with its reason. Grok has an
opt-in `conversation_id` replay; it is left for when a receipt exists.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from talk_with_hermes import persona
from talk_with_hermes.audio import Upsample16To24
from talk_with_hermes.gemini_live import (
    CANCELLED,
    DELEGATION_FAILED,
    DROPPED,
    EMPTY_ANSWER,
    MAX_ANSWER_CHARS,
    MAX_REQUEST_CHARS,
    QUEUED,
    SENT,
    UNKNOWN_TOOL,
    LiveConfig,
    LiveError,
    LiveHandlers,
    _close_frame,
    _decode,
    _error_text,
    _noop,
    _quiet_close,
    send_or_close,
    websocket_connect,
)

log = logging.getLogger(__name__)

SETUP_TIMEOUT_S = 10.0
#: See gemini_live.SEND_TIMEOUT_S: a socket that takes no frame is closed.
SEND_TIMEOUT_S = 10.0
#: End of turn after this much silence, the same budget as Gemini's.
SILENCE_MS = 800
#: HTTP statuses at the handshake that mean "refused": retrying changes nothing.
REFUSAL_STATUSES = frozenset({400, 401, 402, 403, 404})

Connector = Callable[[str, dict[str, str]], Awaitable[Any]]


def _tool() -> dict:
    """The talk tool in Realtime's function shape (lowercase JSON schema)."""

    def lower(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: (v.lower() if k == "type" and isinstance(v, str) else lower(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [lower(v) for v in node]
        return node

    spec = persona.TALK_TOOL
    return {"type": "function", "name": spec["name"], "description": spec.get("description", ""),
            "parameters": lower(spec.get("parameters") or {"type": "object", "properties": {}})}


def _language(cfg: LiveConfig) -> str:
    """`fr-FR` -> `fr`: the first configured language, as a bare code (OpenAI)."""

    first = next((code for code in cfg.languages if code), "")
    return first.split("-")[0].lower()


#: xAI wants the region for these, and silently ignores a bare `es` or `pt`.
_XAI_REGIONAL = {"es": "es-ES", "pt": "pt-BR", "ar": "ar-AE"}


def _xai_language(cfg: LiveConfig) -> str:
    """`fr-FR` -> `fr`, `es-MX` -> `es-MX`, `es` -> `es-ES` (xAI language table)."""

    first = next((code for code in cfg.languages if code), "").strip()
    base, _, region = first.partition("-")
    base = base.lower()
    if base in _XAI_REGIONAL:
        return f"{base}-{region.upper()}" if region else _XAI_REGIONAL[base]
    return base


@dataclass(frozen=True)
class Dialect:
    """What differs between two Realtime services. Everything else is shared."""

    service: str
    url: str
    input_rate: int
    #: Builds the `session` object of the first `session.update`.
    session: Callable[[LiveConfig, str], dict]
    #: Transcription settings, sent in their own update so a rejected field
    #: never costs the instructions or the tool.
    transcription: Callable[[LiveConfig], dict | None] = lambda cfg: None


def _openai_session(cfg: LiveConfig, instructions: str) -> dict:
    return {
        "type": "realtime",
        "model": cfg.model,
        "output_modalities": ["audio"],
        "instructions": instructions,
        "tools": [_tool()],
        "tool_choice": "auto",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "turn_detection": {"type": "server_vad", "silence_duration_ms": SILENCE_MS},
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": cfg.voice},
        },
    }


def _openai_transcription(cfg: LiveConfig) -> dict | None:
    spec: dict[str, Any] = {"model": "gpt-4o-mini-transcribe"}
    if _language(cfg):
        spec["language"] = _language(cfg)
    words = [w.word for w in cfg.words or ()][:50]
    if words:
        spec["prompt"] = "Vocabulaire : " + ", ".join(words)
    return {"type": "realtime", "audio": {"input": {"transcription": spec}}}


def _xai_session(cfg: LiveConfig, instructions: str) -> dict:
    transcription: dict[str, Any] = {}
    if _xai_language(cfg):
        transcription["language_hint"] = _xai_language(cfg)
    keyterms = [w.word[:50] for w in cfg.words or () if w.word][:100]
    if keyterms:
        transcription["keyterms"] = keyterms
    audio_input: dict[str, Any] = {"format": {"type": "audio/pcm", "rate": 16000}}
    if transcription:
        audio_input["transcription"] = transcription
    return {
        "voice": cfg.voice,
        "instructions": instructions,
        "tools": [_tool()],
        "turn_detection": {"type": "server_vad", "silence_duration_ms": SILENCE_MS},
        "audio": {"input": audio_input, "output": {"format": {"type": "audio/pcm", "rate": 24000}}},
    }


OPENAI = Dialect("OpenAI Realtime", "wss://api.openai.com/v1/realtime?model={model}", 24000,
                 _openai_session, _openai_transcription)
XAI = Dialect("Grok Voice", "wss://api.x.ai/v1/realtime?model={model}", 16000, _xai_session)


@dataclass
class _State:
    socket: Any = None
    reader: asyncio.Task | None = None
    manual: bool = False
    #: A response is being generated: `response.create` must wait for its end.
    responding: bool = False
    #: Tool results sent while a response was running: one `response.create` owed.
    owe_response: bool = False
    #: Function calls received and not yet answered: speech waits for all of
    #: them, then one `response.create` (xAI: "after all results").
    open_calls: set = field(default_factory=set)
    #: Microphone audio appended since the last commit (ours or the server's).
    unheard: bool = False
    seen_calls: set = field(default_factory=set)
    tool_tasks: dict = field(default_factory=dict)


class RealtimeSession:
    """One socket, one conversation, one tool, for a Realtime `Dialect`."""

    def __init__(
        self,
        config: LiveConfig,
        handlers: LiveHandlers | None = None,
        *,
        dialect: Dialect = OPENAI,
        connect: Connector = websocket_connect,
        handle: str | None = None,
        save_handle: Callable[[str | None], Any] = _noop,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if not (config.api_key or "").strip():
            raise LiveError(f"Il manque la clé {dialect.service}.")
        self.config = config
        self.dialect = dialect
        self.handlers = handlers or LiveHandlers()
        self._connect = connect
        self._state = _State()
        self._upsample = Upsample16To24() if dialect.input_rate == 24000 else None
        del handle, save_handle, sleep  # no resumption on this protocol (see module doc)

    # -- public -----------------------------------------------------------

    @property
    def live(self) -> bool:
        return self._state.socket is not None

    @property
    def handle(self) -> str | None:
        return None

    def session_updates(self) -> list[dict]:
        """The `session.update` messages sent right after connecting."""

        cfg = self.config
        instructions = persona.system_instruction(cfg.spoken_answers, cfg.address, cfg.words, cfg.name,
                                                  cfg.intro, cfg.context, cfg.style)
        updates = [{"type": "session.update", "session": self.dialect.session(cfg, instructions)}]
        extra = self.dialect.transcription(cfg)
        if extra:
            updates.append({"type": "session.update", "session": extra})
        return updates

    async def start(self) -> None:
        self._state.manual = False
        url = self.dialect.url.format(model=quote(self.config.model, safe=""))
        try:
            socket = await self._connect(url, {"Authorization": f"Bearer {self.config.api_key}"})
        except Exception as exc:  # handshake refused, DNS, TLS: any type
            raise self._refusal(exc) from None
        try:
            updates = self.session_updates()
            await socket.send(json.dumps(updates[0]))
            await asyncio.wait_for(self._await_ready(socket), timeout=SETUP_TIMEOUT_S)
            for update in updates[1:]:
                await socket.send(json.dumps(update))
        except asyncio.TimeoutError:
            await _quiet_close(socket)
            raise LiveError(f"{self.dialect.service} n'a pas confirmé la session à temps.") from None
        except LiveError:
            await _quiet_close(socket)
            raise
        except BaseException as exc:
            await _quiet_close(socket)
            if isinstance(exc, Exception):
                raise self._refusal(exc) from None
            raise  # close() cancelled us mid-setup: the socket is already shut
        self._state.socket = socket
        self._state.reader = asyncio.ensure_future(self._read())

    async def send_audio(self, pcm16k_mono: bytes) -> None:
        if not pcm16k_mono or self._state.socket is None:
            return
        pcm = self._upsample.feed(pcm16k_mono) if self._upsample else pcm16k_mono
        if pcm:
            self._state.unheard = True
            await self._send({"type": "input_audio_buffer.append",
                              "audio": base64.b64encode(pcm).decode("ascii")})

    async def end_audio(self) -> None:
        """Microphone off mid-sentence: close the turn and ask for the answer,
        as Gemini's `audioStreamEnd` does. A commit alone creates no response
        (OpenAI SDK). Nothing is sent when the server VAD already committed
        everything heard."""

        if self._state.socket is None:
            return
        if self._upsample:
            self._upsample.reset()
        if not self._state.unheard:
            return
        self._state.unheard = False
        await self._send({"type": "input_audio_buffer.commit"})
        await self._ask_response()

    async def send_tool_response(self, call_id: str, answer: str) -> str:
        if not call_id:
            return DROPPED
        if self._state.socket is None:
            return DROPPED  # no resumption: the conversation that asked is gone
        output = (answer or "").strip()[:MAX_ANSWER_CHARS] or EMPTY_ANSWER
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "function_call_output", "call_id": call_id, "output": output}})
        self._state.open_calls.discard(call_id)
        if not self._state.open_calls:  # parallel calls: speak once, after the last result
            await self._ask_response()
        return SENT

    async def close(self) -> None:
        self._state.manual = True
        reader = self._state.reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        for task in list(self._state.tool_tasks.values()):
            if task is not asyncio.current_task():
                task.cancel()
        socket, self._state.socket = self._state.socket, None
        if socket is not None:
            await _quiet_close(socket)

    # -- setup ----------------------------------------------------------------

    def _refusal(self, exc: BaseException) -> LiveError:
        service = self.dialect.service
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int) and status in REFUSAL_STATUSES:
            body = getattr(getattr(exc, "response", None), "body", b"") or b""
            parsed = _decode(body)
            detail = _error_text(parsed.get("error") if parsed else bytes(body).decode("utf-8", "replace"))
            return LiveError(f"{service} a refusé la session ({status}) : {detail or 'sans détail'}", refused=True)
        code, reason = _close_frame(exc)
        if code in (1007, 1008):
            return LiveError(f"{service} a refusé la session : {reason or code}", refused=True)
        return LiveError(f"Connexion à {service} impossible ({type(exc).__name__}).")

    async def _await_ready(self, socket: Any) -> None:
        """`session.updated` acks the update. An `error` before it is a refusal."""

        while True:
            message = _decode(await socket.recv())
            if message is None:
                continue
            kind = message.get("type")
            if kind == "session.updated":
                return
            if kind == "error":
                raise LiveError(f"{self.dialect.service} a refusé la session : "
                                f"{_error_text(message.get('error'))}", refused=True)

    # -- read -----------------------------------------------------------------

    async def _read(self) -> None:
        socket = self._state.socket
        reason = "closed"
        try:
            while True:
                message = _decode(await socket.recv())
                if message is None:
                    log.warning("talk: dropped one malformed %s frame", self.dialect.service)
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the socket died, any type
            code, close_reason = _close_frame(exc)
            if code is not None and code != 1000:
                reason = f"{self.dialect.service} a fermé la session : {close_reason or code}"
            elif code is None:
                reason = f"connexion perdue ({type(exc).__name__})"
        self._state.socket = None
        if self._state.manual:
            return
        await self._emit_async("on_closed", reason)

    def _dispatch(self, message: dict) -> None:
        kind = str(message.get("type") or "")
        if kind in ("response.output_audio.delta", "response.audio.delta"):
            data = message.get("delta")
            if data:
                try:
                    self._emit("on_audio", base64.b64decode(data))
                except (ValueError, TypeError):
                    log.warning("talk: dropped one undecodable audio chunk")
        elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            if message.get("delta"):
                self._emit("on_output_text", str(message["delta"]))
        elif kind == "conversation.item.input_audio_transcription.completed":
            if message.get("transcript"):
                self._emit("on_input_text", str(message["transcript"]))
        elif kind == "input_audio_buffer.committed":
            self._state.unheard = False  # the server VAD closed the turn itself
        elif kind == "input_audio_buffer.speech_started":
            self._emit("on_interrupted")
        elif kind == "response.created":
            self._state.responding = True
        elif kind == "response.done":
            self._state.responding = False
            self._emit("on_turn_complete")
            if self._state.owe_response:
                self._state.owe_response = False
                self._spawn("", self._send({"type": "response.create"}))
        elif kind == "response.function_call_arguments.done":
            self._apply_tool_call(message)
        elif kind == "error":
            # After setup an error refuses one event (an empty commit, a
            # response.create too early), not the call: say it in the log.
            log.warning("talk: %s error: %s", self.dialect.service, _error_text(message.get("error")))

    def _apply_tool_call(self, message: dict) -> None:
        call_id = str(message.get("call_id") or "")
        if not call_id or call_id in self._state.seen_calls:
            return  # the same call can be echoed by response.done: run it once
        self._state.seen_calls.add(call_id)
        self._state.open_calls.add(call_id)
        if message.get("name") != persona.TALK_TOOL_NAME:
            self._spawn(call_id, self.send_tool_response(call_id, UNKNOWN_TOOL))
            return
        try:
            args = json.loads(message.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        request = str((args if isinstance(args, dict) else {}).get("request") or "")[:MAX_REQUEST_CHARS]
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

    async def _ask_response(self) -> None:
        if self._state.responding:
            self._state.owe_response = True  # sent at response.done
            return
        await self._send({"type": "response.create"})

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

    def _spawn(self, call_id: str, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        if call_id:
            self._state.tool_tasks[call_id] = task

        def _done(done: asyncio.Task) -> None:
            if call_id and self._state.tool_tasks.get(call_id) is done:
                self._state.tool_tasks.pop(call_id, None)
            if not done.cancelled() and done.exception() is not None:
                log.error("talk: task %s failed: %r", call_id or "-", done.exception())

        task.add_done_callback(_done)

    async def _send(self, message: dict) -> None:
        socket = self._state.socket
        if socket is None:
            return
        await send_or_close(socket, json.dumps(message), self.dialect.service, SEND_TIMEOUT_S)


def openai_session(config: LiveConfig, handlers: LiveHandlers | None = None, **kwargs: Any) -> RealtimeSession:
    return RealtimeSession(config, handlers, dialect=OPENAI, **kwargs)


def grok_session(config: LiveConfig, handlers: LiveHandlers | None = None, **kwargs: Any) -> RealtimeSession:
    return RealtimeSession(config, handlers, dialect=XAI, **kwargs)


__all__ = ["CANCELLED", "OPENAI", "QUEUED", "XAI", "Dialect", "RealtimeSession", "grok_session", "openai_session"]
