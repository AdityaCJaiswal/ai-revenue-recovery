"""Persistence for RecoveryEvent. The only module that writes the events table."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pymysql

from ..domain.events import RecoveryEvent

Connection = pymysql.connections.Connection

_INSERT = """
INSERT INTO events (
    event_id, source, event_type, occurred_at, customer_ref, obligation_ref,
    amount_paise, currency, rail, decline_family, decline_code_raw,
    attempt_number, raw
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def _utc_naive(dt: datetime) -> datetime:
    """DATETIME columns are UTC by construction (session tz pinned). Strip the
    tzinfo after normalising so no driver-side tz interpretation can occur."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def save(conn: Connection, event: RecoveryEvent) -> bool:
    """Persist one event.

    Returns False when this event_id already exists. Razorpay redelivers on any
    non-2xx, so the same failure legitimately arrives more than once — dedupe
    lives in the PRIMARY KEY, not in application luck.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                _INSERT,
                (
                    event.event_id,
                    event.source.value,
                    event.event_type.value,
                    _utc_naive(event.occurred_at),
                    event.customer_ref,
                    event.obligation_ref,
                    event.amount_paise,
                    event.currency,
                    event.rail.value if event.rail else None,
                    event.decline_family.value,
                    event.decline_code_raw,
                    event.attempt_number,
                    json.dumps(event.raw),
                ),
            )
        conn.commit()
        return True
    except pymysql.err.IntegrityError:
        conn.rollback()
        return False


def save_many(conn: Connection, events: list[RecoveryEvent]) -> int:
    return sum(save(conn, e) for e in events)


def list_recent(conn: Connection, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM events ORDER BY seq DESC LIMIT %s", (limit,)
        )
        return list(cur.fetchall())


def count(conn: Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM events")
        return int(cur.fetchone()["c"])


def revenue_at_risk_paise(conn: Connection) -> int:
    """Sum of unresolved failure events. Interim source until the obligations
    upsert lands — then the v_revenue_at_risk view takes over."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM events "
            "WHERE event_type != 'subscription.charged'"
        )
        return int(cur.fetchone()["total"])
