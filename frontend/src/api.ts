/** Typed fetchers mirroring the FastAPI JSON. One module owns every request. */

export interface Metrics {
  events_total: number;
  decisions_total: number;
  chosen_actions: Record<string, number>;
  arms: Record<string, number>;
  blocked_candidates: number;
  gate_evaluations: number;
  revenue_at_risk_paise: number;
  obligations_at_risk: number;
  constraint_violations: number;
  cohort_note: string;
  recovered_paise: number;
  recoveries_count: number;
  avg_days_to_cash: number;
  execution_cost_paise: number;
  holdout: {
    treatment: { obligations: number; recovered_paise: number };
    control: { obligations: number; recovered_paise: number };
    treatment_rate_bp?: number;
    control_rate_bp?: number;
    incremental_paise?: number;
  };
  ptp: { total: number; kept: number; broken: number; kept_rate_bp: number | null };
}

export interface DecisionRow {
  decision_id: string;
  obligation_ref: string;
  strategy: string;
  arm: string | null;
  diagnosis_family: string | null;
  chosen_action: string | null;
  scheduled_for: string | null;
  decided_at: string;
  rail: string | null;
  decline_family: string;
  amount_paise: number;
  event_type: string;
}

export interface Gate {
  gate_name: string;
  passed: number | boolean;
  detail: string | null;
  rule_source: string;
}

export interface Candidate {
  id: number;
  action_type: string;
  channel: string | null;
  expected_value_paise: number;
  cost_paise: number;
  success_prob_bp: number | null;
  rank_order: number;
  blocked: number | boolean;
  blocked_by_gate: string | null;
  gates: Gate[];
}

export interface DecisionDetail extends DecisionRow {
  diagnosis_rationale: string | null;
  caps_version: string | null;
  customer_ref: string;
  attempt_number: number;
  candidates: Candidate[];
}

export interface BlockedRow {
  decision_id: string;
  obligation_ref: string;
  decided_at: string;
  candidate_id: number;
  action_type: string;
  expected_value_paise: number;
  blocked_by_gate: string | null;
  rule_source: string | null;
  detail: string | null;
}

export interface Health {
  ok: boolean;
  events: number;
  webhook_signature_enforced: boolean;
  strategy: string;
  unverified_retry_caps: string[];
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(path, { signal });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json() as Promise<T>;
}

export const api = {
  metrics: (s?: AbortSignal) => get<Metrics>("/decisions/metrics", s),
  decisions: (s?: AbortSignal) =>
    get<{ decisions: DecisionRow[] }>("/decisions?limit=120", s).then((d) => d.decisions),
  decision: (id: string, s?: AbortSignal) => get<DecisionDetail>(`/decisions/${id}`, s),
  blocked: (s?: AbortSignal) =>
    get<{ blocked: BlockedRow[] }>("/decisions/blocked?limit=200", s).then((d) => d.blocked),
  health: (s?: AbortSignal) => get<Health>("/health", s),

  seed: () => fetch("/admin/generate?count=500&seed=42", { method: "POST" }).then((r) => r.json()),
  run: () => fetch("/admin/process?limit=2000", { method: "POST" }).then((r) => r.json()),
  execute: () => fetch("/admin/execute", { method: "POST" }).then((r) => r.json()),
  advance: (days: number) =>
    fetch(`/admin/simulate?days=${days}`, { method: "POST" }).then((r) => r.json()),
};

/* ---- formatting ---- */

const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** Integer paise -> Indian-grouped rupee string. Display only. */
export function rupees(paise: number): string {
  const sign = paise < 0 ? "−" : "";
  return `${sign}₹${inr.format(Math.trunc(Math.abs(paise) / 100))}`;
}

/** Compact form for the strip: 41.1L / 2.3Cr. */
export function rupeesCompact(paise: number): string {
  const r = paise / 100;
  if (Math.abs(r) >= 1_00_00_000) return `₹${(r / 1_00_00_000).toFixed(1)}Cr`;
  if (Math.abs(r) >= 1_00_000) return `₹${(r / 1_00_000).toFixed(1)}L`;
  return rupees(paise);
}

/** Backend datetimes are UTC-naive ISO strings; render in IST. */
export function istTime(iso: string): string {
  return new Date(iso.endsWith("Z") ? iso : iso + "Z").toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export const truthy = (v: number | boolean): boolean => v === true || v === 1;
