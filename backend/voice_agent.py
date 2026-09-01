"""LiveKit voice worker: real Hinglish audio on the SAME compliance pipeline.

Run (after filling LIVEKIT_* + SARVAM_API_KEY in .env):

    .venv/bin/python -m backend.voice_agent dev          # worker
    .venv/bin/python -m backend.voice_agent console      # local mic test

Two brains, one gate (select with env VOICE_BRAIN=rule|llm, default rule):
- rule: the deterministic RuleBrain -- reproducible on stage, zero LLM risk.
- llm:  sarvam-105b with function tools + the videopd-style system prompt
        (prompt_config.py / tools.py). Tools are ratified deterministically;
        end_call is gated on real outcomes.
EVERY drafted line -- from either brain -- passes screen_agent_line before TTS,
and drafted-vs-spoken lands in voice_utterances (tier-5, RESEARCH.md 10.4).

Production patterns adopted from the fibovoicebot/videopd study (see
RESEARCH.md 11): prewarmed silero VAD; explicit endpointing/interruption
config; customer-join gate before the greeting; per-turn latency stamped into
voice_utterances.latency_ms; user-away timeout.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from livekit.agents import JobContext, WorkerOptions, cli  # noqa: E402
from livekit.agents.voice import Agent, AgentSession  # noqa: E402
from livekit.plugins import sarvam, silero  # noqa: E402

from backend.core.logging import configure_logging, get_logger  # noqa: E402
from backend.repositories import session as db_session  # noqa: E402
from backend.repositories import voice_repository  # noqa: E402
from backend.services.voice.brain import CallState, RuleBrain, rupees_in_words  # noqa: E402
from backend.services.voice.call_service import _decision_ctx  # noqa: E402
from backend.services.voice.prompt_config import get_instructions  # noqa: E402
from backend.services.voice.screening import (  # noqa: E402
    SAFE_FALLBACK, read_customer_signals, screen_agent_line,
)
from backend.services.voice.tools import build_recovery_tools  # noqa: E402

configure_logging("INFO")
log = get_logger("voice-agent")

BRAIN_MODE = os.environ.get("VOICE_BRAIN", "rule")


def prewarm(proc) -> None:
    # videopd pattern: load VAD once per process, not per call.
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.2, min_silence_duration=0.45,
        activation_threshold=0.5, sample_rate=16000, force_cpu=True,
    )


class _GateMixin:
    """Shared: per-utterance screen at the TTS boundary + latency stamping."""

    call_id: str
    _turn_started: float

    async def tts_node(self, text, model_settings):  # noqa: ANN001
        async def screened():
            async for chunk in _accumulate_sentences(text):
                verdict = screen_agent_line(chunk)
                spoken = verdict.spoken if verdict.verdict == "allowed" else SAFE_FALLBACK
                latency_ms = int((time.monotonic() - self._turn_started) * 1000)
                with db_session() as conn:
                    voice_repository.log_utterance(
                        conn, call_id=self.call_id, turn_index=-1, speaker="agent",
                        drafted=chunk, spoken=spoken, verdict=verdict.verdict,
                        blocked_reason=verdict.blocked_reason, latency_ms=latency_ms)
                yield spoken
        async for frame in Agent.default.tts_node(self, screened(), model_settings):
            yield frame


async def _accumulate_sentences(text):  # noqa: ANN001
    """Chunk the LLM stream at sentence boundaries so the screen sees whole
    utterances (videopd sanitizes streamwise; our screen needs sentences)."""
    buf = ""
    async for piece in text:
        buf += piece
        while any(d in buf for d in ("।", ".", "?", "!")):
            idx = max(buf.rfind(d) for d in ("।", ".", "?", "!"))
            yield buf[: idx + 1]
            buf = buf[idx + 1:]
    if buf.strip():
        yield buf


class LLMRecoveryAgent(_GateMixin, Agent):
    """sarvam-105b + function tools, prompt in the videopd house style."""

    def __init__(self, decision_id: str, row: dict, call_id: str) -> None:
        self.call_id = call_id
        self._turn_started = time.monotonic()
        self.call_state = {
            "call_id": call_id, "obligation_ref": row["obligation_ref"],
            "amount_paise": row["amount_paise"], "recent_customer_text": [],
            "flags": {},
        }
        super().__init__(
            instructions=get_instructions(
                "hindi", brand="Razorpay Revenue Recovery",
                amount_words=rupees_in_words(row["amount_paise"]),
                product="आपकी सदस्यता", decline_family=row["decline_family"] or "payment failure",
            ),
            stt=sarvam.STTRealtime(language="auto", mode="codemix"),
            # cookbook warning: reasoning model -- set max_tokens or content is empty
            llm=sarvam.LLM(model="sarvam-105b", max_tokens=400, temperature=0.4),
            tts=sarvam.TTS(target_language_code="hi-IN", model="bulbul:v3",
                           speaker="shubh", speech_sample_rate=24000),
            tools=build_recovery_tools(self.call_state),
        )

    async def on_enter(self) -> None:
        with db_session() as conn:
            voice_repository.mark_disclosure(conn, self.call_id, datetime.now(timezone.utc))
        self.session.generate_reply()  # prompt mandates disclosure-first opening

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:  # noqa: ANN001
        self._turn_started = time.monotonic()
        text = new_message.text_content or ""
        self.call_state["recent_customer_text"].append(text)
        signals = read_customer_signals(text)
        with db_session() as conn:
            voice_repository.log_utterance(
                conn, call_id=self.call_id, turn_index=-1, speaker="customer",
                drafted=None, spoken=text, verdict=None, blocked_reason=None,
                latency_ms=None)
        if signals.needs_handoff:
            self.call_state["flags"]["wants_end"] = "handoff"
        elif signals.cease:
            self.call_state["flags"]["wants_end"] = "opt_out"


class RuleRecoveryAgent(_GateMixin, Agent):
    """Deterministic RuleBrain via session.say -- the stage-safe default."""

    def __init__(self, decision_id: str, row: dict, call_id: str) -> None:
        self.call_id = call_id
        self._turn_started = time.monotonic()
        super().__init__(
            instructions="Deterministic recovery agent. See RuleBrain.",
            stt=sarvam.STTRealtime(language="auto", mode="codemix"),
            tts=sarvam.TTS(target_language_code="hi-IN", model="bulbul:v3",
                           speaker="shubh", speech_sample_rate=24000),
        )
        self.decision_id = decision_id
        self.brain, self.state = RuleBrain(), CallState()
        self.brain_ctx = {"amount_paise": row["amount_paise"],
                          "product": "आपकी सदस्यता", "action_id": row["action_id"]}
        self.obligation_ref = row["obligation_ref"]
        self.ended = False

    async def on_enter(self) -> None:
        with db_session() as conn:
            voice_repository.mark_disclosure(conn, self.call_id, datetime.now(timezone.utc))
        await self._reply(None)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:  # noqa: ANN001
        self._turn_started = time.monotonic()
        text = new_message.text_content or ""
        with db_session() as conn:
            voice_repository.log_utterance(
                conn, call_id=self.call_id, turn_index=-1, speaker="customer",
                drafted=None, spoken=text, verdict=None, blocked_reason=None,
                latency_ms=None)
        await self._reply(text)

    async def _reply(self, customer_text: str | None) -> None:
        now = datetime.now(timezone.utc)
        signals = read_customer_signals(customer_text) if customer_text else None
        reply = self.brain.reply(self.state, customer_text, signals,
                                 self.brain_ctx, now.date())
        self.state.stage = reply.stage
        if reply.capture_ptp and self.state.ptp_date:
            with db_session() as conn:
                voice_repository.create_ptp(
                    conn, obligation_ref=self.obligation_ref, call_id=self.call_id,
                    amount_paise=self.brain_ctx["amount_paise"],
                    promised_for=self.state.ptp_date, verbatim=self.state.ptp_verbatim,
                    confidence_bp=int(self.state.meta.get("confidence_bp", 0)),
                    readback_confirmed=self.state.readback_confirmed)
        await self.session.say(reply.text)  # screen runs inside tts_node
        if reply.end_call:
            self.ended = True
            with db_session() as conn:
                voice_repository.end_call(
                    conn, self.call_id, disposition=reply.disposition or "completed",
                    distress=bool(signals and signals.distress),
                    dispute=bool(signals and signals.dispute),
                    handoff=(reply.disposition == "handoff"), now=now)
            await self.session.aclose()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    room_name = ctx.room.name
    if not room_name.startswith("call_"):
        return
    decision_id = room_name[len("call_"):]

    with db_session() as conn:
        row = _decision_ctx(conn, decision_id)
        if row is None or row["action_id"] is None:
            log.warning("room %s: no executable decision/action", room_name)
            return
        call_id = voice_repository.start_call(
            conn, action_id=row["action_id"],
            asr_model="saaras:v3-realtime", tts_model="bulbul:v3",
            now=datetime.now(timezone.utc))

    cls = LLMRecoveryAgent if BRAIN_MODE == "llm" else RuleRecoveryAgent
    agent = cls(decision_id, row, call_id)
    session = AgentSession(
        vad=ctx.proc.userdata.get("vad"),
        user_away_timeout=20.0,  # videopd default; silence-nudge hook point
    )
    await session.start(agent=agent, room=ctx.room)
    log.info("call %s live (%s brain) in %s", call_id, BRAIN_MODE, room_name)

    # LLM-brain teardown: honour wants_end set by tools/signals.
    if isinstance(agent, LLMRecoveryAgent):
        import asyncio
        # ponytail: 10-min expiry leaves the call row open (no end_call write);
        # add a timeout disposition if long calls ever matter. Demo calls are <3 min.
        for _ in range(600):  # <=10 min, matches videopd time-limit spirit
            await asyncio.sleep(1)
            reason = agent.call_state["flags"].get("wants_end")
            if reason:
                await asyncio.sleep(2)  # let the in-turn goodbye play out
                with db_session() as conn:
                    voice_repository.end_call(
                        conn, call_id, disposition=reason,
                        distress=False, dispute=False,
                        handoff=(reason == "handoff"),
                        now=datetime.now(timezone.utc))
                await session.aclose()
                break


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
