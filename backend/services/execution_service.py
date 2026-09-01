"""The executor: turns chosen actions into executed actions, exactly once.

reserve -> conversion-check -> submit -> persist. Patterns adopted from the
Surface-A prior art (RESEARCH.md 11): idempotency_key reservation, and
re-checking the obligation is still unpaid AT SEND TIME, not just decision time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..adapters.razorpay.links import create_payment_link
from ..core.logging import get_logger
from ..repositories import action_repository, session
from ..utils.money import format_inr

log = get_logger(__name__)

LINK_ACTIONS = {"upi_intent_link", "whatsapp_link", "mandate_reregistration"}
MESSAGE_ACTIONS = {"sms_reminder", "email_reminder"}


def _still_owed(conn, obligation_ref: str) -> bool:
    """Conversion check at send time: never dun someone who already paid."""
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM obligations WHERE external_ref=%s", (obligation_ref,))
        row = cur.fetchone()
    return row is not None and row["status"] not in ("recovered", "cancelled")


def _obligation(conn, obligation_ref: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT o.*, m.id AS mandate_id FROM obligations o
               LEFT JOIN mandates m ON m.obligation_id = o.id
               WHERE o.external_ref=%s""",
            (obligation_ref,),
        )
        return cur.fetchone()


def _submit(conn, row: dict[str, Any], action_id: str, now: datetime) -> str:
    """Perform the action. Returns provider_ref. Simulation is labelled, never
    passed off as real."""
    act = row["chosen_action"]
    if act in LINK_ACTIONS:
        url = create_payment_link(
            row["amount_paise"],
            f"Payment recovery: {format_inr(row['amount_paise'])} ({row['obligation_ref']})",
            action_id,
        )
        return url or f"simulated:link:{action_id}"
    if act in MESSAGE_ACTIONS:
        return f"simulated:{act}:{action_id}"
    if act == "retry_at":
        ob = _obligation(conn, row["obligation_ref"])
        if ob is not None:
            attempt_id = action_repository.record_attempt(
                conn, obligation_id=ob["id"], mandate_id=ob["mandate_id"],
                decision_id=row["decision_id"], attempt_number=row["attempt_number"] + 1,
                rail=row["rail"] or "card", amount_paise=row["amount_paise"],
                initiated_at=now,
            )
            return f"attempt:{attempt_id}"
        return f"simulated:retry:{action_id}"
    if act == "voice_call":
        # Placed by the voice slice (LiveKit session); executor only reserves.
        return f"pending:voice:{action_id}"
    if act == "escalate_human":
        return f"queued:human:{action_id}"
    return f"simulated:{act}:{action_id}"


def execute_batch(limit: int = 2000, now: datetime | None = None,
                  database: str | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    executed = skipped_paid = 0
    with session(database) as conn:
        for row in action_repository.executable_decisions(conn, limit):
            action_id = action_repository.reserve(
                conn,
                decision_id=row["decision_id"], candidate_id=row["candidate_id"],
                obligation_ref=row["obligation_ref"], action_type=row["chosen_action"],
                channel=None, scheduled_for=row["scheduled_for"] or now,
                cost_paise=row["cost_paise"] or 0,
            )
            if action_id is None:
                continue  # another run already reserved it -- exactly-once holds

            if not _still_owed(conn, row["obligation_ref"]):
                action_repository.mark(conn, action_id, "cancelled",
                                       provider_ref="skipped:already_recovered")
                skipped_paid += 1
                continue

            ref = _submit(conn, row, action_id, now)
            status = "scheduled" if ref.startswith("pending:") else "executed"
            action_repository.mark(conn, action_id, status, provider_ref=ref,
                                   executed_at=None if status == "scheduled" else now)
            executed += 1

    log.info("executor: %d executed, %d skipped (already paid)", executed, skipped_paid)
    return {"executed": executed, "skipped_already_paid": skipped_paid}
