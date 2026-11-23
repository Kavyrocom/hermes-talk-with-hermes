"""Discord helpers shared by the /talk command and the voice presence."""

from __future__ import annotations

NOT_IN_CHANNEL = (
    "Tu n'es pas dans un salon vocal. Entre dans un salon vocal de ce serveur, "
    "puis tape /talk dans un salon texte."
)


def session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return ""
    return str(get_session_env(name, "") or "").strip()


def gateway_runner():
    """The gateway this process runs, or None outside one. Hermes has no
    public handle on it yet: this is one of the two private seams the README
    declares."""

    try:
        from gateway.run import _gateway_runner_ref
    except Exception:
        return None
    return _gateway_runner_ref() if callable(_gateway_runner_ref) else None


def _host_discord_adapter():
    runner = gateway_runner()
    if runner is None:
        return None
    adapters = getattr(runner, "adapters", None) or {}
    for module_name in ("gateway.config", "models", "gateway.platform_registry"):
        try:
            module = __import__(module_name, fromlist=["Platform"])
            adapter = adapters.get(module.Platform.DISCORD)
        except Exception:
            continue
        if adapter is not None:
            return adapter
    for key, value in adapters.items():
        label = getattr(key, "value", None) or getattr(key, "name", None) or key
        if str(label).strip().lower() == "discord":
            return value
    return None
