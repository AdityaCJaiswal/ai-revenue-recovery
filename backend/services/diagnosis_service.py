"""Failure -> root cause. Deterministic rules, not an LLM.

Reproducible on stage, zero latency, and a judge can interrogate the table.
An LLM may later ENRICH the rationale text; the classification that feeds the
gates stays deterministic. Even this table is ahead of the market: RESEARCH.md
10.1 -- no vendor surveyed distinguishes mandate_revoked from
insufficient_balance from psp_timeout.
"""

from __future__ import annotations

from typing import Any

from ..domain.actions import Diagnosis
from ..domain.enums import DeclineFamily
from ..domain.events import RecoveryEvent
from ..utils.money import format_inr


def diagnose(event: RecoveryEvent, mandate: dict[str, Any] | None) -> Diagnosis:
    f = event.decline_family
    rail = event.rail.value if event.rail else "unknown rail"
    amount = format_inr(event.amount_paise)
    n = event.attempt_number

    if f is DeclineFamily.INSUFFICIENT_FUNDS:
        return Diagnosis(
            "genuine_nsf",
            f"Balance shortfall on {rail}, attempt {n}, {amount}. Timing problem, "
            "not a churn signal -- recovery odds improve near salary credit. "
            "Customer-present nudge or a well-timed re-presentation both viable.",
        )
    if f is DeclineFamily.ISSUER_DECLINE:
        return Diagnosis(
            "issuer_soft_decline",
            f"Issuer declined (do-not-honor class) on {rail}, attempt {n}. Cause opaque "
            "by design; moderate re-presentation odds, better via customer-present rail.",
        )
    if f in (DeclineFamily.PSP_TIMEOUT, DeclineFamily.GATEWAY_ERROR):
        return Diagnosis(
            "infrastructure_transient",
            f"{f.value} on {rail} -- infrastructure, not the customer. Cheap retry has "
            "the best odds; contacting the customer would spend goodwill on our problem.",
        )
    if f is DeclineFamily.MANDATE_REVOKED:
        return Diagnosis(
            "mandate_revoked_by_customer",
            f"Customer actively revoked the {rail} mandate ({amount}). This is intent, "
            "not friction: re-presentation is prohibited and would be hostile. Only a "
            "fresh AFA-validated consent (re-registration) or a conversation recovers this.",
        )
    if f is DeclineFamily.MANDATE_PAUSED:
        return Diagnosis(
            "mandate_paused",
            f"Mandate paused by customer on {rail} -- a deliberate, reversible signal. "
            "Ask, don't debit: unpause request or conversation.",
        )
    if f is DeclineFamily.MANDATE_EXPIRED:
        return Diagnosis(
            "credential_expired",
            f"Mandate/credential expired on {rail}. Nobody chose this -- friction, not "
            "intent. Re-registration link has good odds.",
        )
    if f is DeclineFamily.AUTHENTICATION_REQUIRED:
        return Diagnosis(
            "afa_required",
            f"Issuer demands fresh AFA on {rail}. Silent retry cannot succeed; only a "
            "customer-present authentication moment can.",
        )
    if f is DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP:
        return Diagnosis(
            "amount_above_cap",
            f"{amount} exceeds the mandate cap on {rail}. RBI framework requires AFA "
            "above the ceiling -- this debit is structurally impossible silently. "
            "Customer-present payment or a higher-cap re-registration.",
        )
    if f is DeclineFamily.HARD_DECLINE:
        return Diagnosis(
            "credential_invalid",
            f"Hard decline on {rail} (lost/stolen/invalid instrument). Scheme rules bar "
            "re-presentation; the instrument is dead. Collect a new one.",
        )
    return Diagnosis(
        "unclassified",
        f"Unmapped decline '{event.decline_code_raw}' on {rail}. Conservative posture: "
        "low-cost nudge only, no retry, until the reason string is classified.",
    )
