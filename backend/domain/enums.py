"""Domain vocabulary. Pure — no I/O, no framework imports."""

from __future__ import annotations

from enum import Enum


class Source(str, Enum):
    RAZORPAY = "razorpay"
    SYNTHETIC = "synthetic"


class Rail(str, Enum):
    """Retry physics differ per rail — RESEARCH.md 3.2."""

    CARD_SI = "card_si"          # card e-mandate; ~26h processing lock
    UPI_AUTOPAY = "upi_autopay"  # user can pay out-of-band via intent link
    ENACH = "enach"              # banking-day calendar, T+n settlement
    CARD = "card"                # one-time
    UPI = "upi"                  # one-time


class EventType(str, Enum):
    # Razorpay-native (loss surfaces B + C)
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_PENDING = "subscription.pending"
    SUBSCRIPTION_HALTED = "subscription.halted"
    SUBSCRIPTION_CHARGED = "subscription.charged"
    # Proactive, clock-derived. Emitted by the generator, so "detect risk before
    # failure" costs one enum value rather than a scheduler subsystem.
    MANDATE_EXPIRING = "mandate.expiring"
    PRE_DEBIT_NOTICE_DUE = "mandate.pre_debit_notice_due"


class DeclineFamily(str, Enum):
    """The diagnosis axis.

    RESEARCH.md 5.2 and 10: incumbents act on raw decline codes. No vendor
    surveyed separates mandate_revoked from insufficient_funds from psp_timeout
    -- and each implies a completely different action. This enum is the gap.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    ISSUER_DECLINE = "issuer_decline"
    HARD_DECLINE = "hard_decline"
    AUTHENTICATION_REQUIRED = "authentication_required"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_PAUSED = "mandate_paused"
    MANDATE_EXPIRED = "mandate_expired"
    AMOUNT_EXCEEDS_MANDATE_CAP = "amount_exceeds_mandate_cap"
    PSP_TIMEOUT = "psp_timeout"
    GATEWAY_ERROR = "gateway_error"
    UNKNOWN = "unknown"


#: Retrying these is pure waste plus scheme-fee risk. The correct intervention
#: is credential/mandate collection, not another attempt. RESEARCH.md 2.1.
NON_RETRYABLE: frozenset[DeclineFamily] = frozenset(
    {
        DeclineFamily.HARD_DECLINE,
        DeclineFamily.AUTHENTICATION_REQUIRED,
        DeclineFamily.MANDATE_REVOKED,
        DeclineFamily.MANDATE_EXPIRED,
        DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP,
    }
)


class Strategy(str, Enum):
    """baseline is the control arm, not scaffolding: it is what the industry
    ships today (Razorpay: retry tomorrow x3 then halt) and what the agent has
    to beat on a holdout."""

    BASELINE = "baseline"
    AGENTIC = "agentic"
