"""Call preferences the operator edits on the Parler page: tu or vous, and a
dictionary of words the ear gets wrong.

Gemini Live has no vocabulary bias on its speech recognition, so the
dictionary works in two places we control: the voice model is told the right
spelling (system instruction), and every request handed to the agent is
corrected before it leaves. The renderer applies the same corrections to the
transcript it shows.

Stored in the profile's plugin state, never in the install folder, so a
reinstall keeps it and Discord can read the same file later.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ADDRESS_FORMS = ("tu", "vous")
MAX_WORDS = 200
MAX_WORD_CHARS = 60
MAX_HEARD = 12


@dataclass(frozen=True)
class Word:
    word: str
    heard: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prefs:
    address: str = "tu"
    words: tuple[Word, ...] = field(default_factory=tuple)
    #: Empty: the plugin setting (or the adapter default) decides.
    voice: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"address": self.address, "words": [{"word": w.word, "heard": list(w.heard)} for w in self.words],
                "voice": self.voice}


class PrefsError(ValueError):
    """A refused edit, worded for the operator."""


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def parse(raw: Any, voices: Any = None) -> Prefs:
    """Validate an edit from the page. Raises PrefsError with a French reason.
    `voices`: the ids the voice service accepts; None skips that check (a
    stored file is re-read before the service is known)."""

    if not isinstance(raw, dict):
        raise PrefsError("Réglages illisibles.")
    address = str(raw.get("address") or "tu")
    if address not in ADDRESS_FORMS:
        raise PrefsError("Adresse inconnue : choisis tu ou vous.")
    items = raw.get("words") or []
    if not isinstance(items, list):
        raise PrefsError("Le dictionnaire doit être une liste.")
    if len(items) > MAX_WORDS:
        raise PrefsError(f"{MAX_WORDS} mots au plus dans le dictionnaire.")
    words: list[Word] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise PrefsError("Entrée de dictionnaire illisible.")
        word = _clean(item.get("word"))
        if not word:
            continue  # an empty row on the page, not an error
        if len(word) > MAX_WORD_CHARS:
            raise PrefsError(f"« {word[:20]}… » dépasse {MAX_WORD_CHARS} caractères.")
        if word.casefold() in seen:
            raise PrefsError(f"« {word} » est déjà dans le dictionnaire.")
        seen.add(word.casefold())
        heard_raw = item.get("heard") or []
        if isinstance(heard_raw, str):
            if len(heard_raw) > MAX_HEARD * (MAX_WORD_CHARS + 2):
                raise PrefsError(f"{MAX_HEARD} variantes au plus pour « {word} ».")
            heard_raw = heard_raw.split(",")
        if not isinstance(heard_raw, list):
            raise PrefsError(f"Variantes de « {word} » illisibles.")
        if len(heard_raw) > MAX_HEARD * 2:  # refused before any work, a body can be huge
            raise PrefsError(f"{MAX_HEARD} variantes au plus pour « {word} ».")
        heard: list[str] = []
        known = {word.casefold()}
        for variant in heard_raw:
            variant = _clean(variant)
            if not variant or variant.casefold() in known:
                continue
            if len(variant) > MAX_WORD_CHARS:
                raise PrefsError(f"Une variante de « {word} » dépasse {MAX_WORD_CHARS} caractères.")
            known.add(variant.casefold())
            heard.append(variant)
        if len(heard) > MAX_HEARD:
            raise PrefsError(f"{MAX_HEARD} variantes au plus pour « {word} ».")
        words.append(Word(word, tuple(heard)))
    voice = str(raw.get("voice") or "").strip()
    if voice and (len(voice) > MAX_WORD_CHARS or (voices is not None and voice not in set(voices))):
        raise PrefsError("Voix inconnue pour ce service.")
    return Prefs(address, tuple(words), voice)


def apply_words(text: str, words: tuple[Word, ...]) -> str:
    """Replace each heard variant by the right word, whole words only,
    case-insensitive. Longest variants first, so "fluent cart" wins over "cart"."""

    if not text or not words:
        return text
    pairs = sorted(((h, w.word) for w in words for h in w.heard), key=lambda p: -len(p[0]))
    for heard, right in pairs:
        # A function, not a string: a word like "C:\\x" or "a\\1" is literal text, never an escape.
        text = re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", lambda _m, r=right: r, text, flags=re.IGNORECASE)
    return text


class PrefsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Prefs:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Prefs()
        except (OSError, ValueError):
            log.warning("talk-with-hermes: prefs unreadable, using defaults", exc_info=True)
            return Prefs()
        try:
            return parse(raw)
        except PrefsError:
            log.warning("talk-with-hermes: prefs invalid, using defaults")
            return Prefs()

    def save(self, prefs: Prefs) -> None:
        """Raises PrefsError, worded for the operator, if the disk refuses."""

        try:
            write_private(self.path, json.dumps(prefs.to_json(), ensure_ascii=False, indent=2))
        except OSError:
            log.warning("talk-with-hermes: prefs not saved", exc_info=True)
            raise PrefsError("Enregistrement impossible sur le serveur.") from None


def write_private(path: Path, text: str) -> None:
    """Atomic write, 0600 from the first byte, in a 0700 folder. A unique temp
    name, so two saves never write the same file."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)  # mkdir leaves an existing folder as it was
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # mkstemp is 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
