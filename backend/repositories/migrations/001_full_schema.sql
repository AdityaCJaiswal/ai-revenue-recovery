-- 001_full_schema.sql
-- Full table map per docs/DATA_MODEL.md. Conventions:
--   * money = BIGINT paise, never FLOAT/DECIMAL
--   * all DATETIME values are UTC (session tz pinned to +00:00 at connect)
--   * enums = VARCHAR + CHECK, portable and non-rewriting to extend
--   * facts (events, decisions, gates, consents) are append-only
--   * entity ids = app-generated VARCHAR(40); log rows = BIGINT AUTO_INCREMENT

-- ---------------------------------------------------------------- customers
CREATE TABLE IF NOT EXISTS customers (
    id                 VARCHAR(40)  PRIMARY KEY,
    external_ref       VARCHAR(255) NOT NULL UNIQUE,
    phone_e164         VARCHAR(20)  NULL,
    email              VARCHAR(255) NULL,
    preferred_language VARCHAR(16)  NOT NULL DEFAULT 'hi-IN',
    -- The RBI 08:00-19:00 contact window is borrower-LOCAL. Stored per
    -- customer so the gate converts explicitly, never via session tz.
    timezone           VARCHAR(64)  NOT NULL DEFAULT 'Asia/Kolkata',
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    -- DPDP erasure = redact-in-place, never DELETE: retention laws (RBI 2008
    -- recording mandate, PMLA/KYC) outrank erasure. RESEARCH.md 9.4.
    redacted_at        DATETIME(6)  NULL,
    redaction_reason   VARCHAR(128) NULL
) ENGINE=InnoDB;

