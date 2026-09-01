import { useCallback, useEffect, useRef, useState } from "react";
import type { BlockedRow, DecisionDetail, DecisionRow, Health, Metrics } from "./api";
import { api } from "./api";
import { BlockedLedger } from "./components/BlockedLedger";
import { Controls } from "./components/Controls";
import { DecisionDrawer } from "./components/DecisionDrawer";
import { DecisionFeed } from "./components/DecisionFeed";
import { MoneyStrip } from "./components/MoneyStrip";

const POLL_MS = 2500;

type Theme = "system" | "dark" | "light";

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("theme") as Theme) ?? "light",  // Razorpay console world is light-first
  );
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  }, [theme]);
  const cycle = () =>
    setTheme((t) => (t === "system" ? "dark" : t === "dark" ? "light" : "system"));
  return [theme, cycle];
}

export default function App() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [rows, setRows] = useState<DecisionRow[]>([]);
  const [blocked, setBlocked] = useState<BlockedRow[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DecisionDetail | null>(null);
  const [theme, cycleTheme] = useTheme();
  const inflight = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (document.hidden) return; // don't poll a hidden tab
    inflight.current?.abort();
    const ctl = new AbortController();
    inflight.current = ctl;
    try {
      const [m, d, b, h] = await Promise.all([
        api.metrics(ctl.signal),
        api.decisions(ctl.signal),
        api.blocked(ctl.signal),
        api.health(ctl.signal),
      ]);
      setMetrics(m); setRows(d); setBlocked(b); setHealth(h);
    } catch {
      /* transient poll failure: keep last good state; health dot goes stale */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => { clearInterval(t); inflight.current?.abort(); };
  }, [refresh]);

  const select = useCallback((id: string) => {
    setSelected(id);
    api.decision(id).then(setDetail).catch(() => setDetail(null));
  }, []);

  return (
    <div className="shell">
      <header className="top">
        <span className="wordmark">
          <svg className="bolt" viewBox="0 0 24 24" aria-hidden="true">
            {/* abstract payment bolt — not the Razorpay trademark */}
            <path d="M13.4 2 5 14.2h5.1L8.6 22 19 9.4h-5.4L16 2z" />
          </svg>
          <span className="rzp">Razorpay</span>
          Revenue Recovery <span className="dim">· control tower</span>
        </span>
        <span className="mode">{health?.strategy ?? "…"} mode</span>
        <span className="spacer" />
        <Controls onDone={refresh} />
        <button onClick={cycleTheme} aria-label={`Theme: ${theme}`}>
          {theme === "system" ? "◐" : theme === "dark" ? "●" : "○"}
        </button>
        <span className="health">
          <span className={"dot" + (health?.ok ? " ok" : "")} aria-hidden />
          {health ? `${health.events} events` : "connecting…"}
        </span>
      </header>

      <MoneyStrip m={metrics} />

      <div className="main">
        <div className="feedwrap">
          <div className="paneh">
            Decision feed <span className="n">newest first · click a row</span>
          </div>
          <div className="scroll">
            <DecisionFeed rows={rows} selected={selected} onSelect={select} />
          </div>
        </div>
        <aside className="drawer" aria-label="Decision detail">
          <DecisionDrawer d={detail} onRefresh={refresh} />
        </aside>
      </div>

      <BlockedLedger rows={blocked} onSelect={select} />
    </div>
  );
}
