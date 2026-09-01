"""Canonical event shape.

Everything downstream reads a RecoveryEvent, never a PSP payload. That is what
makes the PSP swappable, and what lets a synthetic event be indistinguishable
from a real one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..utils.money import format_inr, paise_to_rupees
from .enums import NON_RETRYABLE, DeclineFamily, EventType, Rail, Source


class RecoveryEvent(BaseModel):
    """One thing that happened to one obligation."""

    event_id: str
    source: Source
    event_type: EventType
    occurred_at: datetime

    customer_ref: str
    obligation_ref: str  # the subscription/invoice this failure belongs to
    amount_paise: int    # money is always int paise -- never float
    currency: str = "INR"

    rail: Rail | None = None
    decline_family: DeclineFamily = DeclineFamily.UNKNOWN
    decline_code_raw: str | None = None
    attempt_number: int = 1

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def retryable(self) -> bool:
        return self.decline_family not in NON_RETRYABLE

    @property
    def amount_rupees(self) -> float:
        return paise_to_rupees(self.amount_paise)

    def describe(self) -> str:
        rail = self.rail.value if self.rail else "-"
        return (
            f"{self.event_type.value} {rail} {self.decline_family.value} "
            f"{format_inr(self.amount_paise)} attempt={self.attempt_number}"
        )
