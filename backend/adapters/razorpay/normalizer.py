"""Razorpay webhook payload -> RecoveryEvent.

Everything PSP-specific lives in this package. Supporting another gateway means
adding a sibling adapter; nothing downstream changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ...core.logging import get_logger
from ...domain.enums import DeclineFamily, EventType, Rail, Source
from ...domain.events import RecoveryEvent

log = get_logger(__name__)

# Razorpay error_reason -> our diagnosis axis.
#
# UNVERIFIED against live payloads: these strings are inferred, not confirmed
# from Razorpay docs. Anything unmapped falls through to UNKNOWN and is recorded
# rather than guessed at. Populate this from real test-mode webhooks before
# trusting it -- RESEARCH.md "Still open -- do not guess".
_REASON_TO_FAMILY: dict[str, DeclineFamily] = {
    "insufficient_funds": DeclineFamily.INSUFFICIENT_FUNDS,
    "payment_failed_due_to_insufficient_funds": DeclineFamily.INSUFFICIENT_FUNDS,
    "issuer_declined": DeclineFamily.ISSUER_DECLINE,
    "do_not_honor": DeclineFamily.ISSUER_DECLINE,
    "card_lost": DeclineFamily.HARD_DECLINE,
    "card_stolen": DeclineFamily.HARD_DECLINE,
    "invalid_card_number": DeclineFamily.HARD_DECLINE,
    "card_expired": DeclineFamily.MANDATE_EXPIRED,
    "authentication_failed": DeclineFamily.AUTHENTICATION_REQUIRED,
    "mandate_revoked": DeclineFamily.MANDATE_REVOKED,
    "mandate_cancelled": DeclineFamily.MANDATE_REVOKED,
    "mandate_paused": DeclineFamily.MANDATE_PAUSED,
    "amount_exceeds_mandate": DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP,
    "gateway_timeout": DeclineFamily.PSP_TIMEOUT,
    "payment_timeout": DeclineFamily.PSP_TIMEOUT,
    "gateway_error": DeclineFamily.GATEWAY_ERROR,
    "server_error": DeclineFamily.GATEWAY_ERROR,
}

_METHOD_TO_RAIL: dict[str, Rail] = {
    "card": Rail.CARD,
    "upi": Rail.UPI,
    "emandate": Rail.ENACH,
    "nach": Rail.ENACH,
}

#: Decline reasons seen on the wire with no mapping. Inspect after a test-mode
#: run and promote the real strings into _REASON_TO_FAMILY.
unmapped_reasons: set[str] = set()


def classify_decline(reason: str | None) -> DeclineFamily:
    if not reason:
        return DeclineFamily.UNKNOWN
    key = reason.strip().lower()
    family = _REASON_TO_FAMILY.get(key)
    if family is None:
        unmapped_reasons.add(key)
        log.warning("unmapped decline reason %r -> UNKNOWN", key)
        return DeclineFamily.UNKNOWN
    return family


def _resolve_rail(method: str, recurring: bool) -> Rail | None:
    rail = _METHOD_TO_RAIL.get(method.lower())
    if not recurring or rail is None:
        return rail
    # A recurring debit on card/upi is the mandate rail, not the one-time rail.
    return {Rail.CARD: Rail.CARD_SI, Rail.UPI: Rail.UPI_AUTOPAY}.get(rail, rail)


def _occurred_at(*entities: dict[str, Any]) -> datetime:
    for entity in entities:
        created = entity.get("created_at")
        if isinstance(created, (int, float)):
            return datetime.fromtimestamp(created, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _coalesce(key: str, *entities: dict[str, Any], default: Any = None) -> Any:
    """First non-empty value for `key` across the given entities.

    A subscription webhook splits its data: the payment entity carries the
    failure reason, the subscription entity carries the amount. Reading from a
    single "primary" entity silently yields zeros.
    """
    for entity in entities:
        value = entity.get(key)
        if value not in (None, "", 0):
            return value
    return default


def normalize(body: dict[str, Any]) -> RecoveryEvent | None:
    """Return None for event types we do not cover -- the caller still acks."""
    raw_type = body.get("event") or ""
    try:
        event_type = EventType(raw_type)
    except ValueError:
        return None

    payload = body.get("payload") or {}
    payment = (payload.get("payment") or {}).get("entity") or {}
    subscription = (payload.get("subscription") or {}).get("entity") or {}
    recurring = bool(subscription) or raw_type.startswith("subscription.")

    reason = payment.get("error_reason") or payment.get("error_code")
    occurred_at = _occurred_at(payment, subscription)

    # Prefer the subscription: it is the obligation that recurs. Fall back to
    # order, then the payment itself for one-off failures.
    obligation = (
        subscription.get("id")
        or payment.get("subscription_id")
        or payment.get("order_id")
        or payment.get("id")
        or "unknown"
    )

    return RecoveryEvent(
        event_id=_event_id(raw_type, payment, subscription, occurred_at),
        source=Source.RAZORPAY,
        event_type=event_type,
        occurred_at=occurred_at,
        customer_ref=_coalesce("customer_id", payment, subscription, default="unknown"),
        obligation_ref=obligation,
        # Razorpay amounts are already paise. Amount may live on either entity.
        amount_paise=int(_coalesce("amount", payment, subscription, default=0)),
        currency=_coalesce("currency", payment, subscription, default="INR"),
        rail=_resolve_rail(_coalesce("method", payment, subscription, default=""), recurring),
        decline_family=classify_decline(reason),
        decline_code_raw=reason,
        attempt_number=_attempt_number(payment, subscription, recurring),
        raw=body,
    )


def _event_id(
    raw_type: str,
    payment: dict[str, Any],
    subscription: dict[str, Any],
    occurred_at: datetime,
) -> str:
    """Idempotency key. Redelivery of one attempt must collapse; distinct retry
    attempts must not.

    Razorpay sends no webhook-level id. A payment id is unique per attempt, so
    prefer it. When there is no payment entity we fall back to the subscription
    id -- which is identical across all three retries -- so the timestamp is
    mixed in, otherwise attempts 2 and 3 would silently dedupe away as
    duplicates of attempt 1.

    UNVERIFIED: whether subscription.pending can arrive with no payment entity.
    Capture a real test-mode webhook and freeze it as a fixture to settle this.
    """
    payment_id = payment.get("id")
    if payment_id:
        return f"{raw_type}:{payment_id}"
    subscription_id = subscription.get("id")
    if subscription_id:
        return f"{raw_type}:{subscription_id}:{int(occurred_at.timestamp())}"
    return f"{raw_type}:{uuid.uuid4().hex}"


def _attempt_number(
    payment: dict[str, Any], subscription: dict[str, Any], recurring: bool
) -> int:
    """Which attempt this is.

    UNVERIFIED, and the naive reading is wrong: Razorpay's `paid_count` counts
    SUCCESSFUL charges, so it does not increment across the retries of a single
    failed cycle -- deriving the attempt from it yields the billing-cycle number
    instead. Until a real payload confirms the right field, report 1 and let the
    repository count prior events for the obligation.
    """
    if not recurring:
        return 1
    return 1
