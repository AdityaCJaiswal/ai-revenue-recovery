# AI Revenue Recovery — Landscape, Standards, and Whitespace

Research notes for Razorpay Buildathon. Compiled 2026-08-26.

Source-confidence labels used throughout:
- **[P]** primary docs (Stripe/Razorpay/Hyperswitch docs, RBI/statute) — quotable as fact
- **[V]** official vendor marketing claim — attribute, don't assert
- **[T]** third-party/SEO content — directional only, never quote as fact

---

## 0. The one insight that should frame the whole build

Stripe's own retry documentation lists the cases where it **will not retry a failed
subscription payment at all**. One of them is: *"The payment card is India-issued."* **[P]**

That is not a Stripe limitation — it is the RBI e-mandate regime. In India, a recurring
debit needs a registered mandate with Additional Factor Authentication, a 24-hour
pre-debit notification, and an amount inside the mandate cap. You cannot quietly
re-present a card charge eight times over two weeks and hope an issuer says yes.

**Consequence:** the entire Western recovery playbook — silent/invisible retries, ML-timed
re-presentment, account-updater-driven credential refresh — is *structurally unavailable*
as the primary lever in India. Indian revenue recovery is **customer-present recovery**:
the winning move is not "retry smarter", it is *"find the shortest, cheapest, most
compliant path back to an authenticated payment moment"* — UPI intent link, WhatsApp
payment link, mandate re-registration, eNACH re-presentation on a valid banking day,
or a human/voice conversation.

Any submission that just re-implements Smart Retries for Razorpay is solving the
American version of this problem. The Indian version is a **constrained
sequential-decision problem over communication channels and payment rails**.

---

## 1. Problem decomposition: four loss surfaces, not one

The brief bundles them, but they differ in signal source, latency, legal regime, and
who you are allowed to talk to. Treat them separately.

| Surface | Where money leaks | Detection latency | Legal regime | Counterparty |
|---|---|---|---|---|
| **A. Pre-auth** — checkout abandonment | Intent existed, no auth attempted | seconds–minutes | DPDP consent, TRAI/DLT, Meta template policy | consumer, warm |
| **B. Auth** — payment failure / decline | Auth attempted, issuer/gateway said no | real-time (webhook) | network retry caps, PA rules | consumer, present |
| **C. Mandate** — subscription / EMI / autopay failure | Recurring obligation failed | T+0 to T+3 (eNACH) | RBI E-Mandate Framework 2026, NPCI | consumer, absent |
| **D. Post-invoice** — B2B receivables / delinquency | Invoice raised, cash not arrived | days–months | MSMED/43B(h), RBI recovery-agent conduct | business or borrower |

Rough funnel economics (label each number before you put it on a slide):

- **A**: ~70.2% average cart abandonment across 50 studies (Baymard) **[T-aggregated]**;
  mobile 73–75% vs desktop 65–68% **[T]**. Classic abandoned-cart email converts ~3.3%
  (Klaviyo benchmark) **[T]**. WhatsApp-based recovery in India vendor-reported at
  15–30% of abandoned cart value **[V/T]**. Baymard puts the recoverable US+EU pool at
  ~$260B **[T]**.
- **B**: single-gateway setups ~80–85% success rate vs 90%+ with orchestration
  (Razorpay) **[V]**; automated retries recover 15–20% of failed transactions, adding
  3–5 percentage points to overall SR (Razorpay) **[V]**.
- **C**: involuntary churn is 20–40% of total SaaS churn **[T]**. Stripe claims 55% of
  failed payments recovered on average, $8.2B recovered platform-wide in 2025, and
  Deliveroo recovering >£100M using Smart Retries + card account updater + Adaptive
  Acceptance **[V]**. Adaptive Acceptance alone: $6B of falsely-declined transactions
  recovered in 2024, +60% YoY retry success **[V]**. Independent-audit claims put
  real-world Stripe Billing recovery nearer 25–35% **[T — treat as unverified]**.
  "Median dunning recovers ~half, best-in-class 70–85%" **[T]**.
- **D**: HighRadius claims ~20% reduction in past-due AR and +30% collector
  productivity **[V]**.

Also worth one line: a fifth, quieter surface — **billing/metering leakage** (unbilled
usage, wrong proration, missed price uplifts). Not in the brief's examples but it is
where a chunk of real "revenue slipping away" lives.

---

## 2. Industry standards — the plumbing judges will assume you know

### 2.1 Decline taxonomy: soft vs hard is the first branch of any recovery agent

Stripe's **non-retryable (hard decline) list** **[P]** — retrying these is pure waste and
scheme-fee risk:

`incorrect_number`, `lost_card`, `pickup_card`, `stolen_card`,
`revocation_of_authorization`, `revocation_of_all_authorizations`,
`authentication_required`, `highest_risk_level`, `transaction_not_allowed`

For these, Stripe keeps the retry *schedule* but only executes once a **new payment
method** appears — i.e. the correct intervention is credential collection, not retry. **[P]**

Soft declines worth retrying: insufficient funds (ISO 51), issuer/do-not-honor (05),
velocity/limit exceeded, temporary issuer or network unavailability, expired card
(only after an update).

**Mastercard Merchant Advice Codes** are the network's explicit machine-readable stop
signal: MAC 03 = *do not retry, no further attempts*; MAC 21 = *recurring cancelled,
stop*; other MACs = retry later. **[T, but this is well-established scheme behaviour]**

### 2.2 Network retry caps — this is literally why the brief says "stopping rules"

- **Visa** Excessive Reattempts: broadly no more than **15 attempts per decline within
  30 calendar days** on card-not-present; ~$0.10 per attempt beyond, plus non-compliance
  exposure. **[T, widely reported]**
- **Mastercard**: much tighter cadence — roughly **1 retry/day, ~10 over 30 days**, and
  per-transaction signalling via MAC; ~$0.10 per attempt after MAC 03/21. **[T]**
- Scheme monitoring programmes (Visa VAMP-style, Mastercard TPE) penalise excessive
  reattempts and high decline ratios at the merchant level. **[T]**

A serious agent maintains a **per-(instrument, decline-reason) attempt counter with a
rolling 30-day window** and refuses to act when the budget is exhausted. That counter,
plus MAC/hard-decline handling, is 80% of "compliant stopping rules" for surface B/C.

### 2.3 Retry timing — the actual state of the art

**Stripe Smart Retries** **[P]**:
- ML-chosen retry times; recommended default **8 attempts within 2 weeks**; configurable
  windows of 1 week / 2 weeks / 3 weeks / 1 month / 2 months.
- Custom (non-ML) schedules are capped at **3 retries**.
- Named signals: *number of distinct devices that presented the payment method in the
  last N hours*, and *best-time-to-pay* effects (e.g. debit cards in some countries
  convert slightly better at 12:01 AM local time).
- Payment-method fallback order is explicit: subscription default PM → subscription
  default source → customer default PM → legacy customer default source.
- End-of-schedule behaviour is a configurable policy: `canceled` / `unpaid` /
  leave `past_due`.
- **Mandate-based rails get hard caps**, which is the precedent for how to treat
  eNACH/UPI Autopay: ACH Direct Debit 2 retries / 40 days; SEPA, Bacs, AU BECS 2 / 30;
  ACSS, NZ BECS 1 / 30 — insufficient-funds only.

**Adyen RevenueAccelerate**: ML transaction-level optimisation + Real Time Account
Updater + network tokens, positioned as auth-rate uplift and churn reduction. **[V]**

Everyone's heuristics converge on the same handful of features: decline code family,
issuer/BIN behaviour, historical success by hour-of-day/day-of-month, salary-cycle
proximity, card expiry proximity, customer tenure/LTV, prior recovery history, amount.

### 2.4 Credential freshness (the unglamorous highest-ROI lever)

Visa VAU / Mastercard ABU / Amex updater services, surfaced as "Automatic card updates"
(Stripe) **[P]** or RTAU (Adyen) **[V]**. Network tokenisation goes further — the
credential self-updates. In India, RBI's card-on-file tokenisation mandate means
tokenised credentials are the norm, so the equivalent hygiene lever is **mandate
health** (expiry, cap, VPA validity) rather than PAN freshness.

### 2.5 Orchestration & cascading

- **Razorpay Optimizer**: AI/ML routing across gateways, ~150 parameters, ~600M data
  points per routing decision, claimed up to +10% success rate, downtime-aware
  failover, cost-vs-SR routing, partial/split routing. Cascading = auto-retry through a
  secondary processor without the customer re-entering details. **[V]**
- **Juspay Hyperswitch**: open-source orchestrator, 300+ processors on one API. Smart
  Retries apply *"where user action is not required after entering card information"* —
  i.e. non-3DS technical/business declines. **[P/docs]**
- **Juspay Revenue Recovery** (directly adjacent to this problem statement): ML retry
  engine, 20+ parameters, configurable **retry budget**, error-category gating (e.g.
  "retry only category-1 errors"), per-plan and per-payment-method strategies. Claims
  reduced involuntary churn and auth-rate uplift; no published numbers. **[P/marketing]**

Note the India constraint again: cascading only works where no customer action is
needed. For 3DS/AFA-gated and mandate flows, cascading is not the lever.

### 2.6 Dunning / communication standards

The industry-standard ladder, in escalation order: in-app banner → email → SMS →
WhatsApp/push → hosted invoice or payment link → self-serve card/mandate update portal →
"failed payment wall" (soft paywall gating the product) → grace period expiry →
downgrade/suspend → human/voice → agency/legal.

Reported channel effects: abandoned-cart email open ~50.5% **[T]**; Churnkey reports
its "Precision Retries" driving 66% of recoveries for some customers and a Failed
Payment Wall adding 4–12% recovery lift **[V]**. FlexPay's split of **"Invisible
Recovery"** (silent retries) vs **"Engaged Recovery"** (branded outreach) is the
cleanest published framing of the two-track model **[V]** — and in India, track one is
mostly closed, which is exactly the arbitrage.

### 2.7 Measurement standards — where almost every incumbent is weak

- **Gross recovered ≠ incremental recovered.** Attribution tells you which message
  preceded a payment; it cannot tell you which payments would have happened anyway.
  The only causal method is a **randomised holdout**: withhold treatment from an
  otherwise identical randomly-assigned control group and difference the outcomes.
- Vendor claims of "41–80% recovery" (Gravy) carry exactly this attribution problem,
  and third-party reviewers flag it. **[T]**
