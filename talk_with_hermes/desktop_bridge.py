"""Desktop call: the backend half of the composer mini-bar.

The renderer never talks to Google. It sends microphone PCM here, polls for
what to play and show, and runs each delegation on the user's own Hermes
session (so tools run under the app's approvals). This module owns the Gemini
socket and the key, and turns the session callbacks into an ordered event
feed the renderer drains.

One call per backend process. A second Start while a call is up returns the
call in progress.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from talk_with_hermes import persona
from talk_with_hermes import identity as identity_mod
from talk_with_hermes.prefs import Prefs, PrefsStore, apply_words, write_private
from talk_with_hermes import services
from talk_with_hermes.gemini_live import (
    LiveConfig,
    LiveError,
    LiveHandlers,
)

log = logging.getLogger(__name__)

PLUGIN_ID = "talk-with-hermes"
#: Gemini's key names, kept for callers that predate the other services.
KEY_ENV_NAMES = services.SERVICES[services.GEMINI].key_env
#: How long the voice waits for the agent before telling the user it failed.
#: Measured on the live wire: a tool call left unanswered for 240 s keeps the
#: turn open (no goAway, no close), and the late answer is still spoken. So the
#: ceiling is ours to set, and ten minutes covers real work without letting a
#: dead task pin the turn for an hour.
DELEGATION_TIMEOUT_S = 600.0
#: 16 kHz mono PCM16 is 32 000 bytes per second; a renderer block is ~0.25 s.
MAX_AUDIO_CHUNK_BYTES = 64_000
#: Events the renderer has not drained yet. Beyond this the oldest are dropped.
MAX_EVENTS = 4000
MAX_ERROR_TEXT = 500
#: base64 of MAX_AUDIO_CHUNK_BYTES, checked before decoding anything.
MAX_AUDIO_B64_CHARS = (MAX_AUDIO_CHUNK_BYTES + 2) // 3 * 4
#: What one answer may carry back to the voice model.
MAX_ANSWER_CHARS = 20_000
#: Delegations waiting at once. More is a loop, not a conversation.
MAX_WAITING = 3
#: A call nobody polls any more (window closed, renderer crashed) is hung up.
IDLE_HANGUP_S = 20.0

IDLE, CONNECTING, LIVE, ERROR = "idle", "connecting", "live", "error"


# -- settings and secrets -----------------------------------------------------


def load_settings() -> dict[str, Any]:
    """`plugins.entries.talk-with-hermes.settings` of the active profile."""

    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        log.debug("talk-with-hermes: settings unreadable", exc_info=True)
        return {}
    node: Any = config
    for key in ("plugins", "entries", PLUGIN_ID, "settings"):
        node = node.get(key) if isinstance(node, dict) else None
    return dict(node) if isinstance(node, dict) else {}


def load_api_key(service: "services.Service | None" = None) -> str:
    """The key of the voice service in use (the configured model's), as Hermes
    resolves it for this profile, the plugin's own (its `.env`, its secret
    scope): `keys.voice_key`, the same reader every surface uses. Never logged."""

    from talk_with_hermes import keys

    service = service or services.current(load_settings())
    return keys.voice_key(service.key_env)


def config_from(settings: dict[str, Any], api_key: str, prefs: Prefs | None = None,
                who: "identity_mod.Identity | None" = None, context: str = "") -> LiveConfig:
    prefs = prefs or Prefs()
    who = who or identity_mod.Identity()
    languages = settings.get("languages") or "fr-FR"
    if isinstance(languages, str):
        languages = [part.strip() for part in languages.split(",")]
    spoken = str(settings.get("spoken_answers") or "full")
    model = services.model_of(str(settings.get("model") or ""))
    service = services.for_model(model)
    return LiveConfig(
        api_key=api_key,
        model=model,
        voice=service.voice(prefs.voice or str(settings.get("voice") or "")),
        languages=tuple(code for code in languages if code),
        spoken_answers=spoken if spoken in persona.SPOKEN_ANSWER_MODES else "full",
        address=prefs.address,
        words=prefs.words,
        name=who.name,
        intro=who.intro,
        style=who.style,
        context=context,
    )


#: Desktop's language, told to the voice so the first words match the screen.
#: French is the persona's own language: nothing to add.
UI_LANGUAGES = {"en": "anglais", "es": "espagnol"}


def start_context(resume: Any = None, link: Any = None, lang: Any = None) -> str:
    """What the voice is reminded of at the start of a call, bounded."""

    parts = []
    spoken = UI_LANGUAGES.get(str(lang or "").split("-")[0].lower()) if isinstance(lang, str) else None
    if spoken:
        parts.append(f"Langue de l'interface de l'opérateur : {spoken}. Parle {spoken} dès ta première "
                     "phrase, et suis l'opérateur s'il change de langue.")
    if isinstance(link, dict):
        title = persona.clean_context([{"who": "user", "text": link.get("title") or ""}])
        title = title[0].split(" : ", 1)[1][:120] if title else "sans titre"
        parts.append(persona.context_block(
            f"Tu travailles sur la session « {title} » de l'opérateur. Fin de son contenu :",
            persona.clean_context(link.get("lines")),
            "Quand tu délègues, l'agent travaille dans cette session et en voit tout l'historique."))
    if resume:
        parts.append(persona.context_block(persona.RECAP_HEADING, persona.clean_context(resume), persona.RECAP_HOW))
    return "\n\n".join(p for p in parts if p)


def default_state_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-state" / PLUGIN_ID


class HandleStore:
    """The Gemini resumption handle, kept in the profile's plugin state, never
    in the install folder."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        try:
            handle = json.loads(self.path.read_text(encoding="utf-8")).get("handle")
        except (OSError, ValueError, AttributeError):
            return None
        return handle if isinstance(handle, str) and handle else None

    def save(self, handle: str | None) -> None:
        try:
            if handle is None:
                self.path.unlink(missing_ok=True)
                return
            write_private(self.path, json.dumps({"handle": handle}))
        except OSError:
            log.warning("talk-with-hermes: could not store the resumption handle", exc_info=True)


