import { useState } from "react";
import { api } from "../api";

/** Drives the stage demo: seed the deterministic batch, run the agent. */
export function Controls({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState("");

  async function act(label: string, fn: () => Promise<Record<string, unknown>>) {
    setBusy(label);
    try {
      const out = await fn();
      setToast(
        "processed" in out ? `${out.processed} events decided`
        : "saved" in out ? `${out.saved} new events seeded`
        : "executed" in out ? `${out.executed} actions executed`
        : "recovered" in out ? `${out.recovered} recovered (+${out.organic_control} organic)`
        : "done",
      );
    } catch (e) {
      setToast(`failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
      onDone();
    }
  }

  return (
    <>
      <button disabled={busy !== null} onClick={() => act("seed", api.seed)}>
        {busy === "seed" ? "Seeding…" : "Seed 500 events"}
      </button>
      <button className="primary" disabled={busy !== null} onClick={() => act("run", api.run)}>
        {busy === "run" ? "Deciding…" : "Run the agent"}
      </button>
      <button disabled={busy !== null} onClick={() => act("execute", api.execute)}>
        {busy === "execute" ? "Executing…" : "Execute actions"}
      </button>
      <button disabled={busy !== null} onClick={() => act("advance", () => api.advance(3))}>
        {busy === "advance" ? "Advancing…" : "Advance 3 days"}
      </button>
      <span className="toast" role="status">{toast}</span>
    </>
  );
}
