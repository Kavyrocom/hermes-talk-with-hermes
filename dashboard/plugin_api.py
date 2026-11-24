"""Routes of the Desktop mini-bar, mounted under /api/plugins/talk-with-hermes/.

Thin on purpose: every rule lives in `talk_with_hermes.desktop_bridge`, which
is tested without FastAPI. The renderer reaches these routes through
`ctx.rest`, never Google.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import APIRouter, Depends, HTTPException, Request  # noqa: E402

from talk_with_hermes.desktop_bridge import DesktopCall  # noqa: E402
from talk_with_hermes.prefs import PrefsError, parse  # noqa: E402

router = APIRouter()
CALL = DesktopCall()

#: Largest body a POST route reads. The biggest real one is an agent answer,
#: cut to 20 000 characters once read; an audio chunk is under 90 kB.
MAX_BODY_BYTES = 2**20


async def json_body(request: Request) -> dict:
    """The JSON object of a POST, at most MAX_BODY_BYTES: a declared size over
    the cap is refused before reading, a stream is cut as soon as it passes."""

    declared = request.headers.get("content-length")
    if declared is not None and declared.strip().isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
    if not raw.strip():
        return {}
    try:
        body = json.loads(bytes(raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="A JSON object is expected")
    return body


def _owner(value) -> str:
    # Always a string here: an absent owner must never mean "any owner".
    return str(value or "")[:64]


@router.get("/status")
async def status() -> dict:
    return CALL.refresh_key()


@router.post("/start")
async def start(body: dict = Depends(json_body)) -> dict:
    return await CALL.start(resume=body.get("resume"), link=body.get("link"), lang=body.get("lang"))


@router.post("/audio")
async def audio(body: dict = Depends(json_body)) -> dict:
    return {"ok": await CALL.audio(body.get("data"), _owner(body.get("owner")))}


@router.get("/poll")
async def poll(after: int = 0, owner: str = "") -> dict:
    return CALL.poll(after, _owner(owner))


@router.post("/answer")
async def answer(body: dict = Depends(json_body)) -> dict:
    return {"result": CALL.answer(str(body.get("id") or "")[:200], body.get("answer"), body.get("error"),
                                  _owner(body.get("owner")))}


@router.post("/mute")
async def mute(body: dict = Depends(json_body)) -> dict:
    return await CALL.mute(body.get("muted"), _owner(body.get("owner")))


@router.post("/stop")
async def stop(body: dict = Depends(json_body)) -> dict:
    return await CALL.stop(_owner(body.get("owner")))


@router.get("/voices")
async def voices() -> dict:
    """The voices the current voice service offers, and the one in use."""

    from talk_with_hermes import services
    from talk_with_hermes.desktop_bridge import load_settings

    settings = load_settings()
    service = services.current(settings)
    current = service.voice(CALL.prefs.load().voice or str(settings.get("voice") or ""))
    return {"service": service.label, "voices": service.voice_list(), "current": current,
            "proven": service.proven}


@router.get("/prefs")
async def get_prefs() -> dict:
    return CALL.prefs.load().to_json()


@router.post("/prefs")
async def set_prefs(body: dict = Depends(json_body)) -> dict:
    """Saved now, used from the next call: the voice model reads its
    instructions when the call starts. A refusal comes back as `error`, in
    French, with a 200: the Desktop REST door does not carry a 4xx detail."""

    try:
        from talk_with_hermes import services
        from talk_with_hermes.desktop_bridge import load_settings

        service = services.current(load_settings())
        if isinstance(body, dict) and body.get("voice"):
            body = {**body, "voice": services.fit_voice(service, str(body["voice"]))}
        prefs = parse(body, voices=[vid for vid, _ in service.voices])
    except PrefsError as exc:
        return {"error": str(exc)}
    try:
        CALL.prefs.save(prefs)
    except PrefsError as exc:
        return {"error": str(exc)}
    return prefs.to_json()
