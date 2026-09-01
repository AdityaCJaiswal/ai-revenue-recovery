import type { Gate } from "../api";
import { truthy } from "../api";

/** The signature element: every gate row ends in its authority, set like a
 * legal citation. UNVERIFIED provenance is shown, never smoothed over. */
function Cite({ source }: { source: string }) {
  const unverified = source.includes("UNVERIFIED");
  const primary = source.includes("[P]");
  return (
    <span className="cite">
      <span>{source.replace(/\s*\[(P|UNVERIFIED[^\]]*)\]\s*/g, " ").trim()}</span>
      {primary && <span className="chip grade-p">P</span>}
      {unverified && <span className="chip grade-u">UNVERIFIED</span>}
    </span>
  );
}

export function GateList({ gates }: { gates: Gate[] }) {
  return (
    <div className="gates">
      {gates.map((g, i) => (
        <div className="gate" key={i}>
          <span className={"ic " + (truthy(g.passed) ? "p" : "f")} aria-hidden>
            {truthy(g.passed) ? "✓" : "✕"}
          </span>
          <span>
            <span className="gname">{g.gate_name}</span>
            {g.detail && <div className="detail">{g.detail}</div>}
            <Cite source={g.rule_source} />
          </span>
        </div>
      ))}
    </div>
  );
}
