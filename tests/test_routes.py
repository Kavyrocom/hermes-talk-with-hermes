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


def test_every_post_route_refuses_an_oversized_body(api):
    """One cap for the six POST routes: a body over 1 MiB gets a 413, with or
    without a Content-Length. A normal body still goes through."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    module, _ = api
    app = FastAPI()
    app.include_router(module.router)
    client = TestClient(app)
    posts = sorted(r.path for r in module.router.routes if "POST" in r.methods)
    assert posts == ["/answer", "/audio", "/mute", "/prefs", "/start", "/stop"]
    big = b'{"data": "' + b"A" * 2**20 + b'"}'
    json_type = {"content-type": "application/json"}
    for path in posts:
        assert client.post(path, content=big, headers=json_type).status_code == 413, path
        chunked = client.post(path, content=iter([big[:1000], big[1000:]]), headers=json_type)
        assert chunked.status_code == 413, path
    assert client.post("/stop", json={"owner": "x"}).status_code == 200
    assert client.post("/stop").status_code == 200  # no body at all, as before


def test_a_declared_oversized_body_is_refused_unread(api):
    from fastapi import HTTPException
    from starlette.requests import Request

    module, _ = api
    reads = []

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/audio", "query_string": b"",
             "headers": [(b"content-length", str(2**20 + 1).encode())]}
    with pytest.raises(HTTPException) as refused:
        asyncio.run(module.json_body(Request(scope, receive)))
    assert refused.value.status_code == 413 and reads == []
