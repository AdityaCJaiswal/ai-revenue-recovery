"""Inbound PSP webhooks. Thin: verify, hand to the service, ack fast."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse

from ...core.logging import get_logger
from ...services.ingestion_service import IngestStatus, ingest_razorpay_webhook

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _process(event_ids: list[str]) -> None:
    """Diagnose -> gate -> score, off the request path: Razorpay redelivers on
    any non-2xx, so the handler acks in milliseconds and the pipeline runs here."""
    from ...repositories import decision_repository, session
    from ...services.decision_service import process_event

    with session() as conn:
        for event_id in event_ids:
            row = decision_repository.get_event(conn, event_id)
            if row is not None:
                decision_id = process_event(conn, row)
                log.info("event %s -> decision %s", event_id, decision_id)


@router.post("/razorpay")
async def razorpay(
    request: Request,
    background: BackgroundTasks,
    x_razorpay_signature: str = Header(default=""),
):
    body = await request.body()  # RAW bytes -- the signature covers these exactly
    result = ingest_razorpay_webhook(body, x_razorpay_signature)

    if result.status is IngestStatus.ACCEPTED and result.event:
        background.add_task(_process, [result.event.event_id])

    payload: dict[str, object] = {"status": result.status.value}
    if result.event:
        payload["event_id"] = result.event.event_id
    if result.detail:
        payload["detail"] = result.detail

    return JSONResponse(payload, status_code=result.http_status)