# -- the call -------------------------------------------------------------------


class DesktopCall:
    def __init__(
        self,
        *,
        settings: Callable[[], dict[str, Any]] = load_settings,
        api_key: Callable[[], str] = load_api_key,
        handles: HandleStore | None = None,
        prefs: PrefsStore | None = None,
        session_factory: Callable[..., Any] = services.open_session,
        delegation_timeout: float = DELEGATION_TIMEOUT_S,
        max_events: int = MAX_EVENTS,
        idle_hangup: float = IDLE_HANGUP_S,
        identity: Callable[[], "identity_mod.Identity"] = identity_mod.load,
    ) -> None:
        self._settings = settings
        self._identity = identity
        self._who = identity_mod.Identity()
        self._api_key = api_key
        self._handles = handles or HandleStore(default_state_dir() / "resume.json")
        self.prefs = prefs or PrefsStore(default_state_dir() / "prefs.json")
        self._call_prefs = Prefs()
        self._session_factory = session_factory
        self._delegation_timeout = delegation_timeout
        self._events: deque = deque()
        self._max_events = max_events
        self._idle_hangup = idle_hangup
        self._last_seen = 0.0
        self._watchdog: asyncio.Task | None = None
        self._has_key = False
        self.owner = ""
        self._seq = 0
        self._waiting: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.session: Any = None
        self.status = IDLE
        self.error = ""
        self.model = ""
        #: Muted: the backend drops every chunk, late uploads included.
        self.muted = False
        self.spoken_answers = "full"

    # -- renderer API ---------------------------------------------------------

    async def start(self, resume: Any = None, link: Any = None, lang: Any = None) -> dict[str, Any]:
        """Start a call, or take over the one running. The caller gets a fresh
        `owner`; the previous renderer (a reloaded or second window) loses the
        feed, so one voice request never runs twice.

        `resume`: the previous call's lines ({who, text}), recalled to the voice.
        `link`: {title, lines} of a chat session the voice works on."""

        async with self._lock:
            self._has_key = bool(self._api_key())
            self._last_seen = time.monotonic()
            if self.status in (CONNECTING, LIVE):
                self._fail_waiting()  # the old renderer was running them
                self.owner = secrets.token_urlsafe(16)
                return {**self.summary(), "owner": self.owner}
            self._fail_waiting()
            self._events.clear()  # nothing of the previous call; `_seq` stays monotonic
            self.owner = secrets.token_urlsafe(16)
            self.status, self.error, self.muted = CONNECTING, "", False
            try:
                self._call_prefs = self.prefs.load()
                self._who = self._identity()
                config = config_from(self._settings(), self._api_key(), self._call_prefs, self._who,
                                     context=start_context(resume, link, lang))
                session = self._session_factory(
                    config,
                    self._handlers(),
                    handle=self._handles.load(),
                    save_handle=self._handles.save,
                )
                self.session = session
                self.model, self.spoken_answers = config.model, config.spoken_answers
                await session.start()
            except LiveError as exc:
                self.session = None
                self._end(ERROR, str(exc))
            except Exception as exc:  # network, DNS, TLS: say it, do not crash the route
                log.warning("talk-with-hermes: start failed", exc_info=True)
                self.session = None
                label = services.current(self._settings()).label
                self._end(ERROR, f"Connexion à {label} impossible : {type(exc).__name__}.")
            else:
                if self.status == CONNECTING:
                    self.status = LIVE
                    self._arm_watchdog()
            return {**self.summary(), "owner": self.owner}

    def owns(self, owner: str | None) -> bool:
        """`None` is the in-process caller (tests); the routes always pass a string.
        Compared as bytes: `compare_digest` raises on non-ASCII text."""

        if owner is None:
            return True
        return bool(self.owner) and secrets.compare_digest(
            str(owner).encode("utf-8", "replace"), self.owner.encode("utf-8"))

    async def audio(self, data_b64: Any, owner: str | None = None) -> bool:
        if self.status != LIVE or self.session is None or not isinstance(data_b64, str) or not self.owns(owner):
            return False
        if self.muted:  # a chunk still in flight when the mic was cut never reaches Gemini
            return False
        if len(data_b64) > MAX_AUDIO_B64_CHARS:  # before decoding: a huge body costs nothing
            return False
        try:
            pcm = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            return False
        if not pcm or len(pcm) > MAX_AUDIO_CHUNK_BYTES or len(pcm) % 2:
            return False
        try:
            await self.session.send_audio(pcm)
        except Exception:  # socket closing under us: the poll reports the end
            log.debug("talk-with-hermes: audio not sent", exc_info=True)
            return False
        return True

    async def mute(self, muted: Any, owner: str | None = None) -> dict[str, Any]:
        """Cut or reopen the microphone. Cutting tells Gemini the stream ended."""

        if not self.owns(owner) or self.status != LIVE:
            return {**self.summary(), "ok": False}
        was, self.muted = self.muted, muted is True
        if self.muted and not was and self.session is not None:
            try:
                await self.session.end_audio()
            except Exception:  # socket closing: nothing is listening anyway
                log.debug("talk-with-hermes: audioStreamEnd not sent", exc_info=True)
        return {**self.summary(), "ok": True}

    def answer(self, call_id: str, answer: str | None = None, error: str | None = None,
               owner: str | None = None) -> str:
        """The renderer's result for one delegation: "accepted" or "unknown"
        (cancelled, timed out, never asked, or not this renderer's call)."""

        if not self.owns(owner):
            return "unknown"
        waiter = self._waiting.get(call_id or "")
        if waiter is None or waiter.done():
            return "unknown"
        if error:
            waiter.set_exception(RuntimeError(str(error)[:MAX_ERROR_TEXT]))
        else:
            waiter.set_result(str(answer or "")[:MAX_ANSWER_CHARS])
        return "accepted"

    async def stop(self, owner: str | None = None) -> dict[str, Any]:
        """A stale window cannot hang up the call another window took over."""

        async with self._lock:
            if not self.owns(owner):
                return self.summary()
            session, self.session = self.session, None
            self.owner = ""
            self._disarm_watchdog()
            self._fail_waiting()
            if session is not None:
                await session.close()
            self.status, self.error, self.muted = IDLE, "", False
            return self.summary()

    def poll(self, after: int = 0, owner: str | None = None) -> dict[str, Any]:
        """Events newer than `after`. Events up to `after` are acknowledged
        and released: only the owner drains this feed. Anyone else is told
        the call is no longer theirs and gets nothing."""

        if not self.owns(owner):
            return {**self.summary(), "owner_ok": False, "next": self._seq, "events": []}
        self._last_seen = time.monotonic()
        while self._events and self._events[0]["seq"] <= after:
            self._events.popleft()
        self._drop_settled_delegations()
        return {**self.summary(), "next": self._seq, "events": list(self._events)}

    def _awaited(self, call_id: Any) -> bool:
        waiter = self._waiting.get(call_id or "")
        return waiter is not None and not waiter.done()

    def _drop_settled_delegations(self) -> None:
        """A `delegate` is handed out only while its request is still awaited.
        Answered, timed out, cancelled or failed, it never runs (again) in any
        window: a window that takes over reads the feed from the start, and
        the one that ran it may have closed before acknowledging it."""

        if any(e["kind"] == "delegate" and not self._awaited(e.get("id")) for e in self._events):
            self._events = deque(e for e in self._events
                                 if not (e["kind"] == "delegate" and not self._awaited(e.get("id"))))

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "model": self.model,
            "service": services.for_model(self.model or self._settings().get("model")).label,
            "spoken_answers": self.spoken_answers,
            "has_key": self._has_key,
            "muted": self.muted,
            "agent": self._who.name,
            "profile": self._who.profile,
        }

    def refresh_key(self) -> dict[str, Any]:
        """/status: re-read the key (the operator may just have pasted it)."""

        self._has_key = bool(self._api_key())
        if self.status not in (CONNECTING, LIVE):
            self._who = self._identity()  # the page names the agent before the first call
        return self.summary()

    # -- session callbacks ----------------------------------------------------

    def _handlers(self) -> LiveHandlers:
        return LiveHandlers(
            on_audio=lambda pcm: self._emit("audio", data=base64.b64encode(pcm).decode("ascii")),
            on_interrupted=lambda: self._emit("interrupted"),
            on_input_text=lambda text: self._emit("in", text=text),
            on_output_text=lambda text: self._emit("out", text=text),
            on_turn_complete=lambda: self._emit("turn"),
            on_tool_call=self._delegate,
            on_tool_cancelled=lambda call_id: self._emit("cancel", id=call_id, why="gemini"),
            on_context_lost=lambda: self._emit("context_lost"),
            on_closed=self._closed,
        )

    async def _delegate(self, call_id: str, request: str) -> str:
        if not call_id or call_id in self._waiting:
            raise RuntimeError("identifiant de demande vide ou déjà utilisé")
        if len(self._waiting) >= MAX_WAITING:
            raise RuntimeError("trop de demandes en cours, attends la réponse de l'agent")
        try:
            request = apply_words(request, self._call_prefs.words)
        except Exception:  # a dictionary bug must never cost the request
            log.warning("talk-with-hermes: dictionary not applied", exc_info=True)
        waiter = asyncio.get_running_loop().create_future()
        self._waiting[call_id] = waiter
        self._emit("delegate", id=call_id, request=request,
                   prompt=persona.delegated_prompt(request, self._call_prefs.address))
        try:
            return await asyncio.wait_for(waiter, self._delegation_timeout)
        except asyncio.TimeoutError:
            self._emit("cancel", id=call_id, why="timeout")
            raise RuntimeError("delegation timed out") from None
        finally:
            self._waiting.pop(call_id, None)

    def _closed(self, reason: str) -> None:
        self.session = None
        self._disarm_watchdog()
        self._fail_waiting()
        self._end(ERROR, f"Appel terminé : {reason}" if reason else "Appel terminé.")
        self._emit("closed", reason=reason)

    # -- internals ------------------------------------------------------------

    def _emit(self, kind: str, **fields: Any) -> None:
        """Over budget, audio goes first, then anything but a `delegate` or a
        `cancel`: a lost chunk is a blip, a lost `delegate` or `cancel` is a
        request that never runs or never stops."""

        self._seq += 1
        if len(self._events) >= self._max_events:
            drop = next((i for i, e in enumerate(self._events) if e["kind"] == "audio"), None)
            if drop is None:
                drop = next((i for i, e in enumerate(self._events) if e["kind"] not in ("delegate", "cancel")), 0)
            del self._events[drop]
        self._events.append({"seq": self._seq, "kind": kind, **fields})

    def _arm_watchdog(self) -> None:
        self._disarm_watchdog()
        self._watchdog = asyncio.get_running_loop().create_task(self._watch())

    def _disarm_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _watch(self) -> None:
        step = max(0.05, min(2.0, self._idle_hangup / 4))
        while self.status == LIVE:
            await asyncio.sleep(step)
            if self.status == LIVE and time.monotonic() - self._last_seen > self._idle_hangup:
                log.info("talk-with-hermes: nobody polls the call any more, hanging up")
                self._watchdog = None
                await self.stop()
                self._emit("closed", reason="fenêtre fermée")
                return

    def _end(self, status: str, error: str) -> None:
        self.status, self.error = status, error[:MAX_ERROR_TEXT]

    def _fail_waiting(self, why: str = "l'appel a changé de fenêtre ou s'est arrêté") -> None:
        """Every delegation still waiting fails now, with a reason the voice
        speaks (the session sends DELEGATION_FAILED: the model never waits in
        the void). Its `delegate` event leaves the feed, so the next owner never
        runs the request a second time. A run the old window already started
        may still finish there; its answer is refused (one run, not two)."""

        for waiter in self._waiting.values():
            if not waiter.done():
                waiter.set_exception(RuntimeError(why))
        self._waiting.clear()
        self._drop_settled_delegations()
