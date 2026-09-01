"""Voice sessions: text-mode simulation now; LiveKit token endpoint when
credentials are configured."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...core.config import get_settings
from ...repositories import session as db_session
from ...repositories import voice_repository
from ...services.voice.call_service import customer_turn, start_session

router = APIRouter(prefix="/voice", tags=["voice"])


class TurnBody(BaseModel):
    text: str


@router.get("/config")
def config():
    s = get_settings()
    return {
        "sarvam": bool(s.sarvam_api_key),
        "livekit": bool(s.livekit_url and s.livekit_api_key and s.livekit_api_secret),
        "text_mode": True,
    }


@router.post("/livekit/token")
def livekit_token(decision_id: str):
    """Mint a browser join token for room call_<decision_id>. The voice worker
    (automatic dispatch) joins the same room and runs the screened pipeline."""
    s = get_settings()
    if not (s.livekit_url and s.livekit_api_key and s.livekit_api_secret):
        raise HTTPException(400, "LiveKit credentials not configured in .env")
    from livekit import api as lk_api  # lazy: optional dependency path

    room = f"call_{decision_id}"
    token = (
        lk_api.AccessToken(api_key=s.livekit_api_key, api_secret=s.livekit_api_secret)
        .with_identity("customer-demo")
        .with_name("Customer")
        .with_grants(lk_api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return {"url": s.livekit_url, "token": token, "room": room}


@router.post("/sessions")
def create(decision_id: str):
    out = start_session(decision_id, mode="text")
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@router.post("/sessions/{call_id}/turn")
def turn(call_id: str, body: TurnBody):
    out = customer_turn(call_id, body.text.strip())
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@router.get("/calls/{call_id}")
def call(call_id: str):
    with db_session() as conn:
        view = voice_repository.call_view(conn, call_id)
    if view is None:
        raise HTTPException(404, "no such call")
    return view
