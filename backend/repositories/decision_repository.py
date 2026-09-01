"""Writes and reads for the decision spine:
decisions -> decision_candidates -> gate_evaluations.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain.actions import Candidate
from ..domain.caps import load_caps


def ensure_caps_version(conn) -> str:
    """Content-hash the caps in force and register the snapshot. Stamped on
    every decision: 'what rules were in force when you decided this?'"""
    canonical = json.dumps(load_caps(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO caps_versions (version_hash, content) VALUES (%s, %s)",
            (digest, canonical),
        )
    return digest


def get_event(conn, event_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM events WHERE event_id = %s", (event_id,))
        return cur.fetchone()


def unprocessed_events(conn, limit: int = 1000) -> list[dict[str, Any]]:
    """Events with no decision yet. Every event gets exactly one decision row --
    including successful charges (decision: nothing to recover) -- so this
    query converges to empty."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.* FROM events e
               LEFT JOIN decisions d ON d.event_id = e.event_id
               WHERE d.decision_id IS NULL
               ORDER BY e.seq LIMIT %s""",
            (limit,),
        )
        return list(cur.fetchall())


def insert_decision(
    conn,
    *,
    decision_id: str,
    event_id: str,
    obligation_ref: str,
    strategy: str,
    arm: str | None,
    diagnosis_family: str | None,
    diagnosis_rationale: str | None,
    caps_version: str,
    chosen_action: str | None,
    scheduled_for,
    candidates: list[Candidate],
) -> None:
    """One decision + its full candidate set + every gate evaluation, atomically."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO decisions
                   (decision_id, event_id, obligation_ref, strategy, arm,
                    diagnosis_family, diagnosis_rationale, caps_version,
                    chosen_action, scheduled_for)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (decision_id, event_id, obligation_ref, strategy, arm,
             diagnosis_family, diagnosis_rationale, caps_version,
             chosen_action, scheduled_for),
        )
        for cand in candidates:
            cur.execute(
                """INSERT INTO decision_candidates
                       (decision_id, action_type, channel, expected_value_paise,
                        cost_paise, success_prob_bp, rank_order, blocked, blocked_by_gate)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (decision_id, cand.action.value, cand.channel,
                 cand.expected_value_paise, cand.cost_paise + cand.annoyance_cost_paise,
                 cand.success_prob_bp, cand.rank_order, cand.blocked, cand.blocked_by_gate),
            )
            candidate_row_id = cur.lastrowid
            for g in cand.gates:
                cur.execute(
                    """INSERT INTO gate_evaluations
                           (decision_id, candidate_id, gate_name, passed, detail, rule_source)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (decision_id, candidate_row_id, g.gate, g.passed,
                     g.detail[:512], g.rule_source[:128]),
                )
    conn.commit()