- Control-group hygiene matters: same eligibility, same exposure window, matched on
  amount/tenure/decline-family; extend measurement windows because recovery is lagged.
- Metric set worth reporting (surfaces A–C): revenue-at-risk detected, gross recovered,
  **incremental recovered vs holdout**, recovery rate by decline family, days-to-cash,
  contacts-per-recovery, cost per ₹ recovered, involuntary churn rate, false-positive
  rate on "at risk", constraint violations (should be zero).
- Metric set for surface D (standard AR/collections KPIs): **DSO**, ADD (average days
  delinquent), **CEI** (collections effectiveness index), aging-bucket roll rates,
  **promise-to-pay kept rate**, right-party-contact rate, cost-to-collect.

---

## 3. The India regulatory layer — where this hackathon is actually won

### 3.1 RBI Digital Payments E-Mandate Framework, 2026

Issued **21 April 2026** under PSS Act s.10(2) r/w s.18, effective immediately;
**consolidates eight circulars issued 2019–2024** into one rulebook covering recurring
transactions on cards, PPIs and UPI. Applies to banks, NBFCs, PAs/PGs, fintechs *and
the merchants they onboard* — the acquirer/PA is accountable for merchant compliance. **[P/reported]**

Operative constraints for a recovery agent:
- **One-time AFA at registration** (OTP/PIN) to create the mandate.
- **AFA-free debits up to ₹15,000**; ₹1,00,000 for specified categories (insurance
  premia, mutual fund SIPs, credit-card bill payments).
- **Pre-debit notification at least 24 hours before** each debit, stating amount, debit
  date and merchant name — and the customer may **opt out of that specific debit**.
- Customer can view / modify / pause / cancel a mandate at any time (with AFA).
- Free registration, mandated grievance redressal, zero-liability protection extended.

**What this does to your design:** every debit attempt has a 24h notification lead
time, a per-debit veto, and an amount ceiling. Retry cadence is therefore measured in
days, not minutes; "pause" and "cancel" are first-class customer actions the agent must
detect and respect; and an amount above the mandate cap requires a *customer-present*
flow, not a retry.

### 3.2 Three rails, three different retry physics

| Rail | Failure signature | Retry mechanics | Best recovery move |
|---|---|---|---|
| **Card mandate** | issuer decline codes | scheme caps + mandate cap; AFA above cap | credential/mandate refresh, then debit |
| **UPI Autopay** | insufficient balance, mandate not found/revoked, PSP timeout | mandate-level; user can pay instantly out-of-band | **UPI intent deep link** — highest-converting recovery rail in India |
| **eNACH / NACH** | bank rejection codes, T+n settlement | presentation/re-presentation, banking-day calendar | re-present on a valid day near salary credit |

Razorpay's own eMandate behaviour documents the lag explicitly: retry is attempted
**only after confirmation or rejection of the last payment, which may take more than
24 hours**, with charges shifted to T-1 or T-3 around bank holidays. **[P]**

### 3.3 Razorpay subscription failure semantics (what you would actually wire up)

From Razorpay docs **[P]**:
- `payment.failed` webhook fires on failure or timeout — also the recommended signal
  when a customer closes the checkout window or abandons (with `payment.authorized`
  watched for late authorisations).
- On failure the subscription moves to **`pending`** and an **auto-retry is attempted
  the following day**; `subscription.pending` fires.
- If retries are exhausted, the subscription moves to **`halted`**
  (`subscription.halted` fires); invoices continue to be generated but are not
  auto-charged, and the customer is emailed to charge manually.
- Success at any point fires `subscription.charged`.
- `subscription_card_change` (1/0) controls whether the customer may update card
  details at checkout.
- UPI: customer can switch payment method.

Note the honest gap: Razorpay's published retry policy is essentially **"retry
tomorrow, then halt"** — a fixed schedule, no ML timing, no channel orchestration, no
per-account strategy. That gap is your product.

Useful adjacent Razorpay surfaces: Payment Links, Magic Checkout (one-click, claimed
+15% conversion, WhatsApp Native Payments for pending-order recovery **[V]**), UPI
Intent, Smart Collect (virtual accounts for B2B reconciliation), Optimizer (routing),
RazorpayX (payouts/refunds).

### 3.4 Communication compliance (the "compliant escalation" half of the bar)

- **DPDP Act 2023** + **DPDP Rules 2025** (notified 13 Nov 2025; full enforcement of
  consent/notice/rights obligations from **13 May 2027**). Consent must be free,
  specific, informed, unambiguous; **granular and purpose-scoped** — transactional
  reminders and marketing need *separate* consent; pre-ticked boxes fail; timestamped
  consent records are expected. Penalties up to ₹250 crore (breach) / ₹50 crore
  (processing without consent). **[P/reported]**
- **TRAI TCCCPR 2018 + DLT**: commercial SMS requires registered header and
  pre-approved template; violations escalate warning → 20 msgs/day cap for 6 months →
  disconnection of telecom resources. **WhatsApp is not telecom SMS, so DLT does not
  apply** — but Meta's template categories (utility vs marketing) and opt-in rules do. **[T/P-mixed]**
- Practical design rule: payment reminders ride **utility/transactional** templates with
  a logged consent basis and an always-honoured opt-out. Never let the agent improvise
  message copy on a DLT-registered SMS channel — template IDs are pre-approved artefacts.

### 3.5 Collections conduct (surface D, and mandatory if you touch lending/EMI)

RBI Fair Practices Code / recovery-agent guidelines **[P/reported]**:
- **Contact only between 8:00 AM and 7:00 PM** — applies to calls, SMS, WhatsApp and
  email alike; outside that window is a violation even if the borrower asked for a callback.
- No abusive language, no threats (notably no threat of arrest for unsecured debt), no
  contacting family/employer to embarrass, no persistent harassment even inside the window.
- **The lender is directly liable for outsourced agent misconduct.**
- Escalation path for the borrower: Grievance Redressal Officer → Nodal Officer → RBI
  Ombudsman, each with defined timelines.

Worth borrowing as defensible defaults even outside lending: US **Reg F "7-in-7"**
(max 7 call attempts per account per 7 days, 7-day cooldown after a conversation),
TCPA consent rules, and immediate honouring of cease-communication requests. These are
the best-codified contact-frequency rules in existence and make an excellent stopping-rule
spec.

### 3.6 Indian B2B receivables law — an intervention lever no Western tool has

- **Section 43B(h), Income Tax Act** (Finance Act 2023, effective 1 Apr 2024 / AY
  2024-25): payments to **Udyam-registered micro & small enterprises** must be made
  within **15 days** absent a written agreement, or by the agreed date subject to an
  outer cap of **45 days**. Miss it and the buyer's **deduction for that expense is
  disallowed** for the year — pushed to the year of actual payment. **[P/reported]**
- **MSMED Act**: interest at **three times the RBI bank rate, compounded monthly**, on
  delayed payments; **MSME Samadhaan** portal and **MSEFC** councils for adjudication.
- **TReDS** for invoice discounting; **GST e-invoice/IRN** as invoice ground truth for
  reconciliation.

This gives a B2B chaser agent a *legitimate, factual, and extremely persuasive*
escalation rung that is unique to India: a fiscal-year-end reminder that non-payment
costs the buyer their tax deduction, plus statutory interest accrual, plus a named
adjudication forum. That is compliant escalation with real teeth and zero harassment.

---

## 4. Existing solutions, by layer

**Layer 0 — Networks/issuers:** Visa VAU, Mastercard ABU, Amex updater, network
tokenisation, Merchant Advice Codes, scheme monitoring programmes.

**Layer 1 — PSP / acquirer native:**
- *Stripe*: Smart Retries, Adaptive Acceptance, Automatic card updates, Recovery
  analytics, customer emails, no-code Automations (custom dunning per segment). The
  most complete published taxonomy — and explicitly **excludes India-issued cards from
  retries**. **[P]**
- *Adyen*: RevenueAccelerate (ML optimisation, RTAU, network tokens). **[V]**
- *Razorpay*: Optimizer (routing/cascading/failover), Magic Checkout + WhatsApp Native
  Payments, subscription auto-retry (pending→halted), Payment Links, Smart Collect,
  UPI Intent.
- *India others*: Cashfree, PayU, Juspay — **Hyperswitch** (open-source orchestrator)
  and **Juspay Revenue Recovery** (ML retry engine, retry budgets, error-category gating).

**Layer 2 — Billing platforms:** Recurly, Chargebee (configurable dunning across 30+
gateways, plus a Receivables module and Indian GST handling), Zuora, Paddle/ProfitWell
Retain, Maxio.

**Layer 3 — Failed-payment recovery specialists ("AI retry engines"):** Butter Payments
(per-merchant ML, ~128 data points/txn **[V]**), FlexPay (Invisible + Engaged Recovery,
100+ billing integrations), Gravy, Churn Buster, Churnkey (Precision Retries, Failed
Payment Wall), Slicker, FlyCode, Redux, Stunning. Almost all are US/EU-first,
card-centric, and priced on recovered revenue.

**Layer 4 — Checkout abandonment:** Shopify native abandoned checkout, Klaviyo,
Mailchimp, exit-intent/on-site tools; in India WebEngage, CleverTap, MoEngage, Wigzo,
plus Razorpay Magic Checkout's own recovery flow.

**Layer 5 — B2B AR / order-to-cash:** HighRadius (enterprise O2C suite: collections,
cash application, deductions, credit), Versapay, Billtrust, Esker, Upflow, Growfin
(collections CRM + AI cash application + DSO forecasting), Tesorio, Sidetrade (Aimie);
India-adjacent: KredX, Cashinvoice, Tally/Zoho-attached chasers.

**Layer 6 — Collections AI (lending, telco, BNPL):** Skit.ai (voice-first collections,
1B+ interactions, 53k+ creditors, compliance layer across 19+ debt types **[V]**),
TrueAccord (digital-first agency, "HeartBeat" ML journeys, email/SMS-led), Symend
(behavioural-science nudges for early-stage delinquency), Prodigal (conversation
intelligence / agent assist), InDebted, Domu, Salient, Vodex. India: Credgenics,
Spocto (Yubi), Creditas, Convin; Sarvam AI / Bhashini for Hinglish and vernacular
ASR/TTS.

