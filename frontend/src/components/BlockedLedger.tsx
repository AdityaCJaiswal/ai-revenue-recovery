import type { BlockedRow } from "../api";
import { rupees } from "../api";

/** What the agent refused to do, grouped by the rule that said no. */
export function BlockedLedger({ rows, onSelect }: { rows: BlockedRow[]; onSelect: (id: string) => void }) {
  const byGate = new Map<string, number>();
  for (const r of rows) byGate.set(r.blocked_by_gate ?? "?", (byGate.get(r.blocked_by_gate ?? "?") ?? 0) + 1);
  const top = rows.slice(0, 40);
  return (
    <div className="ledger">
      <div className="paneh">
        Refused actions <span className="n">{rows.length} shown</span>
      </div>
      <div className="groups">
        {[...byGate.entries()].sort((a, b) => b[1] - a[1]).map(([g, n]) => (
          <span className="gcount" key={g}>{g} × {n}</span>
        ))}
      </div>
      <div className="scroll">
        <table className="bl">
          <tbody>
            {top.map((r) => (
              <tr key={r.candidate_id} onClick={() => onSelect(r.decision_id)}
                  style={{ cursor: "pointer" }} title="Open this decision in the drawer">
                <td><span className="bact">{r.action_type.replace(/_/g, " ")}</span></td>
                <td className="ev">{rupees(r.expected_value_paise)}</td>
                <td><span className="stamp">Blocked · {r.blocked_by_gate}</span></td>
                <td className="cite-cell">{r.rule_source ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
