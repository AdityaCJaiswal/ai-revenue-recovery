"""Razorpay webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256(raw_body, webhook_secret), hex-encoded.

    Verify the RAW bytes. Re-serialising parsed JSON changes whitespace and key
    order, so it will silently fail every time. Compared with compare_digest to
    avoid leaking timing information.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
