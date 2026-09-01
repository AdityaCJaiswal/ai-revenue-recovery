import { useEffect, useRef, useState } from "react";

let lkRoom: import("livekit-client").Room | null = null;

interface Turn {
  speaker: "agent" | "customer";
  text: string;
  drafted?: string | null;
  screening?: string;
  blocked_reason?: string | null;
}

interface TurnResponse {
  agent_text: string;
  drafted_text: string | null;
  screening: string;
  blocked_reason: string | null;
  stage: string;
  ended: boolean;
  disposition: string | null;
  ptp_id: string | null;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText);
  return r.json() as Promise<T>;
}

/** Text-mode call simulation: the full voice pipeline (brain -> per-utterance
 * screen -> PTP read-back -> audit rows) minus the audio transport. Works with
 * zero external services -- the wifi-dead demo path. */
export function CallPanel({ decisionId, onDone }: { decisionId: string; onDone: () => void }) {
  const [callId, setCallId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [stage, setStage] = useState("");
  const [ended, setEnded] = useState<string | null>(null);
  const [ptp, setPtp] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [livekit, setLivekit] = useState(false);
  const [voiceLive, setVoiceLive] = useState(false);
  const pollTimer = useRef<number | undefined>(undefined);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/voice/config").then((r) => r.json()).then((c) => setLivekit(!!c.livekit)).catch(() => {});
    return () => { lkRoom?.disconnect(); lkRoom = null; };
  }, []);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [turns]);

  async function start() {
    setError("");
    try {
      const s = await post<TurnResponse & { call_id: string }>(
        `/voice/sessions?decision_id=${decisionId}`,
      );
      setCallId(s.call_id);
      setTurns([{ speaker: "agent", text: s.agent_text, screening: s.screening }]);
      setStage(s.stage);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function send() {
    if (!callId || !input.trim()) return;
    const text = input.trim();
    setInput("");
    setTurns((t) => [...t, { speaker: "customer", text }]);
    try {
      const r = await post<TurnResponse>(`/voice/sessions/${callId}/turn`, { text });
      setTurns((t) => [...t, {
        speaker: "agent", text: r.agent_text, drafted: r.drafted_text,
        screening: r.screening, blocked_reason: r.blocked_reason,
      }]);
      setStage(r.stage);
      if (r.ptp_id) setPtp(r.ptp_id);
      if (r.ended) { setEnded(r.disposition); onDone(); }
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function startVoice() {
    setError("");
    try {
      const cfg = await post<{ url: string; token: string; room: string }>(
        `/voice/livekit/token?decision_id=${decisionId}`,
      );
      const { Room, RoomEvent, Track } = await import("livekit-client");
      const room = new Room();
      lkRoom = room;
      room.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === Track.Kind.Audio) {
          const el = track.attach();
          el.setAttribute("data-call-audio", "1");
          document.body.appendChild(el);
        }
      });
      await room.connect(cfg.url, cfg.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      setVoiceLive(true);
      // transcript + PTP arrive via the DB the worker writes -- poll the call view
      pollTimer.current = window.setInterval(async () => {
        try {
          const calls = await fetch(`/decisions/${decisionId}`).then((r) => r.json());
          void calls; // drawer refresh happens via onDone below on hangup
        } catch { /* keep polling */ }
      }, 4000);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function hangup() {
    lkRoom?.disconnect();
    lkRoom = null;
    document.querySelectorAll("[data-call-audio]").forEach((el) => el.remove());
    setVoiceLive(false);
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    onDone();
  }

  if (voiceLive)
    return (
      <div className="callpanel live">
        <div className="callend">
          🎙 Live voice call in progress — speak in Hinglish. Transcript, screening
          verdicts and any promise land in the audit trail as you talk.
        </div>
        <button onClick={hangup}>Hang up</button>
        {error && <span className="callerr">{error}</span>}
      </div>
    );

  if (callId === null)
    return (
      <div className="callpanel">
        <div style={{ display: "flex", gap: 8 }}>
          <button className="primary" onClick={start}>Simulate the call</button>
          {livekit && <button onClick={startVoice}>🎙 Voice call (LiveKit)</button>}
        </div>
        <span className="callnote">
          Text-mode: same brain, same per-utterance screen, same PTP read-back —
          no audio transport. Try answering in Hinglish, e.g. “pandrah tarikh ko
          salary ke baad”.
        </span>
        {error && <span className="callerr">{error}</span>}
      </div>
    );

  return (
    <div className="callpanel live">
      <div className="calllog" ref={logRef} aria-live="polite">
        {turns.map((t, i) => (
          <div key={i} className={"turn " + t.speaker}>
            <span className="who">{t.speaker === "agent" ? "Agent" : "You"}</span>
            <span className="bubble">
              {t.text}
              {t.screening === "blocked" && (
                <span className="screened">
                  Drafted line blocked · {t.blocked_reason}
                  {t.drafted ? <em> “{t.drafted}”</em> : null}
                </span>
              )}
            </span>
          </div>
        ))}
        {ended && (
          <div className="callend">
            Call ended — <b>{ended}</b>
            {ptp && <> · promise recorded ({ptp})</>}
          </div>
        )}
      </div>
      {!ended && (
        <form className="callinput" onSubmit={(e) => { e.preventDefault(); void send(); }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={stage === "readback" ? "haan sahi hai / nahi…" : "Reply in Hinglish…"}
            aria-label="Your reply"
          />
          <button className="primary" type="submit">Send</button>
        </form>
      )}
      {error && <span className="callerr">{error}</span>}
    </div>
  );
}
