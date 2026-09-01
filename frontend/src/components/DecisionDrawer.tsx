import type { Candidate, DecisionDetail } from "../api";
import { CallPanel } from "./CallPanel";
import { rupees, truthy } from "../api";
import { GateList } from "./GateList";

function CandidateCard({ c, maxAbs, chosen }: { c: Candidate; maxAbs: number; chosen: boolean }) {
  const blocked = truthy(c.blocked);
  const ev = c.expected_value_paise;
  const width = maxAbs > 0 ? Math.max(2, (Math.abs(ev) / maxAbs) * 100) : 2;
  return (
    <details
      className={"cand" + (chosen ? " chosen" : "") + (blocked ? " blocked" : "")}
      // the chosen action opens with its gates visible -- that IS the demo
      open={chosen || undefined}
    >
      <summary aria-label={`${c.action_type}, expected value ${rupees(ev)}${blocked ? ", blocked" : ""}. Toggle rule checks.`}>
        <span className="chev" aria-hidden>▶</span>
        <span className="rank">{c.rank_order}</span>
        <span className="aname">{c.action_type}</span>
        {chosen && <span className="stamp ok">Chosen</span>}
        {blocked && <span className="stamp">Blocked · {c.blocked_by_gate}</span>}
        <span className="evwrap" aria-hidden>
          <span className={"ev" + (ev < 0 ? " neg" : "")} style={{ width: `${width}%` }} />
        </span>
        <span className={"evn" + (ev < 0 ? " neg" : "")}>{rupees(ev)}</span>
      </summary>
      <GateList gates={c.gates} />
    </details>
  );
}

export function DecisionDrawer({ d, onRefresh }: { d: DecisionDetail | null; onRefresh: () => void }) {
  if (d === null)
    return <div className="empty">Select a decision to see its full reasoning:<br />diagnosis, every candidate&rsquo;s expected value, and every rule checked.</div>;

  const maxAbs = Math.max(1, ...d.candidates.map((c) => Math.abs(c.expected_value_paise)));
  return (
    <>
      <div className="dhead">
        <div className="amount">{rupees(d.amount_paise)}</div>
        <div className="meta">
          {d.rail ?? "—"} · {d.decline_family} · attempt {d.attempt_number} · {d.obligation_ref}
          {d.arm === "control" && <> · <span className="pill control">Control arm</span></>}
        </div>
      </div>
      <div className="scroll">
        <div className="diag">
          <strong>{d.diagnosis_family}</strong> — {d.diagnosis_rationale}
        </div>
        {d.chosen_action === "voice_call" && (
          <>
            <div className="sect">Voice call — Hinglish agent, screened per utterance</div>
            <CallPanel decisionId={d.decision_id} onDone={onRefresh} />
          </>
        )}
        <div className="sect">Candidates, ranked by expected value</div>
        {d.candidates.map((c) => (
          <CandidateCard
            key={c.id}
            c={c}
            maxAbs={maxAbs}
            chosen={d.chosen_action === c.action_type}
          />
        ))}
        {d.caps_version && (
          <div className="sect" title="sha256 of caps.yaml in force at decision time">
            decided under rules {d.caps_version.slice(0, 12)}…
          </div>
        )}
      </div>
    </>
  );
}
