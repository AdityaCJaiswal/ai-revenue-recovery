"""Outbound Razorpay: TEST-mode Payment Links.

When keys are configured, upi/whatsapp link actions create REAL test-mode
payment links -- a live short_url lands in the audit trail. Without keys the
sink simulates, marked as such in provider_ref.
"""

from __future__ import annotations

from ...core.config import get_settings
from ...core.logging import get_logger

log = get_logger(__name__)


def create_payment_link(amount_paise: int, description: str, reference_id: str) -> str | None:
    """Returns short_url or None (no keys / API error -> caller simulates)."""
    s = get_settings()
    if not (s.razorpay_key_id and s.razorpay_key_secret):
        return None
    try:
        import razorpay  # imported lazily; optional at runtime

        client = razorpay.Client(auth=(s.razorpay_key_id, s.razorpay_key_secret))
        link = client.payment_link.create({
            "amount": amount_paise,
            "currency": "INR",
            "description": description[:255],
            "reference_id": reference_id[:40],
            "notes": {"source": "ai-revenue-recovery", "cohort": "test-mode"},
        })
        return link.get("short_url")
    except Exception as exc:  # noqa: BLE001 -- sink must never kill the executor
        log.warning("payment link creation failed (%s) -- simulating", exc)
        return None