**Layer 7 — Agent infrastructure:** Stripe now ships agent skills/MCP for its own
API **[P]**; LangGraph-style human-in-the-loop checkpointing; emerging agent-payment
standards (AP2, x402) are adjacent but not needed here.

---

## 5. Gap analysis — where the whitespace actually is

1. **Nobody spans the funnel.** A customer who abandons checkout, then fails a mandate,
   then goes 60 days overdue is handled by three unrelated systems with no shared
   memory, no shared contact budget, and no shared view of "how much have we already
   annoyed this person". Cross-surface state is genuinely unclaimed territory.
2. **Diagnosis is shallow.** Incumbents act on decline codes. The brief explicitly says
   *"payment degradation → root cause → recovery action"* — that middle step barely
   exists commercially. Distinguishing **issuer/BIN-level degradation** from **gateway
   downtime** from **mandate expiry/revocation** from **genuine insufficient funds**
   from **intent-to-churn** implies completely different actions, and mostly nobody does it.
3. **Intervention choice is a fixed ladder, not a decision.** Standard practice is a
   static dunning sequence. What is missing: per-account choice of channel × timing ×
   offer by **expected value net of cost, annoyance budget and legal constraint** —
   uplift/treatment-effect modelling of the kind that is routine in marketing and rare
   in recovery.
4. **Measurement is self-serving.** Vendors report gross recovered. Very few run
   holdouts. The brief's phrase *"measured money recovered across a batch"* is an
   invitation to do the honest thing and report **incremental** recovery.
5. **Compliance is bolted on, not solved as constraints.** No incumbent models
   "Visa 15-in-30 + MAC 03 + mandate cap + 24h pre-debit notice + RBI 8am–7pm window +
   DPDP consent basis + template category + annoyance budget" as one constraint set
   that every planned action must satisfy before execution.
6. **Audit trails are logs, not justifications.** Regulated Indian lenders and PAs need
   to answer *"why did you contact this person, at this time, on this channel, with this
   offer, and on what consent basis?"* No incumbent exposes that as a first-class,
   queryable artefact.
7. **India-native gaps specifically:** no silent-retry lever; UPI intent as the
   highest-converting recovery rail (largely unexploited by the recovery-tool
   category); Hinglish/vernacular voice; eNACH banking-calendar and re-presentation
   logic; and 43B(h) as compliant legal leverage in B2B.

---

## 6. Mapping the brief's "bar" to concrete requirements

| Brief phrase | What it actually demands | Standard to borrow |
|---|---|---|
| "detects revenue at risk" | scored, ranked queue with a revenue-at-risk figure, not just a failure list | decline-family + mandate-health + aging-bucket features |
| "determines the right intervention" | per-account action selection with expected value and cost | uplift modelling; FlexPay's invisible-vs-engaged split |
| "bounded recovery workflow" | hard caps on attempts, contacts, spend, time | Stripe 8/2wk; Visa 15/30; Reg F 7-in-7; ACH 2/40 |
| "measured money recovered across a batch" | gross **and** incremental vs randomised holdout | incrementality/holdout methodology |
| "compliant escalation" | ordered ladder with legal gates at each rung | RBI 8am–7pm; DPDP purpose-scoped consent; DLT templates; pre-debit notice |
| "stopping rules" | machine-checkable terminal conditions | hard-decline list; MAC 03/21; mandate revoked; opt-out; attempt budget exhausted |
| "audit trail" | per-action record of trigger, diagnosis, alternatives, constraints checked, consent basis, outcome | event-sourced decision log |

---

## 7. Implications for the build (not a design — just what the research implies)

- **Model it as a constrained sequential decision problem.**
  State = (obligation, failure history, mandate health, consent state, contactability,
  prior annoyance). Actions = {retry now, retry at time T, cascade to alternate rail,
  UPI intent link, WhatsApp payment link, SMS (template ID), email, voice call,
  request mandate re-registration, offer instalment/partial, escalate to human,
  stop/write-off}. Constraints = scheme caps ∧ mandate rules ∧ contact window ∧ consent
  ∧ frequency cap ∧ cost budget. Objective = ₹ recovered − cost − annoyance/churn risk.
- **Make the constraint layer a hard gate the planner cannot bypass**, and log every
  gate evaluation. That single design choice satisfies three of the four bar items.
- **Stopping rules to implement explicitly** (all machine-checkable): hard decline →
  stop retrying, switch to credential collection; MAC 03/21 → stop; rolling 30-day
  attempt budget exhausted → stop; mandate revoked/paused → stop and re-register;
  opt-out received → stop permanently on that channel; contact-frequency cap hit →
  cooldown; outside 08:00–19:00 IST → defer; amount > mandate cap → customer-present
  flow; value above ₹X or dispute detected → human handoff.
- **Instrument the batch with a randomised holdout** (e.g. 15–20% of accounts,
  stratified by amount and decline family). Report gross recovered, incremental
  recovered, recovery rate by cohort, days-to-cash, contacts per recovery, cost per ₹
  recovered, and constraint violations = 0. That is a far stronger demo than a bigger
  gross number.
- **Scope discipline:** pick at most two loss surfaces. The two most defensible pairings
  are (i) **payment failure + mandate/subscription recovery**, seeded from Razorpay
  test-mode `payment.failed` / `subscription.pending` / `subscription.halted` webhooks;
  or (ii) **B2B receivables + promise-to-pay tracking**, which has the richest compliant
  escalation ladder and the unique 43B(h)/MSMED leverage.
- **Promise-to-pay deserves to be a first-class object** if you go the receivables
  route: captured commitment (amount, date, channel, verbatim), kept/broken outcome,
  and a kept-rate metric. It is the standard collections KPI and almost nothing in the
  Indian SME stack tracks it.

---

## 8. Hinglish voice recovery — stack, constraints, and what incumbents actually do

Verified 27 Aug 2026. Voice is the only *synchronous, negotiable* channel available: SMS is
frozen by DLT templates, WhatsApp and email are async and one-way in practice. It is also the
only channel that returns **ground truth on why a payment failed** — the decline code says
`insufficient_funds`, the customer says *"salary 3 tareekh ko aa rahi hai"*. Those are not the
same information, and the second one is what §5.2's "diagnosis is shallow" gap is actually about.

### 8.1 Sarvam AI — the only vendor with documented first-class code-mixing

Model lineup **[P]** — https://docs.sarvam.ai/api/getting-started/models

| Model | ID | Type | Languages |
|---|---|---|---|
| Saaras v3 / v4 | `saaras:v3`, `saaras:v4` | ASR | 23 (22 Indian + English); v4 adds Global English |
| Saaras v3-realtime | `saaras:v3-realtime` | Streaming ASR (WebSocket) | as v3 |
| Bulbul v3 | `bulbul:v3` | TTS | 11 (10 Indian + English) |
| Sarvam-105B | `sarvam-105b` | LLM | 11, 128K ctx |

**Saarika is legacy** — docs direct migration to Saaras v3 **[P]**. The realtime WebSocket API
(`saaras:v3-realtime`) shipped Aug 2026, "purpose-built for voice agents" **[P/changelog]**.

**`codemix` is a documented output mode**, not an inference. `/speech-to-text` exposes five
modes: `transcribe`, `translate`, `verbatim`, `translit` (Roman output), **`codemix`** **[P]**.
No other vendor surveyed documents this.

Realtime API shape **[P]** — https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming.md
- `GET /speech-to-text-realtime/ws`; `saaras:v3-realtime` is the only accepted model
- Encodings `linear16/32`, **`mulaw`/`alaw` at 8kHz — telephony-native**, which is what a
  collections dialer actually needs
- Sample rate must be `8000` or `16000`; anything else closes the connection (code `4000`)
- Events: `transcript.partial` / `transcript.final`
- VAD: `silence_duration_ms` default **500**, `threshold` 0.3, `min_speech_duration_ms` 250

> ⚠️ **Partials are always plain transcription** regardless of mode; task modes including
> `codemix` apply **only to finals** **[P]**. You cannot render code-mixed text mid-utterance,
> which removes the usual speculative-LLM-on-partial trick unless you accept divergence.

### 8.2 The Bulbul script trap — highest-impact finding in this section

Bulbul docs **require native Indic script** and explicitly warn that *"Transliterated input
(e.g., 'Aapka order confirm ho gaya hai') significantly reduces output quality"* **[P]** —
https://docs.sarvam.ai/api-reference-docs/models/bulbul

LLMs prompted in Hinglish emit **Roman script by default**. So the naive pipeline
(LLM → Bulbul) silently degrades TTS quality. Fix in the prompt — constrain the LLM to emit
Devanagari with few-shot examples — rather than adding a Transliterate API hop (₹20/10K chars)
to the latency budget.

Bulbul publishes **no latency figures and no code-mixed-text handling guidance**
**[P — verified absence]**. TTS via WebSocket is "lowest on a warm connection", with messages
"recommended under 500 characters" — i.e. chunk at clause boundaries **[P]**.

**Voice Agents** (formerly Samvaad) is a managed ASR→LLM→TTS stack, not a speech-to-speech
model; all three models self-hosted by Sarvam, no third-party hop — relevant to both latency
and fintech data residency **[P]** — https://docs.sarvam.ai/conversations/overview

Pricing **[P]** — https://docs.sarvam.ai/api/getting-started/pricing: STT **₹30/hour**
(~₹0.50/min), Bulbul TTS **₹30/10K chars**, Transliterate ₹20/10K, ₹100 free credits.
A 3-minute call lands around **₹5 all-in**. That number matters: it is what makes voice
EV-positive on mid-size obligations, and it is what the action-selection function needs.

### 8.3 Bhashini — not a realtime component

**[P]** https://bhashini.gitbook.io/bhashini-apis — mandatory Config → Compute two-call REST
pipeline, inference at `dhruva-api.bhashini.gov.in`. Two hard blockers:

1. **Licence**: *"Usage of these APIs shall be for the purposes of PoC only"* **[P]**
2. **Architecture**: synchronous REST, **no WebSocket / streaming / partial-transcript
   endpoint anywhere in the docs** **[P — verified absence]**

Legitimate as a long-tail-language fallback and a government-alignment talking point. Not a
barge-in-capable voice loop.

### 8.4 ElevenLabs — real Hindi, but read the latency caveat

**[P]** https://elevenlabs.io/docs/overview/models — `eleven_flash_v2_5` (~75ms stated
inference) **does support Hindi**; `eleven_v3_conversational` ~280ms. Scribe v2 Realtime ~150ms.

