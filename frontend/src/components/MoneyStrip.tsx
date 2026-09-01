import type { Metrics } from "../api";
import { rupeesCompact } from "../api";

const bp = (v?: number | null) => (v == null ? "—" : `${(v / 100).toFixed(1)}%`);

/** The judge reads this strip before anything else. Every figure is mono and
 * exact; a rupee number never appears without its cohort label. */
export function MoneyStrip({ m }: { m: Metrics | null }) {
  const withheld = m?.chosen_actions["WITHHELD_OR_NONE"] ?? 0;
  const inc = m?.holdout?.incremental_paise;
  return (
    <div className="strip" role="region" aria-label="Key figures">
      <div className="cards">
        <div className="tile">
          <div className="v">{m ? rupeesCompact(m.revenue_at_risk_paise) : "—"}</div>
          <div className="k">Revenue at risk</div>
          <div className="s">{m ? `${m.obligations_at_risk} obligations` : ""}</div>
        </div>
        <div className="tile">
          <div className="v">{m && m.recovered_paise > 0 ? rupeesCompact(m.recovered_paise) : "₹0"}</div>
          <div className="k">Recovered</div>
          <div className="s">
            {m && m.recoveries_count > 0
              ? `${m.recoveries_count} recoveries · ${m.avg_days_to_cash}d to cash`
              : "run the agent, then advance days"}
          </div>
        </div>
        <div className="tile">
          <div className={"v" + (inc != null && inc > 0 ? " pass" : "")}>
            {inc != null ? rupeesCompact(inc) : "—"}
          </div>
          <div className="k">Incremental vs holdout</div>
          <div className="s">
            {m?.holdout?.treatment_rate_bp != null
              ? `${bp(m.holdout.treatment_rate_bp)} vs ${bp(m.holdout.control_rate_bp)} control`
              : `${m?.holdout?.control.obligations ?? 0} control obligations withheld`}
          </div>
        </div>
        <div className="tile">
          <div className="v">{m?.blocked_candidates ?? "—"}</div>
          <div className="k">Actions refused</div>
          <div className="s">each cites its rule</div>
        </div>
        <div className="tile">
          <div className="v">{m?.ptp?.kept_rate_bp != null ? bp(m.ptp.kept_rate_bp) : withheld}</div>
          <div className="k">{m?.ptp?.kept_rate_bp != null ? "Promises kept" : "Withheld — control"}</div>
          <div className="s">
            {m?.ptp?.kept_rate_bp != null
              ? `${m.ptp.kept} kept · ${m.ptp.broken} broken — the rate no vendor publishes`
              : m ? `${m.arms["control"] ?? 0} of ${m.decisions_total} in holdout` : ""}
          </div>
        </div>
        <div className="tile">
          <div className={"v " + (m && m.constraint_violations > 0 ? "block" : "pass")}>
            {m?.constraint_violations ?? "—"}
          </div>
          <div className="k">Constraint violations</div>
          <div className="s">must be zero</div>
        </div>
      </div>
      <div className="cohort">Test-mode + synthetic cohort; outcomes time-compressed by the simulator — no production money is shown.</div>
    </div>
  );
}
