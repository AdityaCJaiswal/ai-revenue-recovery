"""Liveness and configuration visibility."""

from __future__ import annotations

from fastapi import APIRouter

from ...adapters.razorpay import unmapped_reasons
from ...core.config import get_settings
from ...domain.caps import is_unverified
from ...repositories import event_repository, session

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    settings = get_settings()
    with session() as conn:
        events = event_repository.count(conn)
    return {
        "ok": True,
        "version": settings.version,
        "events": events,
        "webhook_signature_enforced": bool(settings.razorpay_webhook_secret),
        "strategy": settings.default_strategy,
        # Surfaced deliberately: these caps are not primary-sourced, and the UI
        # should say so rather than presenting a guess as a rule.
        "unverified_retry_caps": [r for r in ("upi_autopay", "enach") if is_unverified(r)],
        # Populate the adapter's mapping table from these after a test-mode run.
        "unmapped_decline_reasons": sorted(unmapped_reasons),
    }
