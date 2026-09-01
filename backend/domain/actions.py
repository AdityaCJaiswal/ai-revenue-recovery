"""Decision-pipeline vocabulary: actions, candidates, gate results, diagnoses.

Pure types -- no I/O. Money stays int paise; probabilities stay int basis
points. Floats appear nowhere in scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ActionType(str, Enum):
    RETRY_AT = "retry_at"                          # re-present the debit at a chosen time
    UPI_INTENT_LINK = "upi_intent_link"            # deep link -> authenticated payment moment
    WHATSAPP_LINK = "whatsapp_link"
    SMS_REMINDER = "sms_reminder"                  # DLT template only, never improvised
    EMAIL_REMINDER = "email_reminder"
    VOICE_CALL = "voice_call"
    MANDATE_REREGISTRATION = "mandate_reregistration"
    ESCALATE_HUMAN = "escalate_human"
    DO_NOTHING = "do_nothing"


#: Delivery channel per action (None = no customer contact happens).
ACTION_CHANNEL: dict[ActionType, str | None] = {
    ActionType.RETRY_AT: None,
    ActionType.UPI_INTENT_LINK: "whatsapp",   # link rides WhatsApp in this build
    ActionType.WHATSAPP_LINK: "whatsapp",
    ActionType.SMS_REMINDER: "sms",
    ActionType.EMAIL_REMINDER: "email",
    ActionType.VOICE_CALL: "voice",
    ActionType.MANDATE_REREGISTRATION: "whatsapp",  # re-consent link
    ActionType.ESCALATE_HUMAN: None,
    ActionType.DO_NOTHING: None,
}


@dataclass(slots=True)
class Diagnosis:
    family: str          # machine key, e.g. "genuine_nsf"
    rationale: str       # human-readable, shown in the decision drawer


@dataclass(slots=True)
class GateResult:
    gate: str
    passed: bool
    detail: str
    rule_source: str     # the authority: 'RBI/2022-23/108', 'caps.yaml:... [UNVERIFIED]'


@dataclass(slots=True)
class Candidate:
    action: ActionType
    channel: str | None
    success_prob_bp: int          # 0..10000
    cost_paise: int               # hard channel cost
    annoyance_cost_paise: int     # priced customer irritation / churn risk (ASSUMPTION)
    expected_value_paise: int = 0
    rank_order: int = 0
    blocked: bool = False
    blocked_by_gate: str | None = None
    scheduled_for: datetime | None = None
    gates: list[GateResult] = field(default_factory=list)

    def first_failed_gate(self) -> GateResult | None:
        return next((g for g in self.gates if not g.passed), None)
