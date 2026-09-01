import type { DecisionRow } from "../api";
import { istTime, rupees } from "../api";

/* Chosen action reads as text + one semantic dot — no chip wall. */
function Action({ row }: { row: DecisionRow }) {
  if (row.arm === "control" && row.chosen_action === null)
    return <span className="pill withheld">withheld</span>;
  if (row.chosen_action === null || row.chosen_action === "do_nothing")
    return <span className="act none"><span className="adot" />no action</span>;
  return (
    <span className={"act" + (row.chosen_action === "voice_call" ? " voice" : "")}>
      <span className="adot" />
      {row.chosen_action.replace(/_/g, " ")}
    </span>
  );
}

export function DecisionFeed({
  rows, selected, onSelect,
}: {
  rows: DecisionRow[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  if (rows.length === 0)
    return (
      <div className="empty">
        No decisions yet.<br />Seed 500 events, then run the agent.
      </div>
    );
  return (
    <table className="feed">
      <tbody>
        {rows.map((r) => (
          <tr
            key={r.decision_id}
            className={"row" + (r.decision_id === selected ? " sel" : "")}
            onClick={() => onSelect(r.decision_id)}
            onKeyDown={(e) => e.key === "Enter" && onSelect(r.decision_id)}
            tabIndex={0}
            role="button"
            aria-label={`Open decision for ${r.obligation_ref}`}
          >
            <td className="t">{istTime(r.decided_at)}</td>
            <td><span className="rail">{(r.rail ?? "—").replace(/_/g, " ")}</span></td>
            <td className="fam">{r.decline_family.replace(/_/g, " ")}</td>
            <td className="amt">{rupees(r.amount_paise)}</td>
            <td><Action row={r} /></td>
            <td>{r.arm === "control" ? <span className="pill control">C</span> : null}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