ElevenLabs' own latency page **[P]** says the model-inference figure "is almost always" *not*
the number that matters: time-to-first-audio adds network round-trip **20–200ms**
(geography-dependent — India egress sits at the high end) plus **~500ms of client audio-player
buffering**.

Two documented gaps **[P — verified absence]**: no guidance anywhere on romanized/transliterated
or code-mixed input, and no per-language Hindi quality data. Also note **number normalization is
model-dependent and weakest on the fast model** — Multilingual v2 reads "$1,000,000" correctly
while Flash v2.5 "may say 'one thousand thousand dollars'" **[P]**. Every turn of a
payment-recovery call contains a rupee amount and a date, so **expand amounts and dates to words
in the LLM output** rather than trusting TTS normalization. Sarvam documents no normalization
behaviour at all, so this applies there too.

### 8.5 Why code-mixed ASR is hard — and why naive WER lies

Primary source: **MUCS 2021**, Diwan et al. **[P]** — https://arxiv.org/abs/2104.00235
(~600h across six Indian languages + 150h code-switched Hi-En and Bn-En).

Reported code-switching subtask WER **[P]**: abstract gives **32.45%**; the body's per-system
table gives GMM-HMM 33.35% / TDNN 29.37% / E2E 28.45% on test. *(The paper is internally
inconsistent here — likely a revision artefact. Both reported rather than picking one.)*
Hindi-English OOV rate between train and test/blind: **12.5% / 19.6%**.

**Caveat that matters more than the numbers:** the corpus is *"technical lectures on a diverse
range of computer science topics"* **[P]** — scripted CS narration, not spontaneous
conversational speech about money. Treat ~28–34% as order-of-magnitude evidence that
code-switched ASR is hard, **not** as a predictor for collections audio.

The core artefact is **inconsistent script usage** **[P]**: the same English word appears in
both Latin and native script in training data, so predictions may come back in either. This was
severe enough that the organizers invented **T-WER (transliterated WER)**, which counts an
English word correct in either script.

> **Practical consequence:** standard WER **overstates** error on code-mixed output by punishing
> correct recognitions rendered in the "wrong" script. Evaluating a Hinglish ASR vendor with
> naive WER produces a misleadingly bad number. Normalize script before scoring, or score
> semantically — *did we extract the right amount and date?*

### 8.6 The unsolved layer: entity extraction from code-switched speech

**No published benchmark exists** for numeral / date / currency extraction from code-mixed
Hindi-English speech, and **no vendor here exposes an ASR-side ITN or entity-formatting
parameter** — the Saaras realtime parameter list has no such option **[P — verified absence]**.

So `"pandrah tarikh"` / `"15th ko"` / `"agle Monday"` → `date=15` is **unsolved at the API layer
by every vendor surveyed.** It has to be handled in the LLM step, and it is the single field a
payment agent cannot afford to get wrong. Implication: don't regex the transcript — pass the raw
code-mixed transcript to the LLM under a structured-output schema and **read the parsed value
back to the caller for confirmation** (*"pandrah tareekh, yaani 15 tarikh ko — sahi hai?"*).
Read-back is both the correctness check and, in a payments context, the compliance artefact.

### 8.7 Latency budget

Turn-taking research **[P]** (Stivers et al. 2009, PNAS 106(26):10587) establishes
cross-linguistic turn-gap variation within ~250ms of a common mean. The widely-quoted
"200ms engaged / 700ms dispreferred" thresholds trace to **[T]** blog content and were *not*
verified in the primary text — directional only.

LiveKit **[P]** states voice "feels natural when end-to-end response latency stays under one
second". Their engineering blog **[V]** breaks a fully-streaming pipeline down to 400–800ms
total, with **LLM first token (300–800ms) as the slowest stage**.

Composed budget for an Indic pipeline (*derived arithmetic, not a vendor claim*):

| Component | Budget | Source |
|---|---|---|
| Sarvam VAD `silence_duration_ms` | **500ms** (default, tunable) | [P] |
| ASR finalization after endpoint | ~100–200ms | [V] |
| LLM first token | 300–800ms | [V] |
| TTS first chunk (Sarvam WS, warm) | ~100–200ms | [P] ordinal only |
| Network (in-country) | low tens of ms | [P] |
| **End-of-speech → first audio** | **~1.0–1.7s untuned; ~0.7–1.2s tuned** | derived |

**The 500ms VAD default is the cheapest available win** — dropping it to ~250–300ms buys back
200–250ms directly, at the cost of more mid-sentence false endpoints. Worth tuning against real
collections audio specifically, because people pause mid-sentence when talking about money.

### 8.8 What voice incumbents actually do

**Generate-then-screen is the state of practice.** Nobody credible lets an LLM speak unfiltered,
and nobody credible is still running pure decision trees.

- **Skit.ai** publishes the richest guardrail description found **[V]** —
  https://skit.ai/responsible-voice-ai-for-debt-collection/ : *"Every drafted line is screened in
  real time before the consumer ever hears it"*; guardrail layer "blocks anything outside
  policy"; no threats/harassment/false statements; no legal or financial advice; adversarial
  prompt-injection shielding; hardship/dispute/distress routed to a human; *"100% of calls
  transcribed, scored, and reviewable"*; immutable timestamped audit log; PTP and consent
  records retained.
  Engineering trajectory **[P]**: `github.com/skit-ai/dialogy` is a classical plugin-pipeline
  SLU toolkit (intent + entities, explicitly "not a state machine"), with a 2024 move toward
  SpeechLLM — i.e. classical SLU → LLM over time.
- **Teneo.ai [V]**: *"LLMs alone are not suitable for collections. They must be constrained by a
  logic layer"* … *"zero improvisation in regulated moments."*
- **SquadStack [V]**: "predefined Call Flows" + decision trees — the older, simpler end.

