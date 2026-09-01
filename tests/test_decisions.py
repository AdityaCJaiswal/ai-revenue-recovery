"""Behavioral checks for the diagnose -> gate -> score pipeline.

Every test runs the REAL pipeline against real MySQL (recovery_test) with a
frozen clock, and asserts on what landed in the decision spine -- not on
intermediate Python objects.

    pytest tests/test_decisions.py     # or: python tests/test_decisions.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import get_settings  # noqa: E402
from backend.domain.enums import DeclineFamily, EventType, Rail, Source  # noqa: E402
from backend.domain.events import RecoveryEvent  # noqa: E402
from backend.repositories import decision_repository, event_repository  # noqa: E402
from backend.services.decision_service import assign_arm, process_batch, process_event  # noqa: E402
from backend.services.generator_service import generate_batch  # noqa: E402
from test_ingest import TEST_DB, _test_conn  # noqa: E402

# Frozen clocks. 05:30 UTC = 11:00 IST (inside the RBI voice window);
# 16:00 UTC = 21:30 IST (outside it).
DAY = datetime(2026, 8, 29, 5, 30, tzinfo=timezone.utc)
NIGHT = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)

RS = 100  # paise per rupee


def _ref(arm: str, base: str) -> str:
    """Deterministically find an obligation_ref landing in the wanted arm."""
    for i in range(10_000):
        ref = f"{base}_{i}"
        if assign_arm(ref) == arm:
            return ref
    raise AssertionError(f"no {arm} ref found for {base}")


def _decide(conn, *, ref: str, amount: int, rail: Rail, family: DeclineFamily,
            attempt: int = 1, now: datetime = DAY,
            event_type: EventType = EventType.SUBSCRIPTION_PENDING,
            raw: dict | None = None) -> dict:
    """Insert an event through the real repository, run the real pipeline,
    return the full decision drawer."""
    event = RecoveryEvent(
        event_id=f"t_{ref}_{attempt}",
        source=Source.SYNTHETIC,
        event_type=event_type,
        occurred_at=now - timedelta(hours=1),
        customer_ref=f"cust_{ref}",
        obligation_ref=ref,
        amount_paise=amount,
        rail=rail,
        decline_family=family,
        decline_code_raw=family.value,
        attempt_number=attempt,
        raw=raw or {"synthetic": True},
    )
    assert event_repository.save(conn, event)
    row = decision_repository.get_event(conn, event.event_id)
    decision_id = process_event(conn, row, now=now)
    return decision_repository.get_decision(conn, decision_id)


def _cand(d: dict, action: str) -> dict:
    return next(c for c in d["candidates"] if c["action_type"] == action)


# ------------------------------------------------------------ EV contrast

def test_small_subscription_voice_is_ev_negative_and_not_chosen():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "small"), amount=499 * RS,
                    rail=Rail.UPI_AUTOPAY, family=DeclineFamily.INSUFFICIENT_FUNDS)
        voice = _cand(d, "voice_call")
        # Rs 25 hard+annoyance cost against Rs 499 x 42% -- negative EV.
        assert voice["expected_value_paise"] < 0
        assert d["chosen_action"] != "voice_call"
        assert d["chosen_action"] == "retry_at"   # NSF attempt 1: retry wins
        assert d["diagnosis_family"] == "genuine_nsf"
    finally:
        conn.close()


def test_large_emi_voice_wins_in_daytime():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "emi"), amount=45_000 * RS,
                    rail=Rail.ENACH, family=DeclineFamily.INSUFFICIENT_FUNDS)
        assert d["chosen_action"] == "voice_call"
        voice = _cand(d, "voice_call")
        assert voice["expected_value_paise"] > _cand(d, "retry_at")["expected_value_paise"]
        # daytime: the voice_window gate passed and says so
        gate = next(g for g in voice["gates"] if g["gate_name"] == "voice_window")
        assert gate["passed"] and "RBI/2022-23/108" in gate["rule_source"]
    finally:
        conn.close()


def test_voice_blocked_outside_rbi_window():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "night"), amount=45_000 * RS,
                    rail=Rail.ENACH, family=DeclineFamily.INSUFFICIENT_FUNDS, now=NIGHT)
        voice = _cand(d, "voice_call")
        assert voice["blocked"] and voice["blocked_by_gate"] == "voice_window"
        assert d["chosen_action"] != "voice_call"       # falls to next-best
        gate = next(g for g in voice["gates"] if g["gate_name"] == "voice_window")
        assert not gate["passed"] and "OUTSIDE" in gate["detail"]
        # ranked list still shows voice at the top by EV -- blocked, visibly
        assert voice["rank_order"] == 1
    finally:
        conn.close()


# ------------------------------------------------------------ stopping rules

def test_mac03_blocks_retry():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "mac"), amount=2_499 * RS,
                    rail=Rail.CARD_SI, family=DeclineFamily.ISSUER_DECLINE,
                    raw={"merchant_advice_code": "03"})
        retry = _cand(d, "retry_at")
        assert retry["blocked"] and retry["blocked_by_gate"] == "merchant_advice_stop"
        assert d["chosen_action"] not in (None, "retry_at")
    finally:
        conn.close()


def test_attempt_budget_exhausted_and_unverified_flag_is_in_the_audit_row():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "budget"), amount=2_499 * RS,
                    rail=Rail.UPI_AUTOPAY, family=DeclineFamily.INSUFFICIENT_FUNDS,
                    attempt=3)
        retry = _cand(d, "retry_at")
        assert retry["blocked"] and retry["blocked_by_gate"] == "attempt_budget"
        gate = next(g for g in retry["gates"] if g["gate_name"] == "attempt_budget")
        # The cap traces to a marketing blog, and the audit row admits it.
        assert "UNVERIFIED" in gate["rule_source"]
    finally:
        conn.close()


def test_mandate_revoked_blocks_retry_and_offers_reregistration():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "revoked"), amount=2_499 * RS,
                    rail=Rail.UPI_AUTOPAY, family=DeclineFamily.MANDATE_REVOKED)
        retry = _cand(d, "retry_at")
        assert retry["blocked"]
        mandate_gate = next(g for g in retry["gates"] if g["gate_name"] == "mandate_state")
        assert not mandate_gate["passed"] and "revoked" in mandate_gate["detail"]
        assert any(c["action_type"] == "mandate_reregistration" for c in d["candidates"])
        assert d["chosen_action"] != "retry_at"
        assert d["diagnosis_family"] == "mandate_revoked_by_customer"
    finally:
        conn.close()


def test_afa_ceiling_blocks_upi_autopay_but_not_enach():
    conn = _test_conn()
    try:
        # Rs 16,000 on UPI Autopay: above the Rs 15,000 AFA-free ceiling.
        d1 = _decide(conn, ref=_ref("treatment", "cap"), amount=16_000 * RS,
                     rail=Rail.UPI_AUTOPAY, family=DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP)
        retry1 = _cand(d1, "retry_at")
        assert retry1["blocked"]
        cap_gate = next(g for g in retry1["gates"] if g["gate_name"] == "amount_within_mandate_cap")
        assert not cap_gate["passed"]

        # Same amount on eNACH: outside the e-mandate framework -- ceiling
        # does not apply, retry survives the gates.
        d2 = _decide(conn, ref=_ref("treatment", "enachok"), amount=16_000 * RS,
                     rail=Rail.ENACH, family=DeclineFamily.INSUFFICIENT_FUNDS)
        retry2 = _cand(d2, "retry_at")
        assert not retry2["blocked"]
        enach_gate = next(g for g in retry2["gates"] if g["gate_name"] == "amount_within_mandate_cap")
        assert enach_gate["passed"] and "eNACH" in enach_gate["detail"]
    finally:
        conn.close()


# ------------------------------------------------------------ arms & lifecycle

def test_control_arm_diagnoses_then_deliberately_withholds():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("control", "ctl"), amount=2_499 * RS,
                    rail=Rail.UPI_AUTOPAY, family=DeclineFamily.INSUFFICIENT_FUNDS)
        assert d["arm"] == "control"
        assert d["chosen_action"] is None
        assert "CONTROL ARM" in d["diagnosis_rationale"]
        # transparency is not withheld: candidates and gates fully recorded
        assert len(d["candidates"]) >= 5
        assert all(len(c["gates"]) > 0 for c in d["candidates"]
                   if c["action_type"] not in ("do_nothing", "escalate_human"))
    finally:
        conn.close()


def test_charged_event_resolves_with_no_action():
    conn = _test_conn()
    try:
        d = _decide(conn, ref=_ref("treatment", "paid"), amount=2_499 * RS,
                    rail=Rail.UPI_AUTOPAY, family=DeclineFamily.UNKNOWN,
                    event_type=EventType.SUBSCRIPTION_CHARGED)
        assert d["diagnosis_family"] == "payment_succeeded"
        assert d["chosen_action"] is None and len(d["candidates"]) == 0
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM obligations WHERE external_ref = %s",
                        (d["obligation_ref"],))
            assert cur.fetchone()["status"] == "recovered"
    finally:
        conn.close()


def test_baseline_strategy_is_the_razorpay_ladder():
    settings = get_settings()
    settings.default_strategy = "baseline"
    conn = _test_conn()
    try:
        d1 = _decide(conn, ref=_ref("treatment", "base1"), amount=2_499 * RS,
                     rail=Rail.CARD_SI, family=DeclineFamily.INSUFFICIENT_FUNDS, attempt=1)
        assert d1["strategy"] == "baseline" and d1["chosen_action"] == "retry_at"
        d3 = _decide(conn, ref=_ref("treatment", "base3"), amount=2_499 * RS,
                     rail=Rail.CARD_SI, family=DeclineFamily.INSUFFICIENT_FUNDS, attempt=3,
                     event_type=EventType.SUBSCRIPTION_HALTED)
        # halted -> Razorpay emails the customer to charge manually
        assert d3["chosen_action"] == "email_reminder"
    finally:
        settings.default_strategy = "agentic"
        conn.close()


def test_batch_converges_and_violations_are_zero():
    conn = _test_conn()
    try:
        events = generate_batch(count=40, seed=11)
        event_repository.save_many(conn, events)
        conn.commit()
    finally:
        conn.close()

    first = process_batch(limit=100, now=DAY, database=TEST_DB)
    assert first["processed"] == 40
    second = process_batch(limit=100, now=DAY, database=TEST_DB)
    assert second["processed"] == 0   # every event got exactly one decision

    from backend.repositories import database
    conn = database.connect(TEST_DB)
    try:
        m = decision_repository.metrics(conn)
        assert m["decisions_total"] == 40
        assert m["gate_evaluations"] > 0
        # THE invariant: nothing executed past a failed gate. Ever.
        assert m["constraint_violations"] == 0
        assert m["revenue_at_risk_paise"] > 0   # obligations table lit up
    finally:
        conn.close()




# ------------------------------------------------------------ execute + simulate

def test_executor_is_exactly_once_and_checks_conversion():
    from backend.services.execution_service import execute_batch
    conn = _test_conn()
    try:
        events = generate_batch(count=30, seed=21)
        event_repository.save_many(conn, events)
        conn.commit()
    finally:
        conn.close()
    process_batch(limit=100, now=DAY, database=TEST_DB)

    first = execute_batch(now=DAY, database=TEST_DB)
    assert first["executed"] > 0
    # Exactly-once: a second run reserves nothing new.
    second = execute_batch(now=DAY, database=TEST_DB)
    assert second["executed"] == 0

    from backend.repositories import database
    conn = database.connect(TEST_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM actions")
            assert cur.fetchone()["n"] == first["executed"] + first["skipped_already_paid"]
            # every action row carries its idempotency key
            cur.execute("SELECT COUNT(*) AS n FROM actions WHERE idempotency_key IS NULL")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_simulator_lights_up_recoveries_and_holdout():
    from backend.services.execution_service import execute_batch
    from backend.services.simulation_service import advance_days
    conn = _test_conn()
    try:
        events = generate_batch(count=60, seed=31)
        event_repository.save_many(conn, events)
        conn.commit()
    finally:
        conn.close()
    process_batch(limit=200, now=DAY, database=TEST_DB)
    execute_batch(now=DAY, database=TEST_DB)

    out = advance_days(days=3, database=TEST_DB, now=DAY)
    assert out["recovered"] + out["organic_control"] > 0

    # Determinism: re-running the same horizon must not double-recover.
    out2 = advance_days(days=3, database=TEST_DB, now=DAY)
    from backend.repositories import database
    conn = database.connect(TEST_DB)
    try:
        m = decision_repository.metrics(conn)
        assert m["recovered_paise"] > 0
        assert m["constraint_violations"] == 0
        # holdout panel has both arms populated
        assert m["holdout"]["treatment"]["obligations"] > 0
        assert m["holdout"]["control"]["obligations"] > 0
        with conn.cursor() as cur:
            # an obligation is never recovered twice
            cur.execute("SELECT obligation_id, COUNT(*) AS n FROM recoveries GROUP BY obligation_id HAVING n > 1")
            assert not cur.fetchall()  # pymysql returns a tuple, never compare to []
    finally:
        conn.close()


if __name__ == "__main__":
    checks = [(n, f) for n, f in sorted(globals().items())
              if n.startswith("test_") and callable(f)]
    for name, fn in checks:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(checks)} checks passed")
