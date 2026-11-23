"""Talk with Hermes. Our plugin, not a fork."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from talk_with_hermes.salon import NOT_IN_CHANNEL, session_value

NOT_ON_GATEWAY = "La voix Discord n'est pas active sur ce gateway."
NOT_ALLOWED = "Ton compte n'est pas autorisé en vocal."
HELP = ("Entre dans le salon vocal d'un agent : il arrive tout seul. "
        "Ou tape /talk ici en étant dans un salon vocal : l'agent de ce salon texte te rejoint. "
        "/talk leave pour raccrocher.")


def _platform() -> str:
    return session_value("HERMES_SESSION_PLATFORM").lower()


def _current_profile() -> str:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        return "default"
    return home.name if home.parent.name == "profiles" else "default"


def _may_use_voice(adapter: Any, user: str, guild: Any) -> bool:
    """The check that decides whose voice is heard. Hermes lets the command
    through on its text gate, which can be wider: DISCORD_ALLOWED_CHANNELS
    alone lets anyone type in the channel, never speak."""

    from talk_with_hermes import discord_host

    try:
        return discord_host.allowed(adapter, int(user), guild)
    except ValueError:
        return False


def talk_command(raw_args: str = ""):
    """Sync entry. On Discord, return a coroutine the gateway awaits on its loop."""

    if _platform() != "discord":
        return NOT_IN_CHANNEL
    return _talk_on_gateway_loop(raw_args, _current_profile())


async def _talk_on_gateway_loop(raw_args: str, profile: str) -> str:
    from talk_with_hermes import discord_host
    from talk_with_hermes.discord_presence import PRESENCE

    sub = (raw_args or "").strip().lower()
    if sub in {"help", "aide"}:
        return HELP
    presence = PRESENCE.get("presence")
    if presence is None:
        return NOT_ON_GATEWAY
    try:
        guild_id = int(session_value("HERMES_SESSION_SCOPE_ID"))
    except ValueError:
        return "Tape /talk dans un salon d'un serveur, pas en message privé."
    user = session_value("HERMES_SESSION_USER_ID")
    if sub in {"leave", "stop", "raccroche"}:
        entry = presence.calls.get(guild_id)
        if entry is None:
            return "Je ne suis pas en vocal sur ce serveur."
        if not _may_use_voice(presence.adapter, user, entry["channel"].guild):
            return NOT_ALLOWED
        await presence.end(guild_id, "/talk leave")
        return "Raccroché. Le salon est à toi."
    if sub not in {"", "join"}:
        return HELP
    channel = await presence.adapter.get_user_voice_channel(guild_id, user) if user else None
    if channel is None:
        return NOT_IN_CHANNEL
    if not _may_use_voice(presence.adapter, user, channel.guild):
        return NOT_ALLOWED
    return await presence.summon(channel, discord_host.route_of(channel) or profile)


def register(ctx) -> None:
    ctx.register_command(
        "talk",
        talk_command,
        description="L'agent te rejoint en vocal (ou entre dans son salon vocal).",
        args_hint="[leave|help]",
    )
    from talk_with_hermes.discord_presence import start_in_gateway

    start_in_gateway()
