#!/usr/bin/env python
"""End-to-end check against a running server.

    .venv/bin/python scripts/smoke_test.py [--url http://localhost:8000]

Exercises the real HTTP path: signed webhook, tampered webhook, redelivery,
synthetic batch, and the read API. Exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

OK, BAD = "\033[32m  ok \033[0m", "\033[31m FAIL\033[0m"
failures = 0

#: Fresh per run. Without it, a second run against the same database would
#: legitimately report "duplicate" and look like a failure.
RUN = uuid.uuid4().hex[:8]


def call(url: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(label: str, got, want) -> None:
    global failures
    if got == want:
        print(f"{OK} {label}")
    else:
        failures += 1
        print(f"{BAD} {label}\n       expected {want!r}\n       got      {got!r}")


def failure_payload(payment_id: str, amount_paise: int, reason: str) -> bytes:
    """Shaped like a Razorpay subscription.pending body.

    NOTE: inferred, not captured from a real webhook. See docs/RUNBOOK.md §5.
    """
    return json.dumps(
        {
            "event": "subscription.pending",
            "payload": {
                "subscription": {
                    "entity": {
                        "id": f"sub_{RUN}",
                        "customer_id": f"cust_{RUN}",
                        "amount": amount_paise,
                        "currency": "INR",
                        "method": "upi",
                        "created_at": 1735689600,
                    }
                },
                "payment": {"entity": {"id": payment_id, "error_reason": reason}},
            },
        }
    ).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    base = ap.parse_args().url.rstrip("/")
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

    status, health = call(f"{base}/health")
    check("server is up", status, 200)
    enforced = health.get("webhook_signature_enforced")
    print(f"       signature enforced: {enforced}   events: {health.get('events')}")

    hook = f"{base}/webhooks/razorpay"
    body = failure_payload(f"pay_{RUN}_a", 249900, "insufficient_funds")
    sign = lambda b: hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()
    hdr = {"Content-Type": "application/json"}

    if enforced:
        if not secret:
            print(f"{BAD} RAZORPAY_WEBHOOK_SECRET not exported — cannot sign. "
                  f"Run: export RAZORPAY_WEBHOOK_SECRET=<same value as .env>")
            return 1
        status, out = call(hook, "POST", body, {**hdr, "X-Razorpay-Signature": sign(body)})
        check("signed webhook accepted", (status, out.get("status")), (200, "accepted"))

        status, out = call(hook, "POST", body, {**hdr, "X-Razorpay-Signature": "deadbeef"})
        check("tampered signature rejected", (status, out.get("status")), (401, "invalid_signature"))
    else:
        status, out = call(hook, "POST", body, hdr)
        check("unsigned webhook accepted (secret unset)", (status, out.get("status")), (200, "accepted"))
        print("       set RAZORPAY_WEBHOOK_SECRET in .env to enforce signatures")

    # Redelivery: Razorpay retries on any non-2xx. Same attempt must collapse.
    h = {**hdr, "X-Razorpay-Signature": sign(body)} if enforced else hdr
    status, out = call(hook, "POST", body, h)
    check("redelivery deduped", (status, out.get("status")), (200, "duplicate"))

    # A distinct retry attempt (new payment id) must NOT collapse.
    body2 = failure_payload(f"pay_{RUN}_b", 249900, "insufficient_funds")
    h2 = {**hdr, "X-Razorpay-Signature": sign(body2)} if enforced else hdr
    status, out = call(hook, "POST", body2, h2)
    check("distinct retry attempt kept", (status, out.get("status")), (200, "accepted"))

    # Event types outside our surfaces are acked, never 4xx'd — a non-2xx makes
    # Razorpay redeliver forever.
    other = json.dumps({"event": "payout.processed", "payload": {}, "run": RUN}).encode()
    h3 = {**hdr, "X-Razorpay-Signature": sign(other)} if enforced else hdr
    status, out = call(hook, "POST", other, h3)
    check("unhandled event acked not rejected", (status, out.get("status")), (200, "ignored"))

    status, out = call(f"{base}/admin/generate?count=500&seed=42", "POST")
    check("synthetic batch loads", status, 200)
    print(f"       saved {out.get('saved')}  duplicates {out.get('duplicates')}  total {out.get('total_events')}")

    status, out = call(f"{base}/admin/generate?count=500&seed=42", "POST")
    check("same seed is idempotent", out.get("saved"), 0)

    status, out = call(f"{base}/admin/events?limit=5")
    check("read API returns events", status, 200)
    print(f"       at risk: {out.get('revenue_at_risk_display')}  [{out.get('cohort_note')}]")
    for e in out.get("events", [])[:5]:
        print(f"         {e['event_type']:22} {str(e['rail']):12} "
              f"{e['decline_family']:26} Rs {e['amount_paise'] / 100:>11,.2f}")

    print()
    if failures:
        print(f"\033[31m{failures} check(s) failed\033[0m")
        return 1
    print("\033[32mall checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