> ⚠️ **Skit's published compliance stack is calibrated to the US, not India**: 8am–**9pm**,
> 7-in-7, and "mini-Miranda" are FDCPA / Reg F constructs. India's quotable restriction is
> **8am–7pm and voice-only** (see CORRECTIONS #3). An India-calibrated constraint set is
> therefore genuinely unoccupied ground, even against the best-documented incumbent.

**The pre-due EMI reminder category in India is crowded and commoditized** — SquadStack,
Caller Digital, Vistara AI, Awaaz.ai, Rootle.ai, Botsense, Helo.ai, Elision, Smallest.ai, several
marketing "RBI Compliant" in page titles **[T — mostly SEO-grade]**. Vernacular voice for
*loan EMI* reminders is not whitespace.

> ### ⚠️ CORRECTION (28 Aug 2026) — the first draft of this section was wrong
>
> An earlier version of §8.8 claimed that voice recovery onto India's customer-present rails
> "appears unoccupied." **That claim is substantially falsified and must not be used.** At least
> three India vendors market exactly that mechanic today:
>
> | Lane | Vendor | Verbatim claim | Grade |
> |---|---|---|---|
> | Failed NACH / UPI-Autopay bounce → voice → re-mandate or UPI deep link | **Caller Digital** | *"Fire UPI Autopay mandate or one-time payment link via SMS/WhatsApp during the call. Validate confirmation in the same conversation."* | [V] product page |
> | Failed subscription / expired card → voice → payment link in-call | **Sprio AI** | *"Sprio calls churning subscribers before, during, and after a failed payment… sends the payment link to their WhatsApp or SMS in real time, while they are still on the call."* | [V] product page |
> | Voice → UPI deep link over WhatsApp mid-call (EMI) | **Rootle.ai** | *"Instantly trigger a deep-linked UPI payment handle over WhatsApp while the user is live on the call."* | [V] |
>
> **State the position as "the category exists and is thinly executed", never as "nobody is
> doing this."** The latter would not survive five minutes of judge due-diligence.

**What does survive.** The *mechanic-transfer* claim holds: the Western "guide a secure card
update → silently retry" playbook genuinely does not transfer. Even Vodex — India-founded,
selling into the US — concedes the shape on US rails: *"transfer to a PCI-compliant IVR, send a
secure payment link via SMS, or warm-handoff… Full in-call card collection requires PCI
controls"* **[V]**.

**Precision fix to how we state the regulatory premise.** Per this doc's own CORRECTIONS pass,
RBI/DPSS/2026-27/396 is **silent on retry/re-presentation**. So do **not** write "RBI blocks
retry." The accurate formulation: RBI requires AFA at registration, ≥24h pre-debit notice, an
AFA-free ceiling, and a per-debit AFA-validated opt-out — which makes *silent
credential-swap-and-retry* impossible, because a new credential requires a new AFA-validated
mandate. **That is a customer-present requirement, not a retry ban.**

**PTP-kept-rate — the gap is disclosure, not measurement.** The metric is standard
(`kept ÷ promised`) and commercially benchmarked — OpsDog literally sells a benchmark titled
*"Percentage of Inbound Promises to Pay Kept"* **[T]**. Yet no AI voice vendor publishes its own.
Every one reports the *capture* half: Skit "PTP records retained" and "14% PTP Rate" **[V]**;
Gnani case tiles quote "16% PTP gain" / "12.50% PTP base" **[V]**; Caller Digital "18–28% lift in
PTP **capture**" **[T]**; UnleashX "promise-to-pay records" **[V]**. **Vodex is the sharpest
exhibit — it names *"PTP capture rate, PTP→payment conversion"* as the KPIs a buyer should
measure, and publishes a number for neither [V].** So the framing is: *a measured metric vendors
decline to disclose*, which is stronger than "an unmeasured metric".

### 8.9 Implications for the build

- **Voice is one action in the action space**, not a parallel product — the most expensive,
  most legally constrained, highest-EV-on-large-obligations option. At ~₹5/call it is
  EV-negative on a ₹499 subscription and clearly right on a ₹45,000 EMI. That contrast is
  where the expected-value function stops being decorative.
- **Voice doubles as a diagnosis sensor.** Its output should write back into the decision log
  and reschedule downstream actions, not just close the account. This is the concrete form of
  the §5.2 diagnosis gap.
- **Sarvam end-to-end** is the defensible default: only documented `codemix`, telephony-native
  8kHz mulaw streaming, in-country self-hosted models, ~₹0.50/min STT.
- **Constrain generation, screen every utterance, hard-stop on distress/dispute/cease** — match
  the incumbent state of practice, then beat it on India-calibrated constraints.
- **Three pipeline rules that fall directly out of the primary sources**: emit Devanagari from
  the LLM (§8.2); expand amounts and dates to words before TTS (§8.4); extract entities via
  structured LLM output with spoken read-back confirmation (§8.6).
- **Measure the latency yourself on day one.** Sarvam publishes none (§8.2), and the VAD default
  alone is 500ms of the budget.

---

## 9. Voice compliance in India — verified against primary text (28 Aug 2026)

### 9.1 Bot disclosure: **there is no Indian legal duty to tell the caller it is an AI**

No instrument surveyed — RBI circulars/Master Directions, TCCCPR 2018 as amended to 2025, DPDP
Act 2023, IT Rules as amended 2026, or the India AI Governance Guidelines 2025 — requires the
*caller* to announce to the *recipient* that the speaker is a machine **[P]**.

**This is a gap, not a permission.** Three reasons the voluntary default is still to disclose:

- **The duty that exists runs to the telco, not the human.** TCCCPR reg. 4 (substituted Feb 2025)
  requires every Sender to notify the **Originating Access Provider** in advance about use of
  Auto Dialer or Robo-Calls and their objective **[P]**. The explanatory memorandum concluded
  *"there is no need for any separate regulation for the Auto dialer or Robo-Calls"* **[P]**.
- **Deception *is* actionable, even though non-disclosure is not.** TCCCPR reg. 2(bw) proviso:
  any voice call made *"in the guise of commercial communication or otherwise, to deceive the
  recipient"* is treated as UCC **[P]**. RBI/2022-23/108 separately bars *"making… anonymous
  calls"* and false or misleading representations **[P]**. An agent that conceals its nature
  **and** its principal is arguably making exactly that call.
- **MeitY's IT Amendment Rules 2026** (notified 10 Feb 2026, in force 20 Feb 2026) do mandate
  labelling of synthetic audio — but they bind **intermediaries and SSMIs on uploaded/hosted
  content**, not outbound PSTN calls **[P on scope]**. They are the clearest signal of
  regulatory direction, and they change the analysis completely **if you clone a real person's
  voice**. → **Product rule: synthetic persona voice only, never a clone of a named officer.**

**Defensible default: disclose AI status in the opening seconds, and always answer truthfully
when asked "am I talking to a robot?"** Cheap, costs nothing legally, and neutralises the largest
reputational and unfair-practice risk. Present it as a *voluntary* standard — never claim it is
Indian compliance.

RBI's **FREE-AI Committee report** (13 Aug 2025) recommends transparency in AI interactions and
entity-level AI disclosure in annual reports **[T — read via a KPMG summary; the RBI original was
not retrieved]**. It is a committee report, not a direction, and its disclosure item is
entity-level, not per-call. Do not cite it as binding.

### 9.2 RBI recovery-agent conduct — 8:00–19:00 is **confirmed voice-only** [P]

**RBI/2022-23/108** (DOR.ORG.REC.65/21.04.158/2022-23, 12 Aug 2022) —
https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12378&Mode=0

The CORRECTIONS-pass finding is now confirmed from the circular's own structure: the time window
sits **grammatically inside the *calls* bullet** — *"making threatening and/or anonymous calls,
persistently calling the borrower and/or calling the borrower before 8:00 a.m. and after
7:00 p.m."* — while *"sending inappropriate messages either on mobile or through social media"*
is a **separate bullet with no time qualifier at all** — a content-based, time-agnostic bar.

Operative consequences:
1. **Voice: hard gate 08:00–19:00 borrower-local.** No textual exception.
2. **SMS/WhatsApp: not time-limited by this circular**, but content-limited at every hour, and
   separately constrained by TRAI preference bands where the traffic is preference-governed.
3. **Never copy the US 21:00 end time.** A stack calibrated to 8am–9pm is **non-compliant in
   India for the 19:00–21:00 slot** — the single most likely way an imported product breaks
   Indian law.

**Scope:** banks (excl. Payments Banks), RRBs, SFBs, AIFIs, **NBFCs and HFCs**, co-op banks, ARCs.
**Carve-out:** microfinance loans are governed separately by the Microfinance Loans Directions
2022 — check that Master Direction before touching an MFI book **[P]**.

**Liability does not transfer.** The circular is framed as *responsibilities of regulated
entities employing recovery agents*, under the outsourcing regime. Outsourcing to an AI voice
vendor leaves the lender answerable for every call **[P]**. → **Per-call auditability is a sales
requirement, not a nice-to-have.**

**Nothing in RBI addresses AI or automated calling specifically** **[P — verified absence]**. The
rules are conduct-based, so the agent is judged by what it *says*. *"The model generated it"* is
not a defence — which is precisely the argument for a real-time gate on generated utterances.

### 9.3 Recording is **mandatory** for banks; announcing it is not [P]

**RBI/2007-2008/296** (DBOD.No.Leg.BC.75/09.07.005/2007-08, 24 Apr 2008) —
https://www.rbi.org.in/scripts/NotificationUser.aspx?Id=4141&Mode=0

> *"Banks should ensure that there is a tape recording of the content / text of the calls made by
> recovery agents to the customers, and vice-versa."*

Bidirectional and still operative. **A voice product that does not record 100% of calls cannot be
sold into a bank collections function.** The same circular makes informing the customer of
recording **permissive ("may")**, not mandatory.

Adjacent duty worth exploiting: **RBI Digital Lending Directions 2025** reportedly require the
assigned recovery agent's details to be sent to the borrower **before contact** **[T — law-firm
sourced, RBI original not retrieved]**. If so, that pre-call SMS is the natural, zero-overhead
place to also state that contact may be by an automated voice assistant — turning the §9.1
voluntary disclosure into an already-mandated message.

### 9.4 DPDP — the phasing matters more than the principles [P on dates]

DPDP Rules 2025 notified 13 Nov 2025 with **eighteen-month phased commencement**: Phase 1
(13 Nov 2025) definitions + Board; Phase 2 (13 Nov 2026) consent-manager registration and
enforcement machinery; **Phase 3 (13 May 2027) — notice, consent standards, rights, security
safeguards, breach reporting, retention and deletion.**

> **As at today the operative DPDP notice/consent/retention machinery is NOT yet in force.
> The binding recording obligation on a collections voice product today is RBI's 2008 mandate.**
> Do not let a compliance slide claim "DPDP-compliant" as though the Rules were live — but build
> to the 13 May 2027 standard now, because re-architecting a voice-data pipeline later is
> expensive.

Two points that are load-bearing when it does bite:

- **Consent is very likely the *wrong* lawful ground — and that is good news.** DPDP s.7
  "legitimate uses" operate without consent. Servicing an existing loan and complying with an RBI
  recording mandate sit far better there than under consent. **If recording ran on consent, a
  borrower could withdraw it and defeat the RBI mandate.** *(Exact s.7 sub-clause still to be
  confirmed against the bare Act — see Still open.)*
- **Erasure does not beat retention law.** DPDP's erasure duty yields where retention is required
  by other law (RBI recording mandate, PMLA/KYC). **Build erasure with a regulatory-hold
  exception from day one.**

Recording a call you are party to is safe under Indian practice **[T]**, commonly traced to
*R.M. Malkani* (1973) — but note that case is about **admissibility in a criminal prosecution**,
not a general commercial-recording consent rule, and is routinely over-read in vendor material.
There is no Indian two-party-consent statute.

### 9.5 TRAI — a payment reminder is a **Service** voice call, and it survives DND [P]

TCCCPR 2018 governs *"any voice call **or** message"* — **voice is co-equal with SMS**, so DLT is
not an SMS-only regime **[P]**. Reg. 2(bc) defines **"Robo Calls"** — *"any call made… using an
artificial or prerecorded voice to interactively deliver a voice message without the involvement
of human being on calling side"* **[P]**. That is the closest thing in Indian law to a definition
covering an AI voice agent.

**Classification chain** (this determines everything downstream):
- The 2025 amendment **narrowed "Transactional"** to a Sender's response to a *customer-initiated
  transaction **within thirty minutes*** **[P]**. A payment reminder is neither → **not
  transactional.**
- It lands instead in reg. 2(bh)(i) **"Service Voice Call"** — to *its own Customer*, about an
  existing product/service, not promotional, and **does not require Explicit Consent** **[P]**.
- Reg. 2(bw) **excludes service voice calls from UCC**, and reg. 2(z) provides that "Fully
  blocked" preference does not block communication sent under **inferred consent** **[P]**.
  → **A service voice call to your own borrower is not blocked by DND.**

Numbering: **1600-series for service/transactional** robo-calls, **140-series for promotional**
**[P]**.

> **[INTERPRETATION, not [P]]** Recovery and payment-reminder calls are **not expressly
> enumerated** in reg. 2(bh)(i). The classification above is a *defensible reading* — own
> customer, existing product, analogous to the enumerated "periodic balance alerts", not
> promotional — and it is what the industry operates on, but **it has not been tested by TRAI
> adjudication.** Get the lender's own DLT/telecom compliance sign-off before relying on it.

**The promotional-contamination trap [P]:** reg. 2(av) — if promotional content is mixed with
**any** commercial voice call, the **whole call reclassifies as promotional** (140-series,
DND-scrubbed, consent required, time-band blockable).
→ **Hard product rule: the collections agent must never up-sell, cross-sell, or pitch a top-up on
the call.** This belongs in the constraint gate, not the style guide.

**Time bands are preference-driven, not statutory.** TCCCPR's nine two-hourly opt-out bands bite
only on **preference-governed** traffic; inferred-consent service calls sit outside them
**[P]**. → **The binding time constraint on a collections voice agent is RBI's 08:00–19:00, not
TRAI's bands.**

Also live: DLT Sender registration is mandatory (unregistered senders face disconnection of *all*
telecom resources); UCC complaints must be lodged within seven days; header suffixes -P/-S/-T/-G
**[P]**.

### 9.6 Foreign rules — voluntary defaults only, **not Indian law**

- **EU AI Act Art. 50(1)** **[P]** — the cleanest bot-disclosure rule anywhere: systems
  "intended to interact directly with natural persons" must inform them they are interacting with
  an AI, disclosed "at the latest at the time of the first interaction."
- **US Reg F** **[P]** — 12 CFR 1006.6(b)(1)(i) 8am–9pm local; 1006.14(b)(2)(i) the "7-in-7"
  rebuttable presumption.
