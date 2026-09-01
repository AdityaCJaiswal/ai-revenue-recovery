# Build progress

Single source of truth for what is DONE vs REMAINING. Update this file at the
end of every working session.

_Last updated: 1 Sep 2026 — FEATURE-COMPLETE. Live numbers: ₹7.3L recovered, ₹2.5L incremental (19.2% vs 11.7% control), 100% PTP kept (1/1), violations 0._

## Done ✅

| Slice | What shipped | Proof |
|---|---|---|
| Research | RESEARCH.md (1,150+ lines, [P]/[V]/[T]-labelled): India rails, e-mandate regime, voice stack, compliance, incumbents. Whitespace claim falsified & corrected honestly | published page + RESEARCH.md |
| Ingest | Razorpay webhook (HMAC verify, redelivery dedupe) + seeded synthetic generator → one canonical `RecoveryEvent`; PSP isolated in `adapters/razorpay/` | 11 unit + 9 smoke checks |
| Storage | MySQL (utf8mb4, session tz pinned +00:00), migration runner, **17 tables + 6 views** live | `SHOW FULL TABLES` = 18+6 |
| Decision pipeline | context upsert → deterministic diagnosis (10 families) → candidate enumeration → **10 gates citing rule_source** → transparent EV scoring (priors + annoyance cost, all int paise) → choose/withhold → 3-table decision spine | 11 behavioral tests |
| Strategy arms | baseline (Razorpay ladder) vs agentic, same gates for both; 15% deterministic-hash control arm recorded at decision time | tests + metrics.arms |
| Published page | page.html §11 Build status added; republished (old artifact URL was dead — new: claude.ai/code/artifact/bad9fced-d653-478a-adc4-87104dd7d870) | live |
| Dashboard | React+Vite+TS control tower served BY FastAPI (one process, system fonts, works offline): money strip · decision feed · drawer (candidates+EV bars+gates with citations) · blocked ledger · seed/run demo controls · dark+light · Razorpay-themed with PRIMARY-SOURCED tokens probed from razorpay.com's live DOM (#305EFF CTA blue as accent-only, #F0F4F6 ground, white cards, 4px radius); light-first, neutral-charcoal dark; visual pass done in a real browser (scroll containment, EV bars, 6-col strip, de-chipped feed all verified on screen) | built 49.6KB gz; all endpoints 200 via same origin |
| Execute slice | reserve→conversion-check→submit→persist exactly-once on idempotency_key; REAL Razorpay test Payment Links when keys set (they are); payment_attempts for retries; deterministic outcome simulator `/admin/simulate?days=N` (organic control 8%, truth=priors×0.9, PTP kept 62%, all ASSUMPTION-labelled); metrics: recovered/incremental-rates/days-to-cash/PTP | 13 decision tests incl exactly-once + no-double-recovery |
| Read API | `/decisions` feed · `/decisions/{id}` drawer · `/decisions/blocked` ledger · `/decisions/metrics` money strip | live run below |

**Live batch (502 events):** 502 decisions, converged (2nd run = 0) · voice 131 · retry 99 ·
re-registration 98 · UPI link 74 · human 26 · withheld (control) 74 · 281 blocked candidates ·
10,544 gate evaluations · **constraint_violations = 0** · ₹41.1L at risk on obligations.

## Remaining 🔜

| # | Slice | Notes |
|---|---|---|
| — | **Voice slice DONE**: text-mode call sim (RuleBrain, zero keys, wifi-proof) + LiveKit worker `backend/voice_agent.py` (Sarvam STTRealtime codemix + bulbul:v3, same brain, same per-utterance screen) — browser-verified end-to-end: Hinglish call → read-back → PTP → Advance → KEPT. Live voice needs SARVAM_API_KEY + LIVEKIT_* in .env, then `.venv/bin/python -m backend.voice_agent dev`. **LLM mode added** (`VOICE_BRAIN=llm`): sarvam-105b + function tools in the fibovoicebot/videopd house style (prompt_config.py, tools.py) — deterministic ratification of tool calls vs customer transcript, gated end_call, prewarmed silero VAD, per-turn latency into voice_utterances; 6 tool tests green |
| ~~1~~ | ~~Execute slice~~ DONE | actions/payment_attempts/recoveries writers; simulated outcome engine for synthetic cohort so recoveries + incrementality panel light up; scheduler for `scheduled_for`. Adopt from the Surface-A prior art (see RESEARCH.md §11): reserve→submit→persist exactly-once on `idempotency_key`; re-check obligation not already recovered AT SEND TIME; render messages from approved facts only |
| 2 | **Dashboard polish** | zones 1–3 DONE + browser-verified. Remaining: recovered/incremental tiles go live after execute slice; new-row motion |
| 3 | **Voice slice** | Sarvam ASR/TTS loop as ONE action; per-utterance screening (`voice_utterances`); PTP capture with read-back; Devanagari + number-expansion rules from RESEARCH.md §8 |
| 4 | **Real webhook fixture** | capture one real test-mode Razorpay body → settle `_event_id`/`_attempt_number`/reason-string UNVERIFIEDs (RUNBOOK §5) |
| 5 | **Demo assets** | script beats, holdout/incrementality panel, wifi-off rehearsal |
| 6 | Nice-to-have | LLM-enriched diagnosis rationale; WhatsApp sandbox send; UPI intent link live test (see RESEARCH.md open item) |

## Standing rules
- RESEARCH.md = knowledge base; caps.yaml numbers carry provenance comments; UNVERIFIED stays visible all the way into gate audit rows.
- Money is int paise everywhere. Facts tables are append-only.
- `v_constraint_violations` must always be empty — a row there is the bug.
