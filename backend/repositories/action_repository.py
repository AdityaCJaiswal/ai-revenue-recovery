"""Writers for the execution spine: actions, payment_attempts, recoveries."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pymysql

Connection = pymysql.connections.Connection


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def reserve(conn: Connection, *, decision_id: str, candidate_id: int | None,
            obligation_ref: str, action_type: str, channel: str | None,
            scheduled_for: datetime | None, cost_paise: int,
            dlt_template_id: str | None = None) -> str | None:
    """Reserve exactly-once on idempotency_key (decision_id:action_type).

    Returns the new action id, or None if already reserved -- a retried
    executor run must never double-send a WhatsApp or double-fire a debit.
    (Pattern borrowed from the Surface-A prior art: reserve -> submit -> persist.)
    """
    action_id = f"a_{uuid.uuid4().hex[:16]}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO actions (id, decision_id, candidate_id, obligation_ref,
                       action_type, channel, status, scheduled_for, cost_paise,
                       dlt_template_id, idempotency_key)
                   VALUES (%s,%s,%s,%s,%s,%s,'scheduled',%s,%s,%s,%s)""",
                (action_id, decision_id, candidate_id, obligation_ref, action_type,
                 channel, _utc(scheduled_for) if scheduled_for else None,
                 cost_paise, dlt_template_id, f"{decision_id}:{action_type}"),
            )
        conn.commit()
        return action_id
    except pymysql.err.IntegrityError:
        conn.rollback()
        return None


def mark(conn: Connection, action_id: str, status: str,
         provider_ref: str | None = None, executed_at: datetime | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status=%s, provider_ref=COALESCE(%s, provider_ref), "
            "executed_at=COALESCE(%s, executed_at) WHERE id=%s",
            (status, provider_ref, _utc(executed_at) if executed_at else None, action_id),
        )
    conn.commit()


def record_attempt(conn: Connection, *, obligation_id: str, mandate_id: str | None,
                   decision_id: str, attempt_number: int, rail: str,
                   amount_paise: int, initiated_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO payment_attempts (obligation_id, mandate_id, decision_id,
                   attempt_number, rail, amount_paise, status, initiated_at)
               VALUES (%s,%s,%s,%s,%s,%s,'processing',%s)""",
            (obligation_id, mandate_id, decision_id, attempt_number, rail,
             amount_paise, _utc(initiated_at)),
        )
        attempt_id = cur.lastrowid
    conn.commit()
    return attempt_id


def settle_attempt(conn: Connection, attempt_id: int, succeeded: bool,
                   settled_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE payment_attempts SET status=%s, settled_at=%s WHERE id=%s",
            ("succeeded" if succeeded else "failed", _utc(settled_at), attempt_id),
        )
    conn.commit()


def record_recovery(conn: Connection, *, obligation_id: str, amount_paise: int,
                    rail: str | None, recovered_at: datetime,
                    attributed_action_id: str | None, days_to_cash: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO recoveries (obligation_id, amount_paise, rail, recovered_at,
                   attributed_action_id, days_to_cash)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (obligation_id, amount_paise, rail, _utc(recovered_at),
             attributed_action_id, days_to_cash),
        )
        cur.execute("UPDATE obligations SET status='recovered', resolved_at=%s WHERE id=%s",
                    (_utc(recovered_at), obligation_id))
    conn.commit()


def executable_decisions(conn: Connection, limit: int = 2000) -> list[dict[str, Any]]:
    """Treatment decisions with a real chosen action and no action row yet."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.decision_id, d.obligation_ref, d.chosen_action, d.scheduled_for,
                      e.rail, e.amount_paise, e.attempt_number, e.customer_ref,
                      dc.id AS candidate_id, dc.cost_paise
               FROM decisions d
               JOIN events e ON e.event_id = d.event_id
               LEFT JOIN decision_candidates dc
                 ON dc.decision_id = d.decision_id AND dc.action_type = d.chosen_action
               LEFT JOIN actions a ON a.decision_id = d.decision_id
               WHERE d.chosen_action IS NOT NULL
                 AND d.chosen_action != 'do_nothing'
                 AND a.id IS NULL
               ORDER BY d.decided_at
               LIMIT %s""",
            (limit,),
        )
        return list(cur.fetchall())


def json_dump(obj: Any) -> str:
    return json.dumps(obj, default=str)
