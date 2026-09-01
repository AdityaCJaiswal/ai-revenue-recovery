"""Runnable checks for the ingest path.

Covers the things that must not break: signature verification, the Razorpay ->
RecoveryEvent mapping, generator determinism, and webhook-redelivery dedupe.

    pytest -q            # or:  python tests/test_ingest.py
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters import razorpay  # noqa: E402
from backend.domain.caps import load_caps  # noqa: E402
from backend.domain.enums import DeclineFamily, EventType, Rail, Source  # noqa: E402
from backend.repositories import database, event_repository  # noqa: E402

TEST_DB = "recovery_test"


def _test_conn():
    """Fresh connection to the dedicated test database, rows cleared.
    Storage tests run against real MySQL -- the engine we ship, not a stand-in."""
    database.ensure_schema(TEST_DB)
    conn = database.connect(TEST_DB)
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in (
            "gate_evaluations", "decision_candidates", "recoveries", "actions", "decisions",
            "holdout_assignments", "payment_attempts", "promises_to_pay",
            "voice_utterances", "voice_calls", "mandates", "obligations",
            "consents", "customers", "events", "caps_versions",
        ):
            cur.execute(f"DELETE FROM {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
    conn.commit()
    return conn
from backend.services.generator_service import generate_batch  # noqa: E402
from backend.utils.money import format_inr  # noqa: E402

SECRET = "whsec_test_only_not_a_real_secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ signature

def test_signature_accepts_valid_and_rejects_everything_else():
    body = b'{"event":"payment.failed"}'
    assert razorpay.verify_webhook_signature(body, _sign(body), SECRET)

    # One flipped byte must fail -- that is the entire point of the check.
    tampered = b'{"event":"payment.faile3"}'
    assert not razorpay.verify_webhook_signature(tampered, _sign(body), SECRET)
    assert not razorpay.verify_webhook_signature(body, "deadbeef", SECRET)
    assert not razorpay.verify_webhook_signature(body, "", SECRET)
    assert not razorpay.verify_webhook_signature(body, _sign(body), "")


# ------------------------------------------------------------------ normalize

def test_recurring_upi_failure_maps_to_autopay_rail():
    event = razorpay.normalize(
        {
            "event": "subscription.pending",
            "payload": {
                "subscription": {
                    "entity": {
                        "id": "sub_TEST123",
                        "customer_id": "cust_TEST123",
                        "amount": 249900,  # Rs 2,499 in paise
                        "currency": "INR",
                        "method": "upi",
                        "paid_count": 2,
                        "created_at": 1735689600,
                    }
                },
                "payment": {
                    "entity": {"id": "pay_TEST123", "error_reason": "insufficient_funds"}
                },
            },
        }
    )

    assert event is not None
    assert event.source is Source.RAZORPAY
    assert event.event_type is EventType.SUBSCRIPTION_PENDING
    assert event.amount_paise == 249900 and isinstance(event.amount_paise, int)
    assert event.rail is Rail.UPI_AUTOPAY      # recurring upi != one-time upi
    assert event.decline_family is DeclineFamily.INSUFFICIENT_FUNDS
    assert event.retryable is True
    assert event.obligation_ref == "sub_TEST123"
    # UNVERIFIED which Razorpay field carries the retry attempt. `paid_count`
    # counts SUCCESSFUL charges, so it does not move across the retries of one
    # failed cycle -- deriving from it gives the billing cycle, not the attempt.
    # Pinned to 1 until a real payload settles it. See normalizer._attempt_number.
    assert event.attempt_number == 1


def test_hard_decline_is_not_retryable():
    event = razorpay.normalize(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_TEST999",
                        "customer_id": "cust_TEST999",
                        "amount": 50000,
                        "method": "card",
                        "error_reason": "card_stolen",
                    }
                }
            },
        }
    )
    assert event.decline_family is DeclineFamily.HARD_DECLINE
    assert event.retryable is False   # retrying is waste + scheme-fee risk


def test_unhandled_event_type_returns_none_rather_than_raising():
    assert razorpay.normalize({"event": "payout.processed", "payload": {}}) is None


def test_unmapped_decline_reason_is_recorded_not_guessed():
    event = razorpay.normalize(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {"id": "pay_X", "amount": 1000, "error_reason": "some_new_code"}
                }
            },
        }
    )
    assert event.decline_family is DeclineFamily.UNKNOWN
    assert "some_new_code" in razorpay.unmapped_reasons


# ------------------------------------------------------------------ generator

def test_generator_is_deterministic_for_a_seed():
    a = generate_batch(count=50, seed=7)
    b = generate_batch(count=50, seed=7)
    assert [e.event_id for e in a] == [e.event_id for e in b]
    assert [e.amount_paise for e in a] == [e.amount_paise for e in b]
    # A different seed must actually produce a different batch.
    assert [e.amount_paise for e in a] != [e.amount_paise for e in generate_batch(50, 8)]


def test_generated_events_are_well_formed():
    for event in generate_batch(count=200, seed=3):
        assert event.source is Source.SYNTHETIC
        assert isinstance(event.amount_paise, int) and event.amount_paise > 0
        assert event.rail is not None
        assert event.raw["synthetic"] is True
        # Cap-exceeded events must genuinely exceed the Rs 15,000 AFA-free cap,
        # or the constraint gate would be testing nothing.
        if event.decline_family is DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP:
            assert event.amount_paise > 1_500_000


# ------------------------------------------------------------------ storage

def test_redelivered_webhook_is_deduped():
    conn = _test_conn()
    try:
        event = generate_batch(count=1, seed=1)[0]
        assert event_repository.save(conn, event) is True
        assert event_repository.save(conn, event) is False  # Razorpay retries
        assert event_repository.count(conn) == 1
    finally:
        conn.close()


def test_revenue_at_risk_excludes_successful_charges():
    conn = _test_conn()
    try:
        events = generate_batch(count=20, seed=5)
        event_repository.save_many(conn, events)
        expected = sum(e.amount_paise for e in events)

        # A successful charge must drop out of the at-risk total, or the
        # headline number keeps counting money we already collected.
        charged = events[0].model_copy(
            update={"event_id": "syn_charged", "event_type": EventType.SUBSCRIPTION_CHARGED}
        )
        event_repository.save(conn, charged)
        assert event_repository.revenue_at_risk_paise(conn) == expected
    finally:
        conn.close()


# ------------------------------------------------------------------ config

def test_caps_load_and_keep_their_provenance_flags():
    caps = load_caps()
    assert caps["contact"]["voice_window_ist"] == {"start": "08:00", "end": "19:00"}
    assert caps["mandate"]["afa_free_ceiling_paise"] == 1_500_000
    # Numbers we could not verify must stay flagged as such.
    assert caps["retry"]["upi_autopay"]["max_attempts_grade"] == "UNVERIFIED"
    assert caps["retry"]["enach"]["max_attempts_grade"] == "UNVERIFIED"


def test_inr_uses_indian_digit_grouping():
    assert format_inr(1_500_000) == "Rs 15,000.00"
    assert format_inr(123_456_789) == "Rs 12,34,567.89"
    assert format_inr(19_900) == "Rs 199.00"


if __name__ == "__main__":
    checks = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in checks:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(checks)} checks passed")
