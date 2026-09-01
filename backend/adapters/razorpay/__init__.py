"""Razorpay adapter: the only package that knows Razorpay's payload shape."""

from .normalizer import classify_decline, normalize, unmapped_reasons
from .signature import verify_webhook_signature

__all__ = ["classify_decline", "normalize", "unmapped_reasons", "verify_webhook_signature"]
