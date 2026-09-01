# Runbook — receiving a real Razorpay test-mode webhook

## 1. Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in MYSQL_PASSWORD + Razorpay keys
```

MySQL must be running locally (`MYSQL_*` in `.env`). The app creates the
`recovery` database and applies `backend/repositories/migrations/*.sql` itself
at startup — no manual DDL. Tests use a separate `recovery_test` database.

## 2. Razorpay dashboard (test mode)

1. Toggle **Test Mode** on — every key below must start `rzp_test_`.
2. **Settings → API Keys → Generate** → put the id/secret in `.env`.
3. **Settings → Webhooks → Add New Webhook**
   - URL: the public tunnel URL from step 3 + `/webhooks/razorpay`
   - Secret: invent one, put the same value in `RAZORPAY_WEBHOOK_SECRET`
   - Events: `payment.failed`, `subscription.pending`, `subscription.halted`,
     `subscription.charged`

## 3. Run

```bash
.venv/bin/uvicorn backend.main:app --reload --port 8000
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```

`GET /health` should report `webhook_signature_enforced: true`. If it is false,
`.env` was not loaded and the app is accepting unsigned webhooks.

## 4. Trigger a failure

Create a test-mode subscription and let a charge fail. Razorpay's ladder is
fail day 0 → `pending`, retries on days 1/2/3 → `halted`, so a natural failure
takes days — for a same-day loop, use the synthetic path:

```bash
curl -X POST 'localhost:8000/admin/generate?count=500&seed=42'
curl 'localhost:8000/admin/events?limit=5'
```

## 4b. Verify the whole path

```bash
.venv/bin/python tests/test_ingest.py                       # unit checks
export RAZORPAY_WEBHOOK_SECRET=<same value as .env>
.venv/bin/python scripts/smoke_test.py                      # live HTTP checks
```

The smoke test signs a real request, tampers with one, replays it, and sends a
distinct retry — proving signature enforcement and dedupe together. It is
re-runnable: each run uses fresh ids, so "duplicate" always means a genuine
redelivery rather than leftover state.

## 5. FIRST TASK NEXT SESSION — capture a real payload

The Razorpay adapter has **never seen a real webhook**. Both the decline-reason
strings and the payload *structure* are inferred. Capture one real body and
freeze it as a test fixture:

```bash
# Log the raw body, then replay it into tests/fixtures/
curl 'localhost:8000/health' | jq .unmapped_decline_reasons
```

Anything in `unmapped_decline_reasons` is a real string we guessed wrong —
promote it into `_REASON_TO_FAMILY` in `backend/adapters/razorpay/normalizer.py`.

This one fixture upgrades several UNVERIFIED assumptions to primary-sourced, and
settles the two open questions marked UNVERIFIED in `normalizer.py`
(`_event_id` and `_attempt_number`).


## 6. Voice (LiveKit + Sarvam)

Text-mode simulation works with ZERO keys (drawer → "Simulate the call") and is
the wifi-dead stage fallback. For live audio:

1. Fill in `.env`: `SARVAM_API_KEY` (platform.sarvam.ai, ₹100 free credits) and
   `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` (LiveKit Cloud).
2. Start the voice worker in a second terminal:
   `.venv/bin/python -m backend.voice_agent dev`
3. In the dashboard drawer of any voice-chosen decision: **🎙 Voice call (LiveKit)**
   → allow mic → speak Hinglish. Same brain, same screen, same audit rows.

Local console test without the browser: `.venv/bin/python -m backend.voice_agent console`

Two brains, selected with `VOICE_BRAIN` in `.env` (default `rule`):

| mode | brain | why |
|---|---|---|
| `rule` | deterministic RuleBrain state machine | reproducible on stage, zero LLM risk |
| `llm`  | sarvam-105b + function tools (`capture_promise`, `send_payment_link`, `escalate_human`, `end_call`) | natural conversation; tool calls are ratified deterministically against the customer transcript, `end_call('completed')` is gated on a real outcome |

Both modes pass EVERY drafted line through the same per-utterance screen at the
TTS boundary and log drafted-vs-spoken (+ per-turn latency) to `voice_utterances`.
Tool-safety checks: `python tests/test_voice_tools.py` (6 checks, no keys needed).
