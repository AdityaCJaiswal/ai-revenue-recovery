"""The decision pipeline: event -> context -> diagnose -> candidates -> gates
-> score -> choose -> persist.

Two strategies share the whole pipeline; only candidate enumeration differs:
  baseline = the industry ladder (Razorpay: retry tomorrow while attempts < 3,
             then halted -> email). The control arm the agent must beat.
  agentic  = full action space, chosen by expected value.

Scoring is a transparent arithmetic function over ASSUMPTION-labelled priors --
deliberately not ML. EV = amount x P(success) - channel cost - annoyance cost,
all int paise. The annoyance line is what makes voice earn its place.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..core.config import get_settings
from ..core.logging import get_logger
from ..domain.actions import ACTION_CHANNEL, ActionType, Candidate, Diagnosis
from ..domain.caps import load_caps
from ..domain.enums import DeclineFamily, EventType, Rail
from ..domain.events import RecoveryEvent
from ..repositories import decision_repository, obligation_repository
from . import diagnosis_service
from .gate_service import GateContext, evaluate

log = get_logger(__name__)

MANDATE_BROKEN = {"revoked", "paused", "expired"}
REREG_FAMILIES = {
    DeclineFamily.MANDATE_REVOKED, DeclineFamily.MANDATE_PAUSED,
    DeclineFamily.MANDATE_EXPIRED, DeclineFamily.AUTHENTICATION_REQUIRED,
    DeclineFamily.HARD_DECLINE,
}


def _row_to_event(row: dict[str, Any]) -> RecoveryEvent:
    raw = row["raw"]
    return RecoveryEvent(
        event_id=row["event_id"],
        source=row["source"],
        event_type=row["event_type"],
        occurred_at=row["occurred_at"].replace(tzinfo=timezone.utc),
        customer_ref=row["customer_ref"],
        obligation_ref=row["obligation_ref"],
        amount_paise=row["amount_paise"],
        currency=row["currency"],
        rail=row["rail"],
        decline_family=row["decline_family"],
        decline_code_raw=row["decline_code_raw"],
        attempt_number=row["attempt_number"],
        raw=json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {}),
    )


# --------------------------------------------------------------- holdout arm

def assign_arm(obligation_ref: str) -> str:
    """Deterministic hash split: same obligation always lands in the same arm,
    reproducible across runs -- no RNG state to lose. Control = withheld."""
    fraction = get_settings().holdout_fraction
    bucket = int(hashlib.md5(obligation_ref.encode()).hexdigest()[:8], 16) % 10_000
    return "control" if bucket < int(fraction * 10_000) else "treatment"


# ------------------------------------------------------------------- scoring

def _prior_bp(action: ActionType, family: DeclineFamily, attempt: int) -> int:
    caps = load_caps()
    table = caps["success_priors_bp"].get(action.value, {})
    bp = int(table.get(family.value, table.get("default", 0)))
    if action is ActionType.RETRY_AT and attempt > 1:
        # Mechanical re-presentation gets less likely each time; a conversation
        # does not decay the same way -- decay applies to retry only.
        decay = int(caps["attempt_decay_bp"])
        for _ in range(attempt - 1):
            bp = bp * decay // 10_000
    return bp


def _costs(action: ActionType) -> tuple[int, int]:
    caps = load_caps()
    annoyance = int(caps["annoyance_cost_paise"].get(action.value, 0))
    channel_cost = {
        ActionType.SMS_REMINDER: caps["cost_paise"]["sms"],
        ActionType.WHATSAPP_LINK: caps["cost_paise"]["whatsapp"],
        ActionType.UPI_INTENT_LINK: caps["cost_paise"]["whatsapp"],
        ActionType.MANDATE_REREGISTRATION: caps["cost_paise"]["whatsapp"],
        ActionType.EMAIL_REMINDER: caps["cost_paise"]["email"],
        ActionType.VOICE_CALL: caps["cost_paise"]["voice_call_per_3min"],
        ActionType.RETRY_AT: caps["cost_paise"]["retry_attempt"],
        ActionType.ESCALATE_HUMAN: caps["escalate_human_cost_paise"],
        ActionType.DO_NOTHING: 0,
    }[action]
    return int(channel_cost), annoyance


def _score(c: Candidate, amount_paise: int) -> None:
    gross = amount_paise * c.success_prob_bp // 10_000
    c.expected_value_paise = gross - c.cost_paise - c.annoyance_cost_paise


# -------------------------------------------------------------- enumeration

def _make(action: ActionType, family: DeclineFamily, attempt: int) -> Candidate:
    cost, annoyance = _costs(action)
    return Candidate(
        action=action,
        channel=ACTION_CHANNEL[action],
        success_prob_bp=_prior_bp(action, family, attempt),
        cost_paise=cost,
        annoyance_cost_paise=annoyance,
    )


def agentic_candidates(event: RecoveryEvent, mandate: dict | None) -> list[Candidate]:
    caps = load_caps()
    fam, n = event.decline_family, event.attempt_number
    out = []
    if event.rail in (Rail.CARD_SI, Rail.UPI_AUTOPAY, Rail.ENACH, Rail.CARD):
        out.append(_make(ActionType.RETRY_AT, fam, n))
    out += [
        _make(ActionType.UPI_INTENT_LINK, fam, n),
        _make(ActionType.WHATSAPP_LINK, fam, n),
        _make(ActionType.SMS_REMINDER, fam, n),
        _make(ActionType.EMAIL_REMINDER, fam, n),
        _make(ActionType.VOICE_CALL, fam, n),
    ]
    if fam in REREG_FAMILIES or (mandate and mandate["status"] in MANDATE_BROKEN):
        out.append(_make(ActionType.MANDATE_REREGISTRATION, fam, n))
    if event.amount_paise >= int(caps["escalate_human_min_amount_paise"]):
        out.append(_make(ActionType.ESCALATE_HUMAN, fam, n))
    out.append(_make(ActionType.DO_NOTHING, fam, n))
    return out


def baseline_candidates(event: RecoveryEvent) -> list[Candidate]:
    """Razorpay's published ladder, verbatim: pending -> retry next day (x3),
    halted -> invoice email asking the customer to charge manually."""
    fam, n = event.decline_family, event.attempt_number
    primary = ActionType.RETRY_AT if n < 3 else ActionType.EMAIL_REMINDER
    return [_make(primary, fam, n), _make(ActionType.DO_NOTHING, fam, n)]


# ------------------------------------------------------------------ pipeline

def process_event(conn, event_row: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    event = _row_to_event(event_row)
    settings = get_settings()
    decision_id = f"d_{uuid.uuid4().hex[:16]}"
    caps_version = decision_repository.ensure_caps_version(conn)

    # 1. Materialise the world (also what lights up v_revenue_at_risk).
    customer_id = obligation_repository.upsert_customer(conn, event)
    obligation = obligation_repository.upsert_obligation(conn, event, customer_id)
    mandate = obligation_repository.upsert_mandate(conn, event, obligation["id"])

    # A successful charge is still a decision -- "nothing to recover" -- so the
    # unprocessed queue converges instead of reprocessing it forever.
    if event.event_type is EventType.SUBSCRIPTION_CHARGED:
        decision_repository.insert_decision(
            conn, decision_id=decision_id, event_id=event.event_id,
            obligation_ref=event.obligation_ref, strategy=settings.default_strategy,
            arm=None, diagnosis_family="payment_succeeded",
            diagnosis_rationale="Charge succeeded -- obligation recovered, no action.",
            caps_version=caps_version, chosen_action=None, scheduled_for=None,
            candidates=[],
        )
        conn.commit()
        return decision_id

    # 2. Holdout arm -- stamped into both tables at decision time.
    arm = assign_arm(event.obligation_ref)
    with conn.cursor() as cur:
        band = "emi" if event.amount_paise >= 500_000 else "subscription"
        cur.execute(
            "INSERT IGNORE INTO holdout_assignments (obligation_id, arm, stratum, batch_id) "
            "VALUES (%s,%s,%s,%s)",
            (obligation["id"], arm, f"{band}:{event.decline_family.value}", "batch_dev"),
        )

    # 3. Diagnose (deterministic -- see diagnosis_service).
    diagnosis = diagnosis_service.diagnose(event, mandate)

    # 4. Enumerate per strategy, 5. gate, 6. score.
    strategy = settings.default_strategy
    cands = (baseline_candidates(event) if strategy == "baseline"
             else agentic_candidates(event, mandate))
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM dlt_templates WHERE active = TRUE AND category IN ('utility','service') LIMIT 1")
        sms_template = cur.fetchone()
    ctx = GateContext(event=event, obligation=obligation, mandate=mandate,
                      now=now, sms_template=sms_template)
    for c in cands:
        evaluate(c, ctx)
        _score(c, event.amount_paise)

    # Rank by EV regardless of blocked state -- the drawer shows what WOULD have
    # won, which is exactly how a blocked high-EV action becomes visible.
    cands.sort(key=lambda c: c.expected_value_paise, reverse=True)
    for i, c in enumerate(cands, start=1):
        c.rank_order = i

    # 7. Choose. Control arm: full transparency, then deliberately withhold.
    chosen: Candidate | None = next(
        (c for c in cands if not c.blocked and c.expected_value_paise > 0), None
    )
    rationale = diagnosis.rationale
    if arm == "control":
        chosen_action, scheduled_for = None, None
        rationale += " [CONTROL ARM: treatment withheld for incrementality measurement.]"
    elif chosen is None or chosen.action is ActionType.DO_NOTHING:
        chosen_action, scheduled_for = ActionType.DO_NOTHING.value, None
        rationale += " [No unblocked action has positive expected value -- doing nothing IS the decision.]"
    else:
        chosen_action, scheduled_for = chosen.action.value, chosen.scheduled_for or now

    decision_repository.insert_decision(
        conn, decision_id=decision_id, event_id=event.event_id,
        obligation_ref=event.obligation_ref, strategy=strategy, arm=arm,
        diagnosis_family=diagnosis.family, diagnosis_rationale=rationale,
        caps_version=caps_version, chosen_action=chosen_action,
        scheduled_for=scheduled_for.astimezone(timezone.utc).replace(tzinfo=None) if scheduled_for else None,
        candidates=cands,
    )
    return decision_id


def process_batch(limit: int = 1000, now: datetime | None = None,
                  database: str | None = None) -> dict[str, int]:
    from ..repositories import session
    processed = 0
    with session(database) as conn:
        for row in decision_repository.unprocessed_events(conn, limit):
            process_event(conn, row, now=now)
            processed += 1
    log.info("processed %d event(s) into decisions", processed)
    return {"processed": processed}
