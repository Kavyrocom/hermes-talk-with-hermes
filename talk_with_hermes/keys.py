"""The plugin's keys, in one place for every surface.

The voice keys (Gemini, OpenAI, xAI) and the voice settings live in the
profile where the plugin is installed: the one whose Desktop settings page
writes them. Desktop, a Discord voice channel of any agent, and any later
surface read them there. Nothing is copied into the other profiles.

The agent's local door (`API_SERVER_KEY`, 127.0.0.1 only) is not a setting:
the plugin creates it in the agent's profile the first time a voice call
needs that agent, and never shows it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

PLUGIN_ID = "talk-with-hermes"
DOOR_KEY = "API_SERVER_KEY"
DOOR_READY_SECONDS = 20.0


def owner_home(install: Path | None = None) -> Path:
    """`<home>/plugins/talk-with-hermes/...` -> `<home>`. A checkout outside a
    plugins folder (tests, dev) falls back to the active Hermes home."""

    root = (install or Path(__file__)).resolve()
    for parent in root.parents:
        if parent.parent.name == "plugins" and parent.name == PLUGIN_ID:
            return parent.parent.parent
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def profile_of(home: Path) -> str:
    return home.name if home.parent.name == "profiles" else "default"


def owner_profile() -> str:
    return profile_of(owner_home())


def env_value(name: str, *, strict: bool = False) -> str:
    """A secret as Hermes resolves it in the current profile scope (its .env,
    its secret sources). The raw environment only when Hermes is not
    importable, so a profile never borrows a key inherited from another.
    A read Hermes refuses reads as unset, unless `strict`: then it raises,
    for callers that would write a key they could not read. Never logged."""

    try:
        from hermes_cli.config import get_env_value
    except Exception:
        get_env_value = None
    if get_env_value is None:
        import os

        value = os.environ.get(name)
    else:
        try:
            value = get_env_value(name)
        except Exception:
            if strict:
                raise
            value = None
    return value.strip() if isinstance(value, str) else ""


def voice_key(key_env: tuple[str, ...]) -> str:
    """The first key found among `key_env`, in the current scope. Callers on
    another profile's turn enter the owner's scope first."""

    return next((v for v in (env_value(n) for n in key_env) if v), "")


def ensure_door_key(save: Callable[[str, str], None] | None = None) -> tuple[str, bool]:
    """The current profile's `API_SERVER_KEY`, created when missing.
    Returns (key, created). Must run inside that profile's scope. A key that
    cannot be read is never replaced: the read error goes to the caller."""

    key = env_value(DOOR_KEY, strict=True)
    if key:
        return key, False
    if save is None:
        from hermes_cli.config import require_env_writable, save_env_value as save

        require_env_writable(DOOR_KEY, "set")  # save_env_value refuses silently otherwise
    key = secrets.token_urlsafe(32)
    save(DOOR_KEY, key)
    log.info("talk-with-hermes: created the agent's local door key")
    return key, True


async def wait_door_open(probe: Callable[[], Awaitable[int]], *, seconds: float = DOOR_READY_SECONDS,
                         step: float = 1.0, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> bool:
    """A key just written is read by the gateway a few seconds later
    (it re-scans a profile when its .env changes). Wait for the door."""

    waited = 0.0
    while True:
        try:
            if await probe() != 401:
                return True
        except Exception:
            log.debug("talk-with-hermes: door probe failed", exc_info=True)
        if waited >= seconds:
            return False
        await sleep(step)
        waited += step