- **US TCPA / FCC 24-17** (adopted 2 Feb 2024) **[P]** — confirms *"'artificial or prerecorded
  voice' encompass current AI technologies that generate human voices"*, covering both wholly
  synthetic and cloned voices, and requiring prior express consent. Note even the US mandates
  disclosure of the **responsible entity**, not of the machine nature of the caller.

---

## 10. The honest whitespace (revised 28 Aug 2026)

Both the strong and the narrowed versions of the "nobody does this" claim are falsified (§8.8).
What survives is better, because it is about **rigour rather than novelty** — and it is evidenced.

**1. Nobody reasons about mandate state.** Sprio says "failed payment, expired card" — card-era
language with no e-mandate model. Caller Digital names NACH and re-mandate but never AFA, the
≥24h pre-debit notice, the ₹15,000 AFA-free ceiling, the ~26h `processing` lock on card SI, or
NACH banking-calendar re-presentation. **No vendor found distinguishes *mandate revoked* from
*insufficient balance* from *PSP timeout*** — the §5.2 diagnosis gap, now confirmed empirically
across the voice layer too.

**2. The market leader has the sharpest gap of all.** Credgenics — India's largest collections
platform, with a genuine GenAI voicebot (**Swara**) and pre-due coverage — puts **all** eNACH
capability in *Billzy* (payments), not in collections or voice: mandate **registration**
campaigns and "maintain sufficient funds" nudges **[V]**. **"UPI Autopay" is never named as a
mandate rail, and no mandate-failure / bounce-management module, workflow or trigger exists.**
"Bounce" appears only as a claimed *outcome*, never as an *event they act on* **[V]**.
→ *India's biggest collections platform pre-empts the bounce and registers the mandate, but ships
nothing for the moment the mandate fails.* **A gap in the market leader's own product line is
stronger evidence than any absence-of-competitor argument.**

**3. There is a real seam between two industries.** Razorpay's own NBFC collections playbook
(4 Aug 2026) **[V/T]** recommends only *"trigger borrower reminders"* and *"offer an alternate
rail"* post-bounce — **no voice recommendation anywhere**. Neither Razorpay, Cashfree, Juspay,
Chargebee nor Zoho Billing was found to ship a voice-dunning feature.
→ **Collections vendors have voice but no mandate model; payments vendors have the mandate model
but no voice.** That seam is the position.

**4. India's default voice stack ships a tier-0 guardrail** **[P]** —
https://docs.sarvam.ai/api/cookbook/example-voice-agents/collection-agent
Sarvam's own **Collection Agent cookbook** — the reference implementation most Indian builders
will start from — is free-form LLM-driven (`on_enter()` → `generate_reply()`, no state machine),
and **its entire guardrail is two sentences of prompt text**: *"Never be aggressive, threatening,
or use inappropriate language"* and maintain *"a professional yet friendly tone."* Nothing
enforces a call window, a frequency cap, a disclosure, a distress hard-stop, or a no-threats
rule — those exist only as polite requests to a model.

Ranked constraint mechanisms actually observed in the market:

| Tier | Mechanism | Who |
|---|---|---|
| 0 | **Prompt-only** — the rule is a sentence in the system prompt; nothing enforces it | **Sarvam cookbook [P]** |
| 1 | Deterministic flow / decision tree | SquadStack [V] |
| 2 | Flow + intent inventory + LLM ("30+ flows, 20+ intents") | Credgenics Swara [V] |
| 3 | Retrieval-restricted generation (approved sources only) | Sprio AI [T] |
| 4 | Locked script + deterministic logic layer | Vodex, Teneo [V] |
| 5 | **Per-utterance real-time screening** | **Skit.ai — the only vendor describing this [V]** |

→ **The gap between tier 0 (what the default Indian stack ships) and tier 5 (state of practice)
is the most concrete engineering opportunity in this landscape.** And Skit publishes tier 5
**only on its US page** — https://skit.ai/in/ contains no RBI, no Fair Practices Code, no
08:00–19:00, no DPDP, no TRAI DLT; its entire India compliance statement is *"ISO 27001 certified
and PCI-DSS, SOC 2 compliant"* **[V]**. **An India-calibrated guardrail mechanism is genuinely
unoccupied — this survives both falsifications, because it is about mechanism, not use case.**