-- -------------------------------------------------------------- obligations
CREATE TABLE IF NOT EXISTS obligations (
    id                   VARCHAR(40)  PRIMARY KEY,
    customer_id          VARCHAR(40)  NOT NULL,
    external_ref         VARCHAR(255) NOT NULL UNIQUE,
    kind                 VARCHAR(16)  NOT NULL CHECK (kind IN ('subscription','emi','invoice')),
    amount_paise         BIGINT       NOT NULL,
    currency             CHAR(3)      NOT NULL DEFAULT 'INR',
    status               VARCHAR(16)  NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','past_due','recovering','recovered','written_off','cancelled')),
    due_at               DATETIME(6)  NULL,
    first_failed_at      DATETIME(6)  NULL,
    resolved_at          DATETIME(6)  NULL,
    tenure_days          INT          NULL,
    lifetime_value_paise BIGINT       NULL,
    created_at           DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at           DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_obligations_customer FOREIGN KEY (customer_id) REFERENCES customers(id),
    KEY idx_obligations_customer (customer_id),
    KEY idx_obligations_status (status)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------- mandates
-- The differentiator table: RESEARCH.md 10.1 -- no vendor surveyed models
-- e-mandate state, which is why none can distinguish mandate_revoked from
-- insufficient_balance from psp_timeout.
CREATE TABLE IF NOT EXISTS mandates (
    id                          VARCHAR(40)  PRIMARY KEY,
    obligation_id               VARCHAR(40)  NOT NULL,
    external_ref                VARCHAR(255) NULL UNIQUE,   -- UMN / mandate reference
    rail                        VARCHAR(16)  NOT NULL CHECK (rail IN ('card_si','upi_autopay','enach')),
    status                      VARCHAR(16)  NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','active','paused','revoked','expired')),
    -- AFA-free ceiling: Rs 15,000 (Rs 1,00,000 specified categories).
    -- RBI/DPSS/2026-27/396. Amount above cap => customer-present flow only.
    max_amount_paise            BIGINT       NOT NULL,
    registered_at               DATETIME(6)  NULL,
    expires_at                  DATETIME(6)  NULL,
    next_debit_at               DATETIME(6)  NULL,
    -- Pre-debit notice >= 24h before each debit is MANDATORY (RBI 2026), and
    -- the customer may veto that specific debit. Both are gate inputs.
    pre_debit_notice_sent_at    DATETIME(6)  NULL,
    pre_debit_notice_opt_out_at DATETIME(6)  NULL,
    -- Card SI debits sit in `processing` ~26h and cannot be cancelled =>
    -- min practical retry interval ~1/day. RESEARCH.md CORRECTIONS.
    processing_lock_until       DATETIME(6)  NULL,
    revoked_at                  DATETIME(6)  NULL,
    created_at                  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_mandates_obligation FOREIGN KEY (obligation_id) REFERENCES obligations(id),
    KEY idx_mandates_obligation (obligation_id),
    KEY idx_mandates_status (status)
    -- INVARIANT (app-enforced): at most one status='active' mandate per
    -- obligation. MySQL has no partial unique index; the mandate service must
    -- check-then-insert inside a transaction.
) ENGINE=InnoDB;

-- ------------------------------------------------------------- caps_versions
-- Content-hashed snapshots of caps.yaml. decisions.caps_version answers
-- "what rules were in force when you decided this?" -- without it, editing
-- caps.yaml silently rewrites history.
CREATE TABLE IF NOT EXISTS caps_versions (
    version_hash CHAR(64)    PRIMARY KEY,   -- sha256 of canonical yaml
    content      JSON        NOT NULL,
    created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

-- ------------------------------------------------------------------- events
-- Canonical RecoveryEvent stream. Append-only. obligation_ref/customer_ref are
-- deliberately strings, not FKs: events arrive before obligations exist; the
-- obligations upsert reconciles later.
CREATE TABLE IF NOT EXISTS events (
    event_id         VARCHAR(255) PRIMARY KEY,      -- idempotency: redelivery collapses here
    seq              BIGINT       NOT NULL AUTO_INCREMENT,
    source           VARCHAR(16)  NOT NULL CHECK (source IN ('razorpay','synthetic')),
    event_type       VARCHAR(64)  NOT NULL,
    occurred_at      DATETIME(6)  NOT NULL,
    customer_ref     VARCHAR(255) NOT NULL,
    obligation_ref   VARCHAR(255) NOT NULL,
    amount_paise     BIGINT       NOT NULL,
    currency         CHAR(3)      NOT NULL DEFAULT 'INR',
    rail             VARCHAR(24)  NULL,
    decline_family   VARCHAR(40)  NOT NULL,
    decline_code_raw VARCHAR(255) NULL,
    attempt_number   INT          NOT NULL DEFAULT 1,
    raw              JSON         NOT NULL,
    received_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_events_seq (seq),
    KEY idx_events_obligation (obligation_ref),
    KEY idx_events_received (received_at DESC)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------- decisions
-- THE PRODUCT. One row per decision point, append-only. Candidates and gate
-- checks are normalised into their own tables (below), not JSON blobs.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id         VARCHAR(40)  PRIMARY KEY,
    event_id            VARCHAR(255) NOT NULL,
    obligation_ref      VARCHAR(255) NOT NULL,
    strategy            VARCHAR(16)  NOT NULL CHECK (strategy IN ('baseline','agentic')),
    -- holdout arm stamped at decision time so the incrementality query never
    -- has to reconstruct it
    arm                 VARCHAR(16)  NULL CHECK (arm IN ('treatment','control')),
    diagnosis_family    VARCHAR(40)  NULL,
    diagnosis_rationale TEXT         NULL,
    caps_version        CHAR(64)     NULL,
    -- NULL chosen_action = the agent deliberately did nothing (that is a
    -- decision too, and it appears in the drawer as one)
    chosen_action       VARCHAR(64)  NULL,
    scheduled_for       DATETIME(6)  NULL,
    decided_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    outcome             VARCHAR(32)  NULL,
    outcome_at          DATETIME(6)  NULL,
    CONSTRAINT fk_decisions_event FOREIGN KEY (event_id) REFERENCES events(event_id),
    CONSTRAINT fk_decisions_caps  FOREIGN KEY (caps_version) REFERENCES caps_versions(version_hash),
    KEY idx_decisions_obligation (obligation_ref),
    KEY idx_decisions_decided (decided_at DESC)
) ENGINE=InnoDB;

-- ------------------------------------------------------- decision_candidates
-- One row per action CONSIDERED, blocked or not. The blocked rows are the
-- blocked-actions ledger -- the demo of restraint.
CREATE TABLE IF NOT EXISTS decision_candidates (
    id                   BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    decision_id          VARCHAR(40) NOT NULL,
    action_type          VARCHAR(64) NOT NULL,
    channel              VARCHAR(24) NULL,
    expected_value_paise BIGINT      NOT NULL,
    cost_paise           BIGINT      NOT NULL DEFAULT 0,
    success_prob_bp      INT         NULL,      -- basis points, 0-10000; ints only
    rank_order           INT         NOT NULL,
    blocked              BOOLEAN     NOT NULL DEFAULT FALSE,
    blocked_by_gate      VARCHAR(64) NULL,
    CONSTRAINT fk_candidates_decision FOREIGN KEY (decision_id) REFERENCES decisions(decision_id),
    KEY idx_candidates_decision (decision_id)
) ENGINE=InnoDB;

-- --------------------------------------------------------- gate_evaluations
-- The compliance audit artefact: one row per constraint checked per candidate.
-- rule_source turns a log into a justification (RESEARCH.md 5.6) and keeps
-- UNVERIFIED caps visible in the audit trail.
CREATE TABLE IF NOT EXISTS gate_evaluations (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    decision_id  VARCHAR(40)  NOT NULL,
    candidate_id BIGINT       NULL,     -- NULL = decision-level gate
    gate_name    VARCHAR(64)  NOT NULL,
    passed       BOOLEAN      NOT NULL,
    detail       VARCHAR(512) NULL,
    -- e.g. 'RBI/2022-23/108', 'caps.yaml:retry.upi_autopay [UNVERIFIED]'
    rule_source  VARCHAR(128) NOT NULL,
    evaluated_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_gates_decision  FOREIGN KEY (decision_id)  REFERENCES decisions(decision_id),
    CONSTRAINT fk_gates_candidate FOREIGN KEY (candidate_id) REFERENCES decision_candidates(id),
    KEY idx_gates_decision (decision_id)
) ENGINE=InnoDB;

-- ------------------------------------------------------------- dlt_templates
-- TRAI/DLT-registered templates. The agent NEVER improvises copy on a DLT
-- channel -- templates are pre-approved artefacts, and mixing promotional
-- content reclassifies the whole message (TCCCPR reg. 2(av)). RESEARCH.md 9.5.
CREATE TABLE IF NOT EXISTS dlt_templates (
    template_id   VARCHAR(64)  PRIMARY KEY,
    header        VARCHAR(16)  NOT NULL,
    category      VARCHAR(16)  NOT NULL CHECK (category IN ('utility','service','transactional','promotional')),
    body          TEXT         NOT NULL,
    language      VARCHAR(16)  NOT NULL DEFAULT 'en',
    active        BOOLEAN      NOT NULL DEFAULT TRUE,
    registered_at DATE         NULL,
    created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

-- ------------------------------------------------------------------- actions
CREATE TABLE IF NOT EXISTS actions (
    id              VARCHAR(40)  PRIMARY KEY,
    decision_id     VARCHAR(40)  NOT NULL,
    candidate_id    BIGINT       NULL,
    obligation_ref  VARCHAR(255) NOT NULL,
    action_type     VARCHAR(64)  NOT NULL,
    channel         VARCHAR(24)  NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','executing','executed','failed','cancelled')),
    scheduled_for   DATETIME(6)  NULL,
    executed_at     DATETIME(6)  NULL,
    cost_paise      BIGINT       NOT NULL DEFAULT 0,
    provider_ref    VARCHAR(255) NULL,
    dlt_template_id VARCHAR(64)  NULL,
    -- retries of the executor must not double-send a WhatsApp or double-fire
    -- a debit: idempotency lives in the schema
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_actions_decision  FOREIGN KEY (decision_id)     REFERENCES decisions(decision_id),
    CONSTRAINT fk_actions_candidate FOREIGN KEY (candidate_id)    REFERENCES decision_candidates(id),
    CONSTRAINT fk_actions_template  FOREIGN KEY (dlt_template_id) REFERENCES dlt_templates(template_id),
    KEY idx_actions_obligation (obligation_ref),
    KEY idx_actions_status_sched (status, scheduled_for)
) ENGINE=InnoDB;

-- --------------------------------------------------------- payment_attempts
CREATE TABLE IF NOT EXISTS payment_attempts (
    id                   BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    obligation_id        VARCHAR(40)  NOT NULL,
    mandate_id           VARCHAR(40)  NULL,
    decision_id          VARCHAR(40)  NULL,    -- which decision triggered this retry
    attempt_number       INT          NOT NULL,
    rail                 VARCHAR(24)  NOT NULL,
    amount_paise         BIGINT       NOT NULL,
    status               VARCHAR(16)  NOT NULL DEFAULT 'initiated'
                         CHECK (status IN ('initiated','processing','succeeded','failed')),
    decline_family       VARCHAR(40)  NULL,
    decline_code_raw     VARCHAR(255) NULL,
    -- Mastercard MAC 03/21 = machine-readable STOP. Retrying past it is a
    -- scheme-fee event, not a strategy.
    merchant_advice_code VARCHAR(4)   NULL,
    gateway_ref          VARCHAR(255) NULL UNIQUE,
    initiated_at         DATETIME(6)  NOT NULL,
    settled_at           DATETIME(6)  NULL,
    created_at           DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_attempts_obligation FOREIGN KEY (obligation_id) REFERENCES obligations(id),
    CONSTRAINT fk_attempts_mandate    FOREIGN KEY (mandate_id)    REFERENCES mandates(id),
    CONSTRAINT fk_attempts_decision   FOREIGN KEY (decision_id)   REFERENCES decisions(decision_id),
    -- the 30-day rolling attempt-budget gate reads this
    KEY idx_attempts_obligation_time (obligation_id, initiated_at)
) ENGINE=InnoDB;

-- ------------------------------------------------------ holdout_assignments
-- Randomised holdout: the only honest way to claim INCREMENTAL recovery
-- (RESEARCH.md 2.7). PK = obligation_id: an obligation can never switch arms.
CREATE TABLE IF NOT EXISTS holdout_assignments (
    obligation_id VARCHAR(40) PRIMARY KEY,
    arm           VARCHAR(16) NOT NULL CHECK (arm IN ('treatment','control')),
    stratum       VARCHAR(64) NULL,     -- amount-band x decline-family
    batch_id      VARCHAR(64) NOT NULL,
    assigned_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_holdout_obligation FOREIGN KEY (obligation_id) REFERENCES obligations(id)
) ENGINE=InnoDB;

-- --------------------------------------------------------------- recoveries
CREATE TABLE IF NOT EXISTS recoveries (
    id                   BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    obligation_id        VARCHAR(40) NOT NULL,
    amount_paise         BIGINT      NOT NULL,
    rail                 VARCHAR(24) NULL,
    recovered_at         DATETIME(6) NOT NULL,
    -- attribution is honest-labelled: "preceded by", not "caused by".
    -- causality comes from the holdout, never from attribution.
    attributed_action_id VARCHAR(40) NULL,
    days_to_cash         INT         NULL,
    created_at           DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_recoveries_obligation FOREIGN KEY (obligation_id) REFERENCES obligations(id),
    CONSTRAINT fk_recoveries_action     FOREIGN KEY (attributed_action_id) REFERENCES actions(id),
    KEY idx_recoveries_obligation (obligation_id)
) ENGINE=InnoDB;

-- -------------------------------------------------------------- voice_calls
-- RBI 2008 (DBOD.Leg.BC.75) makes recording MANDATORY for bank collections,
-- bidirectional. A voice product that does not record 100% cannot be sold
-- into a bank. RESEARCH.md 9.3.
CREATE TABLE IF NOT EXISTS voice_calls (
    id                        VARCHAR(40)  PRIMARY KEY,
    action_id                 VARCHAR(40)  NOT NULL,
    started_at                DATETIME(6)  NULL,
    ended_at                  DATETIME(6)  NULL,
    duration_seconds          INT          NULL,
    disposition               VARCHAR(32)  NULL,   -- answered/no_answer/busy/ptp/paid/handoff...
    -- Voluntary AI disclosure (no Indian duty exists -- RESEARCH.md 9.1 -- but
    -- deception IS actionable, so we disclose and timestamp the proof)
    ai_disclosure_given_at    DATETIME(6)  NULL,
    recording_uri             VARCHAR(512) NULL,
    recording_retention_until DATE         NULL,
    transcript_uri            VARCHAR(512) NULL,
    language_detected         VARCHAR(16)  NULL,
    asr_model                 VARCHAR(64)  NULL,   -- e.g. saaras:v3-realtime
    tts_model                 VARCHAR(64)  NULL,   -- e.g. bulbul:v3
    distress_detected         BOOLEAN      NOT NULL DEFAULT FALSE,
    dispute_raised            BOOLEAN      NOT NULL DEFAULT FALSE,
    handoff_to_human_at       DATETIME(6)  NULL,
    created_at                DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_calls_action FOREIGN KEY (action_id) REFERENCES actions(id)
) ENGINE=InnoDB;

-- --------------------------------------------------------- voice_utterances
-- Per-utterance screening log: drafted_text = what the LLM wanted to say,
-- spoken_text = what the gate allowed (NULL if blocked). The tier-0 -> tier-5
-- gap from RESEARCH.md 10.4, as a queryable table.
CREATE TABLE IF NOT EXISTS voice_utterances (
    id                BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    voice_call_id     VARCHAR(40)  NOT NULL,
    turn_index        INT          NOT NULL,
    speaker           VARCHAR(8)   NOT NULL CHECK (speaker IN ('agent','customer')),
    drafted_text      TEXT         NULL,   -- agent turns only
    spoken_text       TEXT         NULL,   -- NULL on a blocked agent turn
    screening_verdict VARCHAR(16)  NULL CHECK (screening_verdict IN ('allowed','modified','blocked')),
    blocked_reason    VARCHAR(128) NULL,
    latency_ms        INT          NULL,
    created_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_utterances_call FOREIGN KEY (voice_call_id) REFERENCES voice_calls(id),
    KEY idx_utterances_call (voice_call_id, turn_index)
) ENGINE=InnoDB;

-- ---------------------------------------------------------- promises_to_pay
-- First-class PTP (RESEARCH.md 7). PTP-KEPT-rate is the metric no vendor
-- publishes (8.8) -- kept/broken status is what makes it computable here.
CREATE TABLE IF NOT EXISTS promises_to_pay (
    id                       VARCHAR(40) PRIMARY KEY,
    obligation_id            VARCHAR(40) NOT NULL,
    voice_call_id            VARCHAR(40) NULL,
    amount_paise             BIGINT      NOT NULL,
    promised_for_date        DATE        NOT NULL,
    -- the customer's own words, code-mixed, utf8mb4: "salary 3 tareekh ko aa
    -- rahi hai". Kept verbatim as the evidence behind the extraction.
    verbatim                 TEXT        NULL,
    extraction_confidence_bp INT         NULL,
    -- "pandrah tarikh, yaani 15 tarikh ko -- sahi hai?" Read-back is both the
    -- correctness check and the compliance artefact (RESEARCH.md 8.6).
    readback_confirmed       BOOLEAN     NOT NULL DEFAULT FALSE,
    status                   VARCHAR(16) NOT NULL DEFAULT 'open'
                             CHECK (status IN ('open','kept','broken','partial')),
    kept_amount_paise        BIGINT      NULL,
    kept_at                  DATETIME(6) NULL,
    created_at               DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_ptp_obligation FOREIGN KEY (obligation_id) REFERENCES obligations(id),
    CONSTRAINT fk_ptp_call       FOREIGN KEY (voice_call_id) REFERENCES voice_calls(id),
    KEY idx_ptp_obligation (obligation_id),
    KEY idx_ptp_due (status, promised_for_date)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------- consents
-- DPDP purpose-scoped consent, append-only: current state = latest row.
-- basis matters commercially: recording runs on legitimate_use/legal_obligation,
-- NOT consent -- a borrower cannot withdraw-consent their way out of the RBI
-- recording mandate. RESEARCH.md 9.4.
CREATE TABLE IF NOT EXISTS consents (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id  VARCHAR(40)  NOT NULL,
    purpose      VARCHAR(16)  NOT NULL CHECK (purpose IN ('servicing','marketing','recording')),
    channel      VARCHAR(24)  NULL,    -- NULL = all channels
    basis        VARCHAR(24)  NOT NULL CHECK (basis IN ('consent','legitimate_use','legal_obligation')),
    granted      BOOLEAN      NOT NULL,  -- a FALSE row is a withdrawal/opt-out
    evidence_ref VARCHAR(255) NULL,
    recorded_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_consents_customer FOREIGN KEY (customer_id) REFERENCES customers(id),
    KEY idx_consents_customer (customer_id, purpose, recorded_at DESC)
) ENGINE=InnoDB;

-- ===================================================================== views
-- Metrics are DERIVED, never stored -- materialising them invites drift.
-- NOW() is UTC here because every session runs at time_zone='+00:00'.

CREATE OR REPLACE VIEW v_revenue_at_risk AS
SELECT COALESCE(SUM(amount_paise), 0) AS at_risk_paise,
       COUNT(*)                       AS obligations_at_risk
FROM obligations
WHERE status IN ('past_due', 'recovering');

CREATE OR REPLACE VIEW v_contacts_rolling_7d AS
SELECT e.customer_ref,
       COUNT(*)           AS contacts_7d,
       MAX(a.executed_at) AS last_contact_at
FROM actions a
JOIN decisions d ON d.decision_id = a.decision_id
JOIN events e    ON e.event_id    = d.event_id
WHERE a.channel IS NOT NULL
  AND a.executed_at >= NOW() - INTERVAL 7 DAY
GROUP BY e.customer_ref;

CREATE OR REPLACE VIEW v_incremental_recovery AS
SELECT ha.arm,
       COUNT(DISTINCT ha.obligation_id)  AS obligations,
       COALESCE(SUM(r.amount_paise), 0)  AS recovered_paise
FROM holdout_assignments ha
LEFT JOIN recoveries r ON r.obligation_id = ha.obligation_id
GROUP BY ha.arm;

CREATE OR REPLACE VIEW v_ptp_kept_rate AS
SELECT COUNT(*)                                  AS total_promises,
       SUM(status = 'kept')                      AS kept,
       SUM(status = 'broken')                    AS broken,
       CASE WHEN SUM(status IN ('kept','broken')) = 0 THEN NULL
            ELSE SUM(status = 'kept') / SUM(status IN ('kept','broken'))
       END                                       AS kept_rate
FROM promises_to_pay;

CREATE OR REPLACE VIEW v_blocked_actions AS
SELECT d.decision_id,
       d.obligation_ref,
       d.decided_at,
       dc.id                   AS candidate_id,
       dc.action_type,
       dc.expected_value_paise,
       dc.blocked_by_gate,
       g.rule_source,
       g.detail
FROM decision_candidates dc
JOIN decisions d          ON d.decision_id  = dc.decision_id
LEFT JOIN gate_evaluations g ON g.candidate_id = dc.id AND g.passed = FALSE
WHERE dc.blocked = TRUE;

-- Actions that executed despite a failed gate on their candidate.
-- This view must ALWAYS be empty; a row here is the bug.
CREATE OR REPLACE VIEW v_constraint_violations AS
SELECT a.*
FROM actions a
JOIN gate_evaluations g ON g.candidate_id = a.candidate_id AND g.passed = FALSE
WHERE a.status IN ('executing', 'executed');
