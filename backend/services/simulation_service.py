"""Time-compressed outcome simulation for the synthetic cohort.

SIMULATED and labelled as such everywhere. Purpose: the demo cannot wait three
real days for eNACH re-presentation, so "advance N days" rolls outcomes forward
deterministically (seeded on obligation_ref -- replayable, no RNG state).

Ground-truth model, honestly simple:
  P(recovery | executed action) = the SAME priors the scorer used, x a fixed
  ground-truth multiplier; control-arm obligations get ORGANIC_RECOVERY_BP with
  no action. The incremental panel therefore measures exactly what a holdout is
  supposed to measure: treatment effect over organic baseline.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from ..core.logging import get_logger
from ..domain.caps import load_caps
from ..repositories import action_repository, session

log = get_logger(__name__)

#: ASSUMPTION: organic recovery without any contact (customer notices and pays).
ORGANIC_RECOVERY_BP = 800  # 8%

#: Ground truth = prior x this (priors are beliefs; truth is a bit worse).
TRUTH_FACTOR_BP = 9000

#: PTP: promises kept at 62% in simulation [ASSUMPTION].
PTP_KEPT_BP = 6200


def _roll(key: str, bp: int) -> bool:
    """Deterministic 'random' draw: same key -> same outcome, forever."""
    bucket = int(hashlib.md5(key.encode()).hexdigest()[:8], 16) % 10_000
    return bucket < bp


def _prior_bp(action_type: str, family: str) -> int:
    table = load_caps()["success_priors_bp"].get(action_type, {})
    return int(table.get(family, table.get("default", 0)))


def advance_days(days: int = 3, database: str | None = None,
                 now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    recovered = attempts_settled = ptp_resolved = organic = 0

    with session(database) as conn:
        # 1. Executed actions whose scheduled time falls inside the horizon.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.id, a.action_type, a.obligation_ref, a.scheduled_for,
                          o.id AS obligation_id, o.amount_paise, o.status,
                          e.decline_family, e.rail
                   FROM actions a
                   JOIN obligations o ON o.external_ref = a.obligation_ref
                   JOIN decisions d ON d.decision_id = a.decision_id
                   JOIN events e ON e.event_id = d.event_id
                   WHERE a.status = 'executed'
                     AND o.status NOT IN ('recovered','cancelled')
                     AND (a.scheduled_for IS NULL OR a.scheduled_for <= %s)""",
                (horizon.replace(tzinfo=None),),
            )
            rows = list(cur.fetchall())

        for r in rows:
            truth_bp = _prior_bp(r["action_type"], r["decline_family"]) * TRUTH_FACTOR_BP // 10_000
            if _roll(f"outcome:{r['obligation_ref']}:{r['action_type']}", truth_bp):
                days_to_cash = 1 + int(hashlib.md5(r["obligation_ref"].encode()).hexdigest()[:2], 16) % days if days > 1 else 1
                action_repository.record_recovery(
                    conn, obligation_id=r["obligation_id"], amount_paise=r["amount_paise"],
                    rail=r["rail"], recovered_at=now + timedelta(days=days_to_cash),
                    attributed_action_id=r["id"], days_to_cash=days_to_cash,
                )
                recovered += 1

        # 2. Pending payment attempts settle inside the horizon.
        with conn.cursor() as cur:
            cur.execute("SELECT id, obligation_id FROM payment_attempts WHERE status='processing'")
            pending = list(cur.fetchall())
        for a in pending:
            action_repository.settle_attempt(conn, a["id"],
                                             _roll(f"attempt:{a['id']}", 5000), now)
            attempts_settled += 1

        # 3. Open PTPs whose promised date falls inside the horizon: kept or broken.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.id, p.obligation_id, p.amount_paise, o.external_ref, o.status AS ostatus
                   FROM promises_to_pay p JOIN obligations o ON o.id = p.obligation_id
                   WHERE p.status='open' AND p.promised_for_date <= %s""",
                (horizon.date(),),
            )
            open_ptps = list(cur.fetchall())
        for ptp in open_ptps:
            kept = _roll(f"ptp:{ptp['id']}", PTP_KEPT_BP)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE promises_to_pay SET status=%s, kept_amount_paise=%s, kept_at=%s WHERE id=%s",
                    ("kept" if kept else "broken",
                     ptp["amount_paise"] if kept else None,
                     now.replace(tzinfo=None) if kept else None, ptp["id"]),
                )
            conn.commit()
            if kept and ptp["ostatus"] not in ("recovered", "cancelled"):
                action_repository.record_recovery(
                    conn, obligation_id=ptp["obligation_id"], amount_paise=ptp["amount_paise"],
                    rail=None, recovered_at=now, attributed_action_id=None, days_to_cash=days,
                )
                recovered += 1
            ptp_resolved += 1

        # 4. Control arm: ORGANIC recovery only -- the honest counterfactual.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.obligation_id, o.amount_paise, o.status
                   FROM holdout_assignments h JOIN obligations o ON o.id = h.obligation_id
                   WHERE h.arm='control' AND o.status NOT IN ('recovered','cancelled')""",
            )
            controls = list(cur.fetchall())
        for c in controls:
            if _roll(f"organic:{c['obligation_id']}:{days}", ORGANIC_RECOVERY_BP):
                action_repository.record_recovery(
                    conn, obligation_id=c["obligation_id"], amount_paise=c["amount_paise"],
                    rail=None, recovered_at=now, attributed_action_id=None, days_to_cash=days,
                )
                organic += 1

    log.info("simulated +%dd: %d recovered (%d organic), %d attempts settled, %d PTPs resolved",
             days, recovered + organic, organic, attempts_settled, ptp_resolved)
    return {"days": days, "recovered": recovered, "organic_control": organic,
            "attempts_settled": attempts_settled, "ptp_resolved": ptp_resolved}
