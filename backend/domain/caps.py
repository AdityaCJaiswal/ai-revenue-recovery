"""Loader for caps.yaml.

Retry and contact limits are config rather than constants because several of
them are UNVERIFIED -- NPCI blocks programmatic fetch, and the widely-quoted UPI
AutoPay "1 + 3 retries" traces only to a marketing blog. The provenance comments
live in caps.yaml; keep them with the numbers.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from ..core.config import get_settings


@lru_cache(maxsize=1)
def load_caps() -> dict[str, Any]:
    return yaml.safe_load(get_settings().caps_path.read_text())


def retry_caps(rail: str) -> dict[str, Any]:
    return load_caps()["retry"].get(rail, {})


def contact_caps() -> dict[str, Any]:
    return load_caps()["contact"]


def mandate_caps() -> dict[str, Any]:
    return load_caps()["mandate"]


def channel_cost_paise(channel: str) -> int:
    return int(load_caps()["cost_paise"].get(channel, 0))


def is_unverified(rail: str) -> bool:
    """True when this rail's attempt cap is not primary-sourced. Surface it in
    the UI rather than presenting a guess as a rule."""
    return retry_caps(rail).get("max_attempts_grade") == "UNVERIFIED"
