"""The voice services the plugin can talk through, and the one in use.

One setting picks the service: the model. `gemini-*` is Gemini Live, `gpt-*`
is OpenAI Realtime, `grok-*` is Grok Voice. Each service brings its own voices
(the page lists them, the saved choice is checked against them), its own key
and its own session class behind the same duplex surface.

Proof status (keep it honest: no model promise without a live receipt, the
README says the same):
- Gemini Live: live-proven, every day since 20/11/2026.
- OpenAI Realtime, Grok Voice: wire written from the docs, unit-tested, never
  run live. A live probe reached xAI and came back 403 (no credit), shown as
  is: that proves the URL, the auth header and the error path, not the call.

Ollama is not here on purpose: since v0.40.9 it synthesises speech
(`/v1/audio/speech`, model `phonon`, English, Apple Silicon) but neither hears
nor converses, so it cannot hold a duplex call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from talk_with_hermes import gemini_live, realtime

GEMINI, OPENAI, GROK = "gemini", "openai", "grok"


@dataclass(frozen=True)
class Service:
    id: str
    label: str
    models: tuple[str, ...]
    #: (voice id, trait key the page translates; "" = no description known).
    voices: tuple[tuple[str, str], ...]
    default_voice: str
    #: Environment names of the key, first found wins. Never logged.
    key_env: tuple[str, ...]
    #: Built like GeminiLiveSession: (config, handlers, *, handle, save_handle).
    factory: Callable[..., Any]
    proven: bool
    #: Voice ids match whatever the case (xAI documents them case-insensitive).
    any_case: bool = False

    @property
    def default_model(self) -> str:
        return self.models[0]

    def voice_list(self) -> list[dict[str, str]]:
        return [{"id": vid, "label": vid if not self.any_case else vid.capitalize(), "trait": trait}
                for vid, trait in self.voices]

    def voice(self, wanted: str | None) -> str:
        """The wanted voice if this service has it, else the service default:
        a voice saved under another service never breaks the call."""

        wanted = (wanted or "").strip()
        for vid, _ in self.voices:
            if vid == wanted or (self.any_case and vid.lower() == wanted.lower()):
                return vid
        return self.default_voice


#: OpenAI documents the ten names, no character for each (Realtime
#: conversations guide, read 21/11/2026). It recommends marin and cedar.
OPENAI_VOICES = tuple((name, "") for name in (
    "marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"))

#: xAI voice table (text-to-speech#voices, read 22/11/2026), one trait each
#: from xAI's own first words. Same roster for Speech to Speech.
GROK_VOICES: tuple[tuple[str, str], ...] = (
    ("eve", "upbeat"), ("ara", "warm"), ("rex", "confident"), ("sal", "smooth"), ("leo", "firm"),
    ("carina", "soft"), ("zagan", "dramatic"), ("helix", "lively"), ("orion", "mature"),
    ("luna", "gentle"), ("iris", "friendly"), ("altair", "elegant"), ("zenith", "forward"),
    ("perseus", "confident"), ("helios", "upbeat"), ("lux", "calm"), ("kepler", "bright"),
    ("rigel", "precise"), ("cosmo", "bright"), ("celeste", "confident"), ("ursa", "warm"),
    ("sirius", "playful"), ("lumen", "clear"), ("castor", "easygoing"), ("naksh", "knowledgeable"),
    ("atlas", "firm"), ("aurora", "even"), ("liora", "calm"),
)

SERVICES: dict[str, Service] = {
    GEMINI: Service(GEMINI, "Gemini Live", ("gemini-3.8-live", "gemini-3.1-flash-live-preview"),
                    gemini_live.VOICES, gemini_live.DEFAULT_VOICE, ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                    gemini_live.GeminiLiveSession, proven=True),
    OPENAI: Service(OPENAI, "OpenAI Realtime", ("gpt-realtime-2.1", "gpt-realtime-2"),
                    OPENAI_VOICES, "marin", ("OPENAI_API_KEY",), realtime.openai_session, proven=False),
    GROK: Service(GROK, "Grok Voice", ("grok-voice-latest", "grok-voice-3.0"),
                  GROK_VOICES, "eve", ("XAI_API_KEY",), realtime.grok_session, proven=False, any_case=True),
}

PREFIXES = (("gemini-", GEMINI), ("gpt-", OPENAI), ("grok-", GROK))


def for_model(model: str | None) -> Service:
    """The service a model name belongs to. Unknown or empty: Gemini, the
    proven one, with its own default model (see `model_of`)."""

    name = (model or "").strip().lower()
    for prefix, sid in PREFIXES:
        if name.startswith(prefix):
            return SERVICES[sid]
    return SERVICES[GEMINI]


def model_of(model: str | None) -> str:
    """The model to dial: the configured one when its service knows it by
    prefix, else that service's default."""

    name = (model or "").strip()
    service = for_model(name)
    return name if name and name.lower().startswith(tuple(p for p, _ in PREFIXES)) else service.default_model


def current(settings: dict[str, Any] | None) -> Service:
    return for_model(str((settings or {}).get("model") or ""))


def fit_voice(service: Service, wanted: str) -> str:
    """The voice to save under `service`. Its own voice (any case for xAI)
    is kept; a voice of another service, left over from before a model
    change, becomes this service's default, so the other settings still save.
    Any other name is returned as is, for prefs.parse to refuse."""

    wanted = (wanted or "").strip()
    own = service.voice(wanted)
    if own.lower() == wanted.lower():
        return own
    elsewhere = any(wanted.lower() == vid.lower()
                    for other in SERVICES.values() if other is not service for vid, _ in other.voices)
    return service.default_voice if elsewhere else wanted


def open_session(config: Any, handlers: Any = None, **kwargs: Any) -> Any:
    """The session for `config.model`, whatever the service."""

    return for_model(config.model).factory(config, handlers, **kwargs)


def all_models() -> list[str]:
    return [model for service in SERVICES.values() for model in service.models]