def list_decisions(conn, limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.decision_id, d.obligation_ref, d.strategy, d.arm,
                      d.diagnosis_family, d.chosen_action, d.scheduled_for, d.decided_at,
                      e.rail, e.decline_family, e.amount_paise, e.event_type
               FROM decisions d JOIN events e ON e.event_id = d.event_id
               ORDER BY d.decided_at DESC, d.decision_id DESC LIMIT %s""",
            (limit,),
        )
        return list(cur.fetchall())


def get_decision(conn, decision_id: str) -> dict[str, Any] | None:
    """The decision drawer: everything about one decision."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.*, e.rail, e.decline_family, e.amount_paise, e.event_type,
                      e.customer_ref, e.attempt_number
               FROM decisions d JOIN events e ON e.event_id = d.event_id
               WHERE d.decision_id = %s""",
            (decision_id,),
        )
        decision = cur.fetchone()
        if decision is None:
            return None
        cur.execute(
            "SELECT * FROM decision_candidates WHERE decision_id = %s ORDER BY rank_order",
            (decision_id,),
        )
        candidates = list(cur.fetchall())
        cur.execute(
            "SELECT * FROM gate_evaluations WHERE decision_id = %s ORDER BY id",
            (decision_id,),
        )
        gates = list(cur.fetchall())
    by_candidate: dict[int, list] = {}
    for g in gates:
        by_candidate.setdefault(g["candidate_id"], []).append(g)
    for c in candidates:
        c["gates"] = by_candidate.get(c["id"], [])
    decision["candidates"] = candidates
    return decision


def blocked_ledger(conn, limit: int = 100) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM v_blocked_actions ORDER BY decided_at DESC LIMIT %s", (limit,))
        return list(cur.fetchall())


def metrics(conn) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM events")
        out["events_total"] = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM decisions")
        out["decisions_total"] = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT chosen_action, COUNT(*) AS n FROM decisions "
            "GROUP BY chosen_action ORDER BY n DESC"
        )
        out["chosen_actions"] = {
            (r["chosen_action"] or "WITHHELD_OR_NONE"): int(r["n"]) for r in cur.fetchall()
        }
        cur.execute("SELECT arm, COUNT(*) AS n FROM decisions GROUP BY arm")
        out["arms"] = {(r["arm"] or "-"): int(r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM decision_candidates WHERE blocked = TRUE")
        out["blocked_candidates"] = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM gate_evaluations")
        out["gate_evaluations"] = int(cur.fetchone()["n"])
        cur.execute("SELECT * FROM v_revenue_at_risk")
        row = cur.fetchone()
        out["revenue_at_risk_paise"] = int(row["at_risk_paise"] or 0)
        out["obligations_at_risk"] = int(row["obligations_at_risk"] or 0)
        cur.execute("SELECT COUNT(*) AS n FROM v_constraint_violations")
        # This MUST be zero. A non-zero value here is the bug, and we surface
        # it rather than hide it.
        out["constraint_violations"] = int(cur.fetchone()["n"])

        # ---- execution & outcomes (simulated cohort, labelled in the UI) ----
        cur.execute("SELECT COALESCE(SUM(amount_paise),0) AS r, COUNT(*) AS n, "
                    "COALESCE(AVG(days_to_cash),0) AS dtc FROM recoveries")
        row = cur.fetchone()
        out["recovered_paise"] = int(row["r"])
        out["recoveries_count"] = int(row["n"])
        out["avg_days_to_cash"] = round(float(row["dtc"]), 1)

        cur.execute("SELECT COALESCE(SUM(cost_paise),0) AS c FROM actions WHERE status='executed'")
        out["execution_cost_paise"] = int(cur.fetchone()["c"])

        cur.execute("SELECT * FROM v_incremental_recovery")
        arms = {r["arm"]: r for r in cur.fetchall()}
        t, c = arms.get("treatment"), arms.get("control")
        out["holdout"] = {
            "treatment": {"obligations": int(t["obligations"]) if t else 0,
                           "recovered_paise": int(t["recovered_paise"]) if t else 0},
            "control": {"obligations": int(c["obligations"]) if c else 0,
                         "recovered_paise": int(c["recovered_paise"]) if c else 0},
        }
        # Incremental = treatment recovery rate minus control rate, scaled to
        # treatment volume. Rates, not raw sums -- arms differ in size.
        if t and c and int(t["obligations"]) and int(c["obligations"]):
            with_amounts = {}
            cur.execute(
                "SELECT h.arm, COALESCE(SUM(o.amount_paise),0) AS total "
                "FROM holdout_assignments h JOIN obligations o ON o.id=h.obligation_id "
                "GROUP BY h.arm")
            for r in cur.fetchall():
                with_amounts[r["arm"]] = int(r["total"])
            t_rate = int(t["recovered_paise"]) / max(1, with_amounts.get("treatment", 1))
            c_rate = int(c["recovered_paise"]) / max(1, with_amounts.get("control", 1))
            out["holdout"]["treatment_rate_bp"] = int(t_rate * 10_000)
            out["holdout"]["control_rate_bp"] = int(c_rate * 10_000)
            out["holdout"]["incremental_paise"] = int(
                (t_rate - c_rate) * with_amounts.get("treatment", 0))

        cur.execute("SELECT * FROM v_ptp_kept_rate")
        row = cur.fetchone()
        out["ptp"] = {
            "total": int(row["total_promises"] or 0),
            "kept": int(row["kept"] or 0),
            "broken": int(row["broken"] or 0),
            "kept_rate_bp": int(float(row["kept_rate"]) * 10_000) if row["kept_rate"] is not None else None,
        }
    return out
