"""Per-utterance screening -- every drafted line is gated BEFORE it is spoken.

This is the tier-0 -> tier-5 gap from RESEARCH.md 10.4 closed in code: India's
default stack ships a two-sentence prompt as its guardrail; the state of
practice (Skit, US-only page) screens every utterance. Ours screens every
utterance AND writes drafted vs spoken into voice_utterances, so "show me the
bot refusing to say something" is a query.

Deterministic regex screens -- reproducible on stage, no model in the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# RBI/2022-23/108 bars threats/harassment; TCCCPR reg 2(av): promotional
# content reclassifies the communication; product rule: never improvise
# discounts the system cannot honour.
_BLOCK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("threat_or_legal", re.compile(
        r"\b(legal action|court|police|arrest|jail|FIR|lawyer|sue|blacklist|"
        r"CIBIL.{0,20}(destroy|ruin)|consequences will)\b", re.I)),
    ("harassment", re.compile(
        r"\b(shame|embarrass|family will know|tell your (employer|office|family)|"
        r"last warning|final warning)\b", re.I)),
    ("promotional_content", re.compile(
        r"\b(special offer|discount|cashback|upgrade your plan|new product|"
        r"limited time|deal for you)\b", re.I)),
    ("unauthorized_commitment", re.compile(
        r"\b(waive|waiver|write.?off|settle for less|reduce (the )?(amount|emi)|"
        r"no penalty|interest free)\b", re.I)),
    ("pii_solicitation", re.compile(
        r"\b(card number|cvv|otp|pin\b|password|net.?banking password)\b", re.I)),
]

_DISTRESS = re.compile(
    r"\b(harass|pareshan|dhamki|threat|suicide|mar (jaunga|jaungi)|depressed|"
    r"lawyer|legal|complaint|consumer court|ombudsman|shikayat)\b", re.I)
_DISPUTE = re.compile(
    r"\b(already paid|pay kar (diya|chuka)|galat (amount|charge)|wrong (amount|charge)|"
    r"fraud|dispute|maine (order|liya) hi nahi)\b", re.I)
_CEASE = re.compile(
    r"\b(stop calling|don'?t call|mat (karo|karna) call|phone mat|unsubscribe|"
    r"do not contact)\b", re.I)


@dataclass(slots=True)
class ScreenResult:
    verdict: str                 # allowed | blocked
    spoken: str | None
    blocked_reason: str | None


@dataclass(slots=True)
class CustomerSignals:
    distress: bool
    dispute: bool
    cease: bool

    @property
    def needs_handoff(self) -> bool:
        return self.distress or self.dispute


def screen_agent_line(drafted: str) -> ScreenResult:
    """Gate a drafted agent line. Blocked lines are never spoken -- the caller
    substitutes a safe fallback and the refusal is logged with its reason."""
    for reason, pattern in _BLOCK_PATTERNS:
        if pattern.search(drafted):
            return ScreenResult("blocked", None, reason)
    return ScreenResult("allowed", drafted, None)


def read_customer_signals(text: str) -> CustomerSignals:
    return CustomerSignals(
        distress=bool(_DISTRESS.search(text)),
        dispute=bool(_DISPUTE.search(text)),
        cease=bool(_CEASE.search(text)),
    )


SAFE_FALLBACK = (
    "मैं समझ सकता हूँ। मैं आपकी बात एक सहकर्मी तक पहुँचा देता हूँ जो आपकी बेहतर मदद कर पाएँगे।"
)  # "I understand. Let me connect you to a colleague who can help better."
