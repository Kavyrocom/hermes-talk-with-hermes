"""The /voices and /prefs routes, called as the page calls them, across a
change of voice service. Skipped where FastAPI is not installed."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from talk_with_hermes import desktop_bridge, services  # noqa: E402
from talk_with_hermes.prefs import PrefsStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("twh_routes", ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CALL.prefs = PrefsStore(tmp_path / "prefs.json")
    model = {"value": "gemini-3.8-live"}
    monkeypatch.setattr(desktop_bridge, "load_settings", lambda: {"model": model["value"]})
    return module, model


def test_a_voice_left_from_another_service_never_blocks_saving(api):
    module, model = api
    saved = asyncio.run(module.set_prefs({"address": "tu", "voice": "Fenrir", "words": []}))
    assert saved["voice"] == "Fenrir"
    for target, default in (("gpt-realtime-2.1", "marin"), ("grok-voice-latest", "eve"), ("gemini-3.8-live", "Puck")):
        model["value"] = target
        voices = asyncio.run(module.voices())
        page_sends = module.CALL.prefs.load().voice  # the page re-sends the saved voice first
        result = asyncio.run(module.set_prefs({"address": "vous", "voice": page_sends,
                                               "words": [{"word": "Kavyro", "heard": []}]}))
        assert "error" not in result, (target, result)
        assert result["address"] == "vous" and result["voice"] in {v["id"] for v in voices["voices"]}
        assert voices["current"] == result["voice"] == default, target  # the leftover became this default


def test_a_service_own_voice_is_kept_and_a_made_up_one_is_refused(api):
    module, model = api
    model["value"] = "grok-voice-latest"
    assert asyncio.run(module.set_prefs({"voice": "REX"}))["voice"] == "rex"  # xAI: any case
    model["value"] = "gpt-realtime-2.1"
    assert asyncio.run(module.set_prefs({"voice": "cedar"}))["voice"] == "cedar"
    assert asyncio.run(module.set_prefs({"voice": "Zzz"})) == {"error": "Voix inconnue pour ce service."}


def test_fit_voice_alone():
    gemini, openai = services.SERVICES[services.GEMINI], services.SERVICES[services.OPENAI]
    assert services.fit_voice(openai, "Puck") == "marin"
    assert services.fit_voice(gemini, "marin") == "Puck"
    assert services.fit_voice(gemini, "Kore") == "Kore"
    assert services.fit_voice(gemini, "nobody") == "nobody"
