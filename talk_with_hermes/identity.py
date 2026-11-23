"""Who the voice is: the agent of the profile the plugin is installed on.

Read from that profile's own files, never from the plugin: its SOUL.md title
and first paragraph, else the profile's display name, else the profile name,
and its `## Style` section (the voice must sound like the agent, not like a
generic assistant). Only those three bounded pieces leave
the machine (the voice model is a remote service): never the rest of SOUL.md,
memory or user notes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_NAME = "Hermes"
MAX_NAME_CHARS = 40
MAX_INTRO_CHARS = 400
MAX_STYLE_CHARS = 700
MAX_SOUL_BYTES = 64_000

_HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$")
#: The section that says how the agent speaks: `## Style`, or a heading such
#: as `## Voix de Camille`.
_STYLE_HEADING = re.compile(r"^##\s+(style|ton|tone|voix|voice)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Identity:
    name: str = DEFAULT_NAME
    intro: str = ""
    #: How the agent speaks, from its own SOUL.md: the voice copies it.
    style: str = ""
    #: The Hermes profile the plugin is installed on: delegations run there.
    profile: str = "default"

    def to_json(self) -> dict:
        return {"name": self.name, "profile": self.profile}


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", " ", str(text or ""))
    text = re.sub(r"[*_`]+", "", text)  # markdown emphasis, not words
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0]


def from_soul(text: str) -> tuple[str, str]:
    """(name, intro) from a SOUL.md: the first `# Title` and the first
    paragraph after it. Either may be empty."""

    name, intro, lines = "", "", (text or "").splitlines()
    for index, line in enumerate(lines):
        match = _HEADING.match(line.strip())
        if match:
            name = _clean(match.group(1), MAX_NAME_CHARS)
            paragraph: list[str] = []
            for rest in lines[index + 1:]:
                stripped = rest.strip()
                if stripped.startswith("#"):
                    break
                if not stripped:
                    if paragraph:
                        break
                    continue
                paragraph.append(stripped)
            intro = _clean(" ".join(paragraph), MAX_INTRO_CHARS)
            break
        if line.strip():
            break  # SOUL.md without a title first: no name from it
    return name, intro


def style_from_soul(text: str) -> str:
    """The first `## Style` (or Ton, Tone, Voix, Voice) section, whole, up to
    the next heading of the same or a higher level. Bullets become plain
    clauses. Empty when the agent has none."""

    body: list[str] = []
    inside = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if inside:
            if re.match(r"^#{1,2}\s", stripped):
                break
            if stripped.startswith("#"):
                continue  # a sub-heading is a label, not a rule
            body.append(re.sub(r"^[-*+]\s+|^\d+[.)]\s+", "", stripped))
        elif _STYLE_HEADING.match(stripped):
            inside = True
    return _clean(" ".join(part for part in body if part), MAX_STYLE_CHARS)


def load(home: Path | None = None) -> Identity:
    """Never raises: a missing or odd profile is still a working call."""

    try:
        if home is None:
            from hermes_constants import get_hermes_home

            home = Path(get_hermes_home())
        home = Path(home)
        profile = home.name if home.parent.name == "profiles" else "default"
        name = intro = style = ""
        soul = home / "SOUL.md"
        if soul.is_file():
            with soul.open("rb") as handle:
                text = handle.read(MAX_SOUL_BYTES).decode("utf-8", "replace")
            name, intro = from_soul(text)
            style = style_from_soul(text)
        if not name:
            try:  # the name `hermes profile` shows, kept in the profile's profile.yaml
                from hermes_yaml import safe_load

                meta = safe_load((home / "profile.yaml").read_text(encoding="utf-8")) or {}
                name = _clean(str(meta.get("display_name") or ""), MAX_NAME_CHARS) if isinstance(meta, dict) else ""
            except Exception:
                name = ""
        if not name and home.parent.name == "profiles":
            name = _clean(home.name.replace("-", " ").replace("_", " ").title(), MAX_NAME_CHARS)
        return Identity(name or DEFAULT_NAME, intro, style, profile)
    except Exception:
        log.warning("talk-with-hermes: identity unreadable, speaking as Hermes", exc_info=True)
        return Identity()
