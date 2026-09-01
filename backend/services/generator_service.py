"""Synthetic event generation.

Emits the same RecoveryEvent shape the Razorpay adapter produces, so nothing
downstream can tell a synthetic failure from a real one. Test mode gives us real
signal but only a trickle; incrementality needs N.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence, TypeVar

from ..domain.enums import DeclineFamily, EventType, Rail, Source
from ..domain.events import RecoveryEvent

T = TypeVar("T")

# Rough India rail mix. ASSUMPTION, not sourced -- tune once real data exists.
_RAIL_MIX: Sequence[tuple[Rail, float]] = (
    (Rail.UPI_AUTOPAY, 0.45),
    (Rail.CARD_SI, 0.35),
    (Rail.ENACH, 0.20),
)

# Weighted so common cases dominate and hard declines stay rare -- that is what
# makes the blocked-actions ledger meaningful rather than noise.
_FAMILY_MIX: Sequence[tuple[DeclineFamily, float]] = (
    (DeclineFamily.INSUFFICIENT_FUNDS, 0.42),
    (DeclineFamily.ISSUER_DECLINE, 0.16),
    (DeclineFamily.PSP_TIMEOUT, 0.11),
    (DeclineFamily.MANDATE_REVOKED, 0.09),
    (DeclineFamily.MANDATE_PAUSED, 0.05),
    (DeclineFamily.MANDATE_EXPIRED, 0.05),
    (DeclineFamily.AUTHENTICATION_REQUIRED, 0.05),
    (DeclineFamily.HARD_DECLINE, 0.04),
    (DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP, 0.02),
    (DeclineFamily.GATEWAY_ERROR, 0.01),
)

# Two populations. A voice call costs ~Rs 5 (RESEARCH.md 8.2), so it is
# EV-negative on the first band and clearly right on the second. That contrast
# is what proves the scoring function is doing real work.
_AMOUNT_BANDS: Sequence[tuple[tuple[int, int], float]] = (
    ((19_900, 299_900), 0.70),      # Rs 199 - Rs 2,999 subscriptions
    ((200_000, 4_500_000), 0.30),   # Rs 2,000 - Rs 45,000 EMIs
)

#: Rs 15,000 AFA-free ceiling, RBI E-Mandate Framework 2026.
_AFA_FREE_CEILING_PAISE = 1_500_000


def _weighted(rng: random.Random, choices: Sequence[tuple[T, float]]) -> T:
    return rng.choices([c for c, _ in choices], weights=[w for _, w in choices])[0]


def generate_batch(count: int = 500, seed: int = 42) -> list[RecoveryEvent]:
    """Synthetic failures shaped exactly like normalized Razorpay events.

    Seeded, so a demo replays identically. Change the seed, not the code, when
    you want a different batch.
    """
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    events: list[RecoveryEvent] = []

    for index in range(count):
        family = _weighted(rng, _FAMILY_MIX)
        low, high = _weighted(rng, _AMOUNT_BANDS)
        amount_paise = rng.randrange(low, high, 100)

        if family is DeclineFamily.AMOUNT_EXCEEDS_MANDATE_CAP:
            # Must genuinely exceed the cap, or the constraint gate tests nothing.
            amount_paise = max(amount_paise, _AFA_FREE_CEILING_PAISE + 100)

        attempt = rng.choices([1, 2, 3], weights=[0.60, 0.28, 0.12])[0]
        event_type = (
            EventType.SUBSCRIPTION_HALTED if attempt >= 3 else EventType.SUBSCRIPTION_PENDING
        )

        raw = _provenance(seed, index)
        # Mastercard Merchant Advice Codes: MAC 03 = do not try again. Stamped
        # on a slice of card_si issuer declines so the MAC stopping rule has
        # real work to do in the demo.
        rail = _weighted(rng, _RAIL_MIX)
        if rail is Rail.CARD_SI and family is DeclineFamily.ISSUER_DECLINE and rng.random() < 0.30:
            raw["merchant_advice_code"] = "03"

        events.append(
            RecoveryEvent(
                event_id=f"syn_{seed}_{index:05d}",
                source=Source.SYNTHETIC,
                event_type=event_type,
                occurred_at=now - timedelta(minutes=rng.randrange(0, 60 * 72)),
                customer_ref=f"cust_syn_{rng.randrange(1, max(2, count // 3)):05d}",
                obligation_ref=f"sub_syn_{index:05d}",
                amount_paise=amount_paise,
                rail=rail,
                decline_family=family,
                decline_code_raw=family.value,
                attempt_number=attempt,
                raw=raw,
            )
        )

    return events


def _provenance(seed: int, index: int) -> dict[str, Any]:
    """Stamped into every synthetic event so the dashboard can label the cohort
    honestly rather than presenting synthetic volume as production revenue."""
    return {"synthetic": True, "seed": seed, "index": index}