**5. Nobody publishes stopping rules or outcomes.** No vendor states attempt budgets or terminal
conditions — only frequency caps borrowed from conduct regulation (Caller Digital's "2–3
calls/day"). And the best-matched vendor for this exact use case, **Sprio AI, footnotes its own
numbers: *"All figures shown above are illustrative only and do not represent guaranteed
results."*** **[V]** — its "62% win-back rate" is explicitly not a result.

**6. Nobody claims intra-sentence code-mixing.** Credgenics markets "8+ languages" with
"real-time language switching during live calls" **[V]**; Skit "10+ regional Indian languages…
160+ dialect variations" **[V]**. **Zero occurrences of "Hinglish", "code-mixed" or
"code-switching" across either vendor's materials.** Whole-language switching between turns is a
materially weaker capability than intra-sentence code-mixing — and per §8.1, Sarvam is the only
vendor with a documented `codemix` mode to build on.

### 10.1 The evidence base for voice — better than the vendor blogs

⚠️ **Delete the previously-circulating figure "voice-only 18–25% vs voice+WhatsApp 38–52%"** — it
traces only to SEO blogs and must never be quoted.

What actually exists, none of it India and none of it AI voice:

- **The one voice RCT, and it is pre-due** **[P, dissertation-grade]** — Takyi, Univ. of Ghana,
  Apr 2026: n=1,002 microfinance borrowers, clean 50/50 randomisation; borrowers receiving
  **phone-call reminders before the due date were 25.55% less likely to default.** Caveats that
  must travel with it: dissertation not peer-reviewed, Ghana not India, **human** calls not AI,
  and **no SMS arm** — so it is *not* a voice-vs-text head-to-head.
- **The most strategically useful finding in the whole sweep** **[P]** — Barboni, Cárdenas &
  de Roux, *Review of Finance* (advance article, 22 Apr 2026), n=7,063 late-paying Colombian bank
  clients: messages cut the likelihood of being late by **4%**, strongest with social-norm
  framing — but *"a second experiment shows that this type of message is **ineffective in
  preventing on-time borrowers from falling into loan delinquency**."*
  → **Peer-reviewed evidence that text nudges fail in the pre-delinquency window.** That makes
  the case for a richer channel there an evidence-backed argument rather than a vendor talking
  point. It does not prove voice works — it proves the incumbent channel does not.
- Supporting SMS literature **[P]**: Cadena & Schoar (NBER w17020, Uganda) — monthly SMS reminder
  ≈ 7–9pp higher on-time payment, comparable to a 25% rate cut. Karlan, Morten & Zinman (NBER
  w17952, Philippines) — messages worked **only** when they named the account officer and **only**
  for clients that officer had served. **Personalisation/relationship is the active ingredient —
  which is exactly what voice amplifies.**

**Verdict:** there is **no published study of voice vs text in Indian collections, and none
anywhere comparing AI voice to text.** Everything India-specific and everything AI-specific is
[V]/[T]. Say so plainly — it is also the argument for running your own holdout (§2.7).

---

## 11. Build log — engineering decisions that affect claims

Running log of build facts a judge might probe. Full tracker: `docs/PROGRESS.md`.

- **1 Sep 2026 — LLM voice mode in the videopd house style.** Studied the
  production voicebot at ~/Projects/fibovoicebot (colleague's repo, patterns
  adopted with attribution). Applied: plain-text prompt skeleton +
  LANGUAGE_CONFIG (native-script rule with WRONG/RIGHT romanization pair,
  banned-bookish-word list, few-shot with behavior note) in
  `backend/services/voice/prompt_config.py`; closure-factory function tools
  with imperative doc constants and their return conventions (None = suppress
  reply, str = coaching, ToolError = corrective bounce) in
  `backend/services/voice/tools.py`. Two deliberate divergences from their
  rules: (1) our prompt MANDATES one confirmation — the promise read-back —
  because the PTP is a compliance artifact (their no-confirmation rule
  optimises flow; ours optimises evidence); (2) their end_call validator is an
  LLM judge, ours is exact flag logic because our outcomes are ground truth.
  Ratification is deterministic: `capture_promise` re-runs
  `extract_promise_date` over the customer's own last 3 utterances and rejects
  any date the customer never said — a hallucinated promise cannot reach the
  database. Worker (`backend/voice_agent.py`) now selects brain via
  VOICE_BRAIN (rule default); both brains share one screening gate at the
  tts_node boundary. Pipeline hardening adopted: prewarmed silero VAD,
  per-turn latency stamped into voice_utterances.latency_ms, user-away
  timeout. 6 new tool-safety checks in tests/test_voice_tools.py; full suite
  24 + 6 green.

- **29 Aug 2026 — diagnose→gate→score slice live.** 502 events → 502 decisions,
  10,544 gate evaluations, 281 blocked candidates, constraint violations 0.
  Every gate row carries `rule_source`; the UNVERIFIED NPCI retry caps surface
  verbatim in audit rows ("caps.yaml:retry.upi_autopay [UNVERIFIED — NPCI
  source unretrieved]") rather than being presented as settled law.
- **Annoyance cost is an explicit EV line item [ASSUMPTION].** Raw channel cost
  cannot make voice EV-negative on small tickets (₹5/call — §8.2), so customer
  irritation/churn risk is priced per channel (voice ₹250, WhatsApp ₹15…).
  This is the lever that makes voice lose on a ₹499 subscription and win on a
  ₹45,000 EMI. Labelled ASSUMPTION in caps.yaml; replace with measured churn
  effects if outcome data ever exists.
- **Success priors are hand-written basis points, deliberately not ML.**
  P(recovery | action, decline family) tables in caps.yaml; retry decays 0.75×
  per prior attempt, conversations do not. Human escalation is priced per
  family — humans out-rank the bot on hard cases (mandate revoked, disputes),
  not on routine first-attempt NSF.
- **Diagnosis is deterministic and table-driven** (10 families) — reproducible
  on stage and interrogable; an LLM may later enrich rationale text but the
  classification feeding the gates stays rules. Still ahead of market per §10.1.
- **eNACH is exempted from the ₹15k AFA-free ceiling gate** (framework scope =
  cards/PPI/UPI per §3.1); the gate's pass row on eNACH says so explicitly.
- **Holdout: 15% control via deterministic md5 hash of obligation_ref** — same
  obligation always lands in the same arm across reruns; control decisions run
  the full pipeline transparently, then withhold (§2.7 honesty requirement).
- **Adjacent internal prior art (borrowed patterns, not merged scope).** A
  colleague on this account built a Surface-A product (cart-abandonment
  recovery for Shopify/Woo — "Revenue Recovery Cloud", TS monorepo, documented
  at claude.ai/code/artifact/8c0864b2-a825-4a9a-80e5-ae57ab24f252). Different
  surface, Western channel mechanics that §0 says don't transfer — but four of
  its execution patterns align with our plan and are adopted for the execute
  slice: (1) **reserve → submit → persist** exactly-once sends keyed on an
  idempotency key (maps to `actions.idempotency_key`); (2) **conversion check
  before send** — re-check the obligation isn't already recovered at execution
  time, not just decision time (we have `subscription.charged` for this);
  (3) **render from approved facts close to send time** — no invented
  scarcity/urgency/discounts, which is also the germ of our per-utterance voice
  screen; (4) **fail-closed policy** and **never summing verified with
  influenced revenue** — both already ours independently, now cross-validated.
- **1 Sep 2026 — feature-complete build.** Execute slice: exactly-once executor
  (idempotency_key reservation; send-time conversion check), REAL Razorpay
  test-mode Payment Links in the audit trail, deterministic outcome simulator
  (organic control 8%, truth = priors×0.9 — ASSUMPTION-labelled). Voice slice:
  ONE deterministic RuleBrain drives both text simulation and the LiveKit+Sarvam
  worker (saaras:v3-realtime `codemix`, bulbul:v3 Devanagari, numbers pre-expanded
  to words); every drafted line passes the regex screen before speech and lands
  as drafted-vs-spoken in voice_utterances (the §10.4 tier-0→tier-5 close);
  Hinglish date extraction with spoken read-back (§8.6) captures PTPs. Verified
  in-browser: call → "pandrah tarikh" → read-back → PTP → simulate → KEPT;
  dashboard shows ₹7.3L recovered, ₹2.5L incremental (19.2% vs 11.7% control),
  PTP-kept 100%, violations 0. API shapes primary-sourced from official SDK
  wheels (scratchpad/voice-api-shapes.md) — docs.sarvam.ai blocks fetch.
- **MySQL** (team choice): session tz pinned +00:00 at connect (kills the
  session-timezone compliance footgun), utf8mb4 for Devanagari verbatim,
  one-active-mandate invariant enforced in app code (no partial indexes).

---

## Sources

Payment recovery / retries:
- [Stripe — Revenue recovery (docs)](https://docs.stripe.com/billing/revenue-recovery)
- [Stripe — Automate payment retries / Smart Retries (docs)](https://docs.stripe.com/billing/revenue-recovery/smart-retries)
- [Stripe — How we built it: Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries)
- [Stripe — AI enhancements to Adaptive Acceptance](https://stripe.com/blog/ai-enhancements-to-adaptive-acceptance)
- [Stripe — Payment blocked due to excessive retries](https://support.stripe.com/questions/payment-blocked-due-to-excessive-retries)
- [Adyen — Announcing RevenueAccelerate](https://www.adyen.com/knowledge-hub/announcing-adyen-revenueaccelerate)
- [Slicker — Visa and Mastercard payment retry rules](https://www.slickerhq.com/resources/blog/visa-mastercard-payment-retry-rules)
- [Congrify — Card scheme penalty programmes](https://congrify.com/what-are-card-scheme-penalty-programs-and-why-should-you-care/)
- [Churnkey — Stripe Smart Retries FAQs](https://churnkey.co/blog/stripe-smart-retries)
- [Redux — Stripe Smart Retries explained](https://www.reduxpayments.com/blog/stripe-smart-retries-explained)
- [FlyCode — Top payment recovery platforms](https://www.flycode.com/blog/top-payment-recovery-platforms-2026-comparison-chart-success-rate-stats)
- [Butter Payments — Involuntary churn guide](https://www.butterpayments.com/guides/disputes-chargebacks-guides/involuntary-churn/)
- [Churn Buster — Best dunning management software](https://churnbuster.io/articles/best-dunning-management-software/)
- [Recurly — Churn rate benchmarks](https://recurly.com/research/churn-rate-benchmarks/)

Razorpay / India rails:
- [Razorpay — Subscription payment retries (docs)](https://razorpay.com/docs/payments/subscriptions/payment-retries/)
- [Razorpay — Subscriptions webhook events (docs)](https://razorpay.com/docs/webhooks/subscriptions/)
- [Razorpay — About webhooks (docs)](https://razorpay.com/docs/webhooks/)
- [Razorpay — Optimizer](https://razorpay.com/optimizer-intelligent-payments-routing/)
- [Razorpay — Optimizer AI/ML routing blog](https://razorpay.com/blog/boost-payments-success-rates-with-optimizers-ai-ml-routing/)
- [Razorpay — Payment success rate optimisation India](https://razorpay.com/blog/payment-success-rate-optimization-india/)
- [Razorpay — Abandoned cart recovery with Magic Checkout](https://razorpay.com/blog/abandoned-cart-recovery-solution/)
- [Razorpay — Magic Checkout](https://razorpay.com/magic-checkout/)
- [Razorpay — UPI Intent (docs)](https://razorpay.com/docs/payments/payment-methods/upi/upi-intent/)
- [Hyperswitch — Revenue Recovery](https://hyperswitch.io/revenue-recovery)
- [Hyperswitch — Smart Retries (docs)](https://docs.hyperswitch.io/integration-guide/workflows/smart-retries)
- [Stripe — India recurring payments (docs)](https://stripe.com/docs/india-recurring-payments)

Regulation:
- [Conventus Law — RBI Digital Payments E-Mandate Framework, 2026](https://conventuslaw.com/report/rbis-digital-payments-e-mandate-framework-2026-consolidated-directions-for-recurring-digital-transactions/)
- [LexOrbis — RBI E-Mandate Framework 2026](https://www.lexorbis.com/rbis-digital-payments-e-mandate-framework-2026-consolidated-directions-for-recurring-digital-transactions/)
- [AMLEGALS — UPI Autopay & recurring payments compliance checklist](https://amlegals.com/upi-autopay-and-recurring-payments-compliance-checklist-under-rbis-e-mandate-framework-2026/)
- [TaxGuru — RBI consolidated e-mandate directions](https://taxguru.in/rbi/rbi-issues-consolidated-directions-digital-payments-e-mandate-framework-2026.html)
- [Freed — RBI guidelines on recovery agents](https://freed.care/blog/rbi-guidelines-recovery-agents)
- [CredSettle — RBI recovery agent calling hours](https://www.credsettle.com/rbi-guidelines-calling-after-7pm)
- [SCC Online — WhatsApp chatbot opt-in consent & DPDP](https://www.scconline.com/blog/post/2026/07/29/whatsapp-chatbot-opt-in-consent-dpdp-act-compliance/)
- [Message Central — India SMS regulations, DLT & TRAI compliance](https://www.messagecentral.com/sms-guideline/india)
- [Leegality — DPDP Act impact on telemarketing](https://www.leegality.com/consent-blog/dpdp-telemarketing-regulations)
- [IndiaFilings — Section 43B(h) MSME 45-day payment rule](https://www.indiafilings.com/learn/section-43bh-new-msme-45-days-payment-rule)
- [Compliance Calendar — Section 43B(h)](https://www.compliancecalendar.in/learn/section-43b-h-msme-45-days-payment-rule)

Checkout abandonment / AR / collections AI / measurement:
- [Baymard — Cart abandonment rate statistics](https://baymard.com/lists/cart-abandonment-rate)
- [HighRadius — Top accounts receivable tools](https://www.highradius.com/en-gb/Blog/top-accounts-receivable-tools/)
- [Growfin — Guide to AR automation](https://www.growfin.ai/blog/guide-to-ar-automation-technologies-benefits-and-best-practices)
- [Upflow — Best accounts receivable software](https://upflow.io/software/best-accounts-receivable-software)
- [Chargebee — Subscription management](https://www.chargebee.com/billing/manage-subscriptions/)
- [Domu — Best AI platform for debt collection automation](https://domu.ai/blog/best-ai-platform-for-debt-collection-automation-voice-email-and-sms-comparison-2026)
- [Amplitude — Incrementality testing](https://amplitude.com/explore/experiment/incrementality-testing)
- [Triple Whale — Incrementality testing methods](https://www.triplewhale.com/blog/incrementality-testing-methods)
- [CRM Knowledge Base — Holdouts and control groups](https://crmknowledgebase.com/measurement/holdouts-and-control-groups)

Hinglish voice stack (added 27 Aug 2026):
- [Sarvam — Models](https://docs.sarvam.ai/api/getting-started/models)
- [Sarvam — Realtime streaming STT](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming.md)
- [Sarvam — STT overview (code-mixing)](https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/speech-to-text/overview)
- [Sarvam — Bulbul TTS (script requirement)](https://docs.sarvam.ai/api-reference-docs/models/bulbul)
- [Sarvam — Which TTS API to use](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/which-api-to-use)
- [Sarvam — Voice Agents overview](https://docs.sarvam.ai/conversations/overview)
- [Sarvam — Pricing](https://docs.sarvam.ai/api/getting-started/pricing)
- [Sarvam — Changelog](https://docs.sarvam.ai/api-reference-docs/changelog)
- [Bhashini — API docs](https://bhashini.gitbook.io/bhashini-apis)
- [ElevenLabs — Models](https://elevenlabs.io/docs/overview/models)
- [ElevenLabs — Latency concepts](https://elevenlabs.io/docs/eleven-api/concepts/latency)
- [ElevenLabs — Normalization / prompting](https://elevenlabs.io/docs/best-practices/prompting/normalization)
- [Diwan et al. — MUCS 2021 code-switching ASR challenge](https://arxiv.org/abs/2104.00235)
- [Stivers et al. — Universals in turn-taking, PNAS 2009](https://www.pnas.org/doi/10.1073/pnas.0903616106)
- [LiveKit — Voice pipelines](https://docs.livekit.io/agents/models/pipelines/)
- [LiveKit — Sequential pipeline architecture](https://livekit.com/blog/sequential-pipeline-architecture-voice-agents)
- [Skit.ai — Responsible Voice AI for debt collection](https://skit.ai/responsible-voice-ai-for-debt-collection/)
- [skit-ai/dialogy — SLU toolkit](https://github.com/skit-ai/dialogy)
- [Teneo.ai — Why AI debt collection requires enterprise control](https://www.teneo.ai/blog/why-ai-debt-collection-requires-enterprise-control)
- [SquadStack — AI voicebot for debt collection](https://www.squadstack.ai/voicebot/ai-voicebot-for-debt-collection)
- [Caller Digital — EMI payment reminders](https://caller.digital/use-cases/emi-payment-reminders)
- [Caller Digital — AI voice agent India (UPI mandate in-call)](https://caller.digital/ai-voice-agent-india)
- [Sprio AI — D2C subscription recovery](https://www.sprio.ai/d2c)
- [Credgenics — Swara voicebot](https://www.credgenics.com/swara-voicebot-for-debt-collection)
- [Credgenics — API docs](https://docs.credgenics.com/)
- [Credgenics — 7 collections platform KPIs](https://blog.credgenics.com/7-debt-collections-platform-metrics-kpis/)
- [Gnani.ai — Collections](https://gnani.ai/solutions/collections/)
- [Vodex — Debt collection](https://vodex.ai/debt-collection/)
- [Skit.ai — India page](https://skit.ai/in/)
- [Sarvam — Collection Agent cookbook (tier-0 guardrail)](https://docs.sarvam.ai/api/cookbook/example-voice-agents/collection-agent)
- [Razorpay — eNACH & UPI Autopay collections playbook 2026](https://razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026)

Voice compliance (India + comparative):
- [RBI/2022-23/108 — Recovery agents (12 Aug 2022)](https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12378&Mode=0)
- [RBI/2007-2008/296 — Recovery agents engaged by banks, call recording (24 Apr 2008)](https://www.rbi.org.in/scripts/NotificationUser.aspx?Id=4141&Mode=0)
- [TRAI — TCCCPR 2018 and Second Amendment 2025](https://www.trai.gov.in/)
- [PIB — DPDP Rules 2025 notification](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2190014)
- [EU AI Act — Regulation (EU) 2024/1689, Art. 50](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)
- [US Reg F — 12 CFR part 1006](https://www.ecfr.gov/current/title-12/part-1006)
- [FCC 24-17 — AI voices are "artificial or prerecorded" under TCPA](https://docs.fcc.gov/public/attachments/FCC-24-17A1.pdf)

Voice/reminder effectiveness (peer-reviewed & primary):
- [Barboni, Cárdenas & de Roux — Behavioral messages and debt repayment, Review of Finance 2026](https://doi.org/10.1093/rof/rfag015)
- [Takyi — Phone call reminders and loan default, Univ. of Ghana 2026](https://www.ug.edu.gh/sites/default/files/2026-04/ABSTRACT%20-%20PRINCE%20BAAH%20TAKYI.pdf)
- [Cadena & Schoar — Remembering to Pay?, NBER w17020](https://www.nber.org/papers/w17020)
- [Karlan, Morten & Zinman — A Personal Touch, NBER w17952](https://www.nber.org/system/files/working_papers/w17952/w17952.pdf)

---

# CORRECTIONS (verified pass, 26 Aug 2026)

A second pass against primary sources corrected three claims made above. **The
published page is now the accurate copy** — https://claude.ai/code/artifact/bad9fced-d653-478a-adc4-87104dd7d870

1. **Mastercard "1/day, ~10 per 30 days" — WRONG, do not cite.** Not found in any
   scheme or PSP source. What is real: MAC 03 = do not try again, MAC 21 = payment
   cancellation, and Mastercard charges a fee for any retry within 30 days of either.
2. **Visa "15 in 30 days" is category-conditional, not flat.** Visa sorts response
   codes into four categories (Apr 2020, updated 17 Apr 2021). Category 1 = never
   reattempt; Category 2 = up to 15 in 30 days. Response Code 14 must never be
   reattempted on the same account number. The bulletin is *not* scoped to CNP, and
   states no fee amount — the $0.10 figure is a processor schedule, not a scheme rule.
   Primary: usa.visa.com/dam/VCOM/global/support-legal/documents/updates-to-rules-for-declined-transaction-resubmission-and-use-of-authorization-response-codes.pdf
3. **RBI contact hours cover CALLS, not all channels.** RBI/2022-23/108 (12 Aug 2022,
   still in force) prohibits "calling the borrower before 8:00 a.m. and after 7:00 p.m."
   SMS/WhatsApp fall under a separate *time-agnostic* bar on "inappropriate messages";
   email only under the general harassment clause. Hold all channels to 08:00–19:00 as
   a design default, but know the quotable restriction is voice-only.

## Upgraded to primary-sourced

- **E-Mandate Framework 2026 = RBI/DPSS/2026-27/396**, 21 Apr 2026, effective
  immediately, repeals+consolidates 8 circulars Aug 2019–Aug 2024. Thresholds confirmed.
  Pre-transaction notice ≥24h naming merchant, amount, debit date, mandate reference,
  plus a separate AFA-validated opt-out per transaction.
  **The Framework is SILENT on retry / re-presentation** (three full-text reads). The
  retry constraint is not RBI's — it lives in NPCI/scheme rules or your PA contract.
- **Card e-mandate debits sit in `processing` for ~26h and cannot be cancelled** →
  minimum practical retry interval on Indian card SI is ~1/day.
- **Razorpay subscription ladder: fail day 0 → `pending`, retries days 1, 2, 3 → `halted`.**
  Card change while pending auto-charges the last invoice. Update Subscription API is
  closed in created/pending/halted — exactly the states that matter.
- **Razorpay ships an official MCP server** (github.com/razorpay/razorpay-mcp-server,
  https://mcp.razorpay.com/mcp, 40+ tools). No subscription tools, no create_payout.

## Still open — do not guess

- **DPDP s.4/s.6/s.7 verbatim text was never retrieved** (MeitY blocked, India Code
  403/404, Gazette PDF unextractable). Working understanding: no GDPR-style
  contract-performance or legitimate-interests ground; consent is purpose-confined by
  the notice, so servicing reminders need no fresh consent *if* servicing was a stated
  purpose at collection. Verify before it becomes compliance copy.
- **All NACH/eNACH return codes, re-presentation limits, and the UPI AutoPay retry cap.**
  npci.org.in returns HTTP 403 to programmatic fetch. Pull manually: NACH-006-FY-24-25,
  Circulars 240 / 274 / 011 MMS, NACH Procedural Guidelines V.6, UPI OC 223 FY 2025-26.
  Circular 016 FY2019-20 indicates NPCI **penalises specific return reasons** — some
  retries cost money per attempt.
  The widely repeated UPI AutoPay "1 original + 3 retries" traces only to a Razorpay
  marketing blog. Do not hardcode it.

## Still open — voice (added 27 Aug 2026)

- **Any published latency figure from Sarvam.** Bulbul and Saaras docs contain zero. The
  relative claims ("lowest on a warm connection") are ordinal only. **Measure it yourself on
  day one** — this is a half-day of work and gates a real architecture decision.
- **Sarvam `codemix` mode accuracy.** The mode is documented; no WER, no benchmark, no eval is
  published for it. Its existence is a feature claim, not evidence of quality.
- **Sarvam realtime-streaming STT pricing** and **Voice Agents per-minute platform pricing.**
  Only the flat ₹30/hour STT rate is public; realtime and the managed platform are sales-gated.
- **Saaras v4 vs v3 accuracy delta.** Changelog announces v4; no comparative benchmark published.
- **Numeral/date/amount extraction benchmarks for code-switched Hindi-English speech.** None
  exist (§8.6). This is the highest-risk unmeasured component of a voice build.
- **ElevenLabs Hinglish / romanized-input behaviour**, and any Hindi-specific quality data.
  Undocumented — neither confirmed good nor bad.
- **Bhashini production latency, reliability, and pricing.** No benchmark, no SLA, no rate card.
  bhashini.gov.in is JS-rendered and returns nothing to programmatic fetch.
- **The "200ms / 700ms" turn-taking thresholds** as stated in the primary PNAS paper — the
  cross-language mean gap is widely cited as ~200ms but was not verified in the source text.
- ~~**Whether Indian law requires disclosing that a caller is an AI.**~~ **RESOLVED 28 Aug 2026
  — no such duty exists (§9.1).** Disclose anyway, as a voluntary default; deception is the
  actionable risk, not non-disclosure.
- **DPDP s.7 sub-clause** relied on for collections recording as a "legitimate use" without
  consent. The structural argument is sound (§9.4) but the bare Act text was never retrieved —
  MeitY blocked, India Code 403/404. Confirm before it becomes compliance copy.
- **RBI FREE-AI Committee report original.** Only a KPMG summary was read. Pull from rbi.org.in
  before quoting anything as RBI's own words.
- **RBI Digital Lending Directions 2025 pre-contact agent-notification duty.** Law-firm sourced
  only; RBI original not retrieved.
- **MeitY IT Amendment Rules 2026 verbatim text.** Scope is primary-sourced; the quoted SGI
  definition came via commentary because the MeitY PDF host returned HTTP 403.
- **TCCCPR time-band roman-numeral mapping.** The default-permitted 10:00–21:00 window is
  inferred from row order in the PDF text layer. Verify against the Gazette print before quoting
  the 10:00 figure externally. (Not load-bearing — §9.5 establishes RBI's window binds instead.)
- **Whether recovery/payment-reminder calls are legally a "Service Voice Call"** under TCCCPR
  reg. 2(bh)(i). Defensible reading, industry practice, **untested by TRAI adjudication** (§9.5).
- **The NACH return-code → action mapping** that circulated in a search summary (01→retry,
  05→collections, 27→mandate re-registration, …). **Direct fetch of the cited Razorpay article
  shows no such table and no return codes.** The mapping appears to originate from an unrelated
  vendor page and is unverified. Do not cite it.
- **What "training and certification" means for a software agent.** RBI asked IBA/IIBF to build a
  100-hour certificate course for human recovery agents. No regulator has addressed the software
  case. Unpriced risk, not a solved problem.
- **Whether Razorpay test-mode UPI intent links are payable from a real UPI app.** Gates the
  live-demo moment. Test-mode payments may complete only through a simulated checkout rather
  than live NPCI rails. Verify before building a demo beat around it.
