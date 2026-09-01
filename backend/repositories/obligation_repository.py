"""Materialise the world an event implies: customer, obligation, mandate.

Events carry string refs (they arrive before entities exist). This module
turns refs into rows with DETERMINISTIC ids, so upserts are idempotent across
redeliveries and re-runs.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta, timezone
from typing import Any

from ..domain.enums import DeclineFamily, EventType, Rail
from ..domain.events import RecoveryEvent

MANDATE_RAILS = {Rail.CARD_SI, Rail.UPI_AUTOPAY, Rail.ENACH}

# ASSUMPTION: no product catalogue exists, so kind is inferred from size.
# Rs 5,000+ reads as EMI, below as subscription. Swap for real metadata when a
# merchant feed exists.
EMI_THRESHOLD_PAISE = 500_000


def _det_id(prefix: str, ref: str) -> str:
    return f"{prefix}_{hashlib.md5(ref.encode()).hexdigest()[:16]}"


def upsert_customer(conn, event: RecoveryEvent) -> str:
    cid = _det_id("c", event.customer_ref)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (id, external_ref) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE id = id",
            (cid, event.customer_ref),
        )
    return cid


def upsert_obligation(conn, event: RecoveryEvent, customer_id: str) -> dict[str, Any]:
    oid = _det_id("o", event.obligation_ref)
    kind = "emi" if event.amount_paise >= EMI_THRESHOLD_PAISE else "subscription"
    recovered = event.event_type is EventType.SUBSCRIPTION_CHARGED
    status = "recovered" if recovered else "past_due"
    occurred = event.occurred_at.astimezone(timezone.utc).replace(tzinfo=None)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO obligations
                   (id, customer_id, external_ref, kind, amount_paise, status, first_failed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                   status = VALUES(status),
                   amount_paise = VALUES(amount_paise),
                   first_failed_at = LEAST(COALESCE(first_failed_at, VALUES(first_failed_at)),
                                           VALUES(first_failed_at))""",
            (oid, customer_id, event.obligation_ref, kind, event.amount_paise,
             status, None if recovered else occurred),
        )
        cur.execute("SELECT * FROM obligations WHERE id = %s", (oid,))
        return cur.fetchone()


def upsert_mandate(conn, event: RecoveryEvent, obligation_id: str) -> dict[str, Any] | None:
    """Synthesize mandate state consistent with the event.

    For real Razorpay events carrying no mandate info the status defaults to
    'active' -- the gate then reasons from the decline signal only, and its
    detail says the state was assumed, not observed.
    """
    if event.rail not in MANDATE_RAILS:
        return None

    status = {
        DeclineFamily.MANDATE_REVOKED: "revoked",
        DeclineFamily.MANDATE_PAUSED: "paused",
        DeclineFamily.MANDATE_EXPIRED: "expired",
    }.get(event.decline_family, "active")

    # Cap consistent with the story: a cap-exceeded event implies a cap BELOW
    # the amount; otherwise leave generous headroom.
    if event.decline_family is DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP:
        max_amount = max(event.amount_paise // 2, 1)
    else:
        max_amount = event.amount_paise * 2

    occurred = event.occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
    # Card SI debits sit in `processing` ~26h and cannot be cancelled.
    lock_until = occurred + timedelta(hours=26) if event.rail is Rail.CARD_SI else None

    mid = _det_id("m", event.obligation_ref)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO mandates
                   (id, obligation_id, rail, status, max_amount_paise,
                    processing_lock_until, revoked_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                   status = VALUES(status),
                   max_amount_paise = VALUES(max_amount_paise),
                   processing_lock_until = VALUES(processing_lock_until)""",
            (mid, obligation_id, event.rail.value, status, max_amount,
             lock_until, occurred if status == "revoked" else None),
        )
        cur.execute("SELECT * FROM mandates WHERE id = %s", (mid,))
        return cur.fetchone()
