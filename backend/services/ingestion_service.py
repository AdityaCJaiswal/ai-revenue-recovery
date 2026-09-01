"""Ingestion orchestration.

Routes stay thin: they hand raw bytes here and get a result back. This is the
single place where "a failure arrived" turns into "a stored RecoveryEvent".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..adapters import razorpay
from ..core.config import get_settings
from ..core.logging import get_logger
from ..domain.events import RecoveryEvent
from ..repositories import event_repository, session

log = get_logger(__name__)


class IngestStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"        # event type outside our surfaces
    INVALID_SIGNATURE = "invalid_signature"
    MALFORMED = "malformed"


@dataclass(slots=True)
class IngestResult:
    status: IngestStatus
    event: RecoveryEvent | None = None
    detail: str | None = None

    @property
    def http_status(self) -> int:
        if self.status is IngestStatus.INVALID_SIGNATURE:
            return 401
        if self.status is IngestStatus.MALFORMED:
            return 400
        # Everything else is 2xx on purpose: a non-2xx makes Razorpay redeliver,
        # and redelivering an event we deliberately ignored achieves nothing.
        return 200


def ingest_razorpay_webhook(body: bytes, signature: str) -> IngestResult:
    settings = get_settings()
    secret = settings.razorpay_webhook_secret

    if secret:
        if not razorpay.verify_webhook_signature(body, signature, secret):
            log.warning("rejected webhook: signature mismatch")
            return IngestResult(IngestStatus.INVALID_SIGNATURE)
    else:
        log.warning("RAZORPAY_WEBHOOK_SECRET unset -- accepting unverified webhook")

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        return IngestResult(IngestStatus.MALFORMED, detail=str(exc))

    event = razorpay.normalize(payload)
    if event is None:
        return IngestResult(IngestStatus.IGNORED, detail=payload.get("event"))

    return _persist(event)


def ingest_events(events: list[RecoveryEvent]) -> int:
    """Bulk path for synthetic batches. Same storage as the webhook path."""
    with session() as conn:
        return event_repository.save_many(conn, events)


def _persist(event: RecoveryEvent) -> IngestResult:
    with session() as conn:
        fresh = event_repository.save(conn, event)
    if not fresh:
        return IngestResult(IngestStatus.DUPLICATE, event=event)
    log.info("ingested %s", event.describe())
    return IngestResult(IngestStatus.ACCEPTED, event=event)
