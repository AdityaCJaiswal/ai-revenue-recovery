"""Money helpers.

Money is integer paise everywhere in this codebase. Floats are display-only and
never round-trip back into a calculation.
"""

from __future__ import annotations


def paise_to_rupees(paise: int) -> float:
    """Display only. Do not feed the result back into arithmetic."""
    return paise / 100


def format_inr(paise: int) -> str:
    """Indian digit grouping: 12,34,567 rather than 1,234,567."""
    rupees, sub = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts) + "," + tail
    sign = "-" if paise < 0 else ""
    return f"{sign}Rs {digits}.{sub:02d}"
