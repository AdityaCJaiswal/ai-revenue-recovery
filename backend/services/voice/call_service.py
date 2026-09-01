"""Voice call sessions: one place that runs the turn loop, regardless of
transport (text simulation now; LiveKit audio drives the same functions).

Every agent line -- from ANY brain -- passes the screening gate before it is
spoken, and both drafted and spoken text land in voice_utterances. A blocked
line is replaced by the safe fallback and ends in a human handoff: the bot
never argues its way around its own guardrail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from ...core.logging import get_logger
from ...repositories import session as db_session
from ...repositories import voice_repository
from .brain import BrainReply, CallState, RuleBrain
from .screening import SAFE_FALLBACK, read_customer_signals, screen_agent_line

log = get_logger(__name__)

#: In-memory session registry. Single process; sessions are short-lived.
#: ponytail: move to redis if the app ever runs multi-worker.
_sessions: dict[str, "LiveSession"] = {}


@dataclass(slots=True)
class LiveSession:
    call_id: str
    decision_id: str
    obligation_ref: str
    ctx: dict[str, Any]
    state: CallState = field(default_factory=CallState)
    turn_index: int = 0
    ended: bool = False


def _decision_ctx(conn, decision_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.decision_id, d.obligation_ref, d.chosen_action,
                      e.amount_paise, e.rail, e.decline_family,
                      a.id AS action_id
               FROM decisions d
               JOIN events e ON e.event_id = d.event_id
               LEFT JOIN actions a ON a.decision_id = d.decision_id
               WHERE d.decision_id = %s""",
            (decision_id,),
        )
        return cur.fetchone()


def start_session(decision_id: str, mode: str = "text") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with db_session() as conn:
        row = _decision_ctx(conn, decision_id)
        if row is None:
            return {"error": "no such decision"}
        if row["chosen_action"] != "voice_call":
            return {"error": f"decision chose '{row['chosen_action']}', not voice_call"}
        action_id = row["action_id"]
        if action_id is None:
            return {"error": "run Execute actions first -- no reserved action for this decision"}

        call_id = voice_repository.start_call(
            conn, action_id=action_id,
            asr_model="text-sim" if mode == "text" else "saaras:v3-realtime",
            tts_model="text-sim" if mode == "text" else "bulbul:v3",
            now=now,
        )
        sess = LiveSession(
            call_id=call_id, decision_id=decision_id,
            obligation_ref=row["obligation_ref"],
            ctx={"amount_paise": row["amount_paise"], "product": "आपकी सदस्यता",
                 "rail": row["rail"], "decline_family": row["decline_family"],
                 "action_id": action_id},
        )
        _sessions[call_id] = sess
        reply = _agent_turn(conn, sess, None, now)
        # The very first line carries the AI disclosure (voluntary default,
        # RESEARCH.md 9.1) -- timestamp the proof.
        voice_repository.mark_disclosure(conn, call_id, now)
    return {"call_id": call_id, **reply}


def customer_turn(call_id: str, text: str) -> dict[str, Any]:
    sess = _sessions.get(call_id)
    if sess is None or sess.ended:
        return {"error": "no live session with that id"}
    now = datetime.now(timezone.utc)
    with db_session() as conn:
        voice_repository.log_utterance(
            conn, call_id=call_id, turn_index=sess.turn_index, speaker="customer",
            drafted=None, spoken=text, verdict=None, blocked_reason=None, latency_ms=None,
        )
        sess.turn_index += 1
        return _agent_turn(conn, sess, text, now)


def _agent_turn(conn, sess: LiveSession, customer_text: str | None,
                now: datetime) -> dict[str, Any]:
    signals = read_customer_signals(customer_text) if customer_text else None
    brain = RuleBrain()
    reply: BrainReply = brain.reply(sess.state, customer_text, signals,
                                    sess.ctx, now.date())
    sess.state.stage = reply.stage

    # THE GATE: even our own deterministic brain does not get to skip it.
    screened = screen_agent_line(reply.text)
    spoken = screened.spoken if screened.verdict == "allowed" else SAFE_FALLBACK
    voice_repository.log_utterance(
        conn, call_id=sess.call_id, turn_index=sess.turn_index, speaker="agent",
        drafted=reply.text, spoken=spoken, verdict=screened.verdict,
        blocked_reason=screened.blocked_reason, latency_ms=None,
    )
    sess.turn_index += 1

    ptp_id = None
    if screened.verdict == "blocked":
        # A blocked line is a hard stop: hand off, never improvise past the gate.
        reply.end_call, reply.disposition = True, "handoff"

    if reply.capture_ptp and sess.state.ptp_date:
        ptp_id = voice_repository.create_ptp(
            conn, obligation_ref=sess.obligation_ref, call_id=sess.call_id,
            amount_paise=sess.ctx["amount_paise"], promised_for=sess.state.ptp_date,
            verbatim=sess.state.ptp_verbatim,
            confidence_bp=int(sess.state.meta.get("confidence_bp", 0)),
            readback_confirmed=sess.state.readback_confirmed,
        )

    if reply.end_call:
        sess.ended = True
        voice_repository.end_call(
            conn, sess.call_id, disposition=reply.disposition or "completed",
            distress=bool(signals and signals.distress),
            dispute=bool(signals and signals.dispute),
            handoff=(reply.disposition == "handoff"), now=now,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE actions SET status='executed', executed_at=%s, provider_ref=%s WHERE id=%s",
                (now.replace(tzinfo=None), f"voice:{sess.call_id}:{reply.disposition}",
                 sess.ctx["action_id"]),
            )
            cur.execute("UPDATE decisions SET outcome=%s, outcome_at=%s WHERE decision_id=%s",
                        (reply.disposition, now.replace(tzinfo=None), sess.decision_id))
        conn.commit()
        _sessions.pop(sess.call_id, None)

    return {
        "agent_text": spoken,
        "drafted_text": reply.text if screened.verdict == "blocked" else None,
        "screening": screened.verdict,
        "blocked_reason": screened.blocked_reason,
        "stage": reply.stage,
        "ended": reply.end_call,
        "disposition": reply.disposition,
        "ptp_id": ptp_id,
        "readback_pending": reply.stage == "readback",
    }
