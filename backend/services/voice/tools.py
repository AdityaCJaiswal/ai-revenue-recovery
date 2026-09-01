"""Function tools for the LLM-brained voice mode, in the fibovoicebot house
style (studied from Backend/agents/shared_agent_core.py + language_switching.py):

- closure factories bound to per-call state, passed via Agent(tools=[...])
- module-level _*_TOOL_DOC constants written as imperatives: when to call,
  when NOT to, and what to speak in the SAME turn
- Annotated[Literal[...], "description"] parameters
- return conventions: None suppresses the follow-up reply; a str return is
  coaching text the LLM must obey next turn; ToolError bounces with corrective
  feedback while keeping the call alive
- hallucinated calls are RATIFIED deterministically against the customer's own
  recent transcript before any side effect (their set_language pattern)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from ...core.logging import get_logger
from ...repositories import session as db_session
from ...repositories import voice_repository
from .brain import extract_promise_date, rupees_in_words

log = get_logger(__name__)

EndCallReason = Literal["completed", "customer_unavailable", "opt_out", "handoff"]

_CAPTURE_PROMISE_TOOL_DOC = """Record the customer's promise to pay.

Call this ONLY after BOTH are true:
- the customer stated a specific payment date, AND
- you read the date and amount back in words and the customer said yes.

Set readback_confirmed=true only if that yes actually happened. The system
independently checks the customer's own words for the date — a call without
transcript evidence is rejected and you must ask for the date again.
After a successful capture, thank them briefly and call end_call reason
'completed' in a following turn. Never call this twice for the same promise."""

_SEND_PAYMENT_LINK_TOOL_DOC = """Send a secure payment link to the customer's WhatsApp.

Call when the customer agrees to pay via link, cannot talk long, or asks for
the link. Tell them in the SAME turn that the link is on its way on WhatsApp.
Never promise any discount or changed amount with the link."""

_ESCALATE_HUMAN_TOOL_DOC = """Transfer this matter to a human colleague.

Call IMMEDIATELY when the customer: says they already paid, disputes the
amount or the charge, sounds distressed or angry about the calls, or raises
anything legal. Say ONE short apologetic line in the SAME turn ("मैं आपको अपने
सीनियर सहकर्मी से जोड़ रहा हूँ") — the system ends the call after it plays.
Never argue with the customer instead of calling this."""

_END_CALL_TOOL_DOC = """End the phone call.

Choose `reason` based on WHY the call is ending:
- 'completed' — a promise was captured or a payment link was sent. Say ONE
  short thank-you goodbye in the SAME turn.
- 'customer_unavailable' — the customer is busy or cannot talk. Offer the
  link first if possible; say a brief goodbye in the SAME turn.
- 'opt_out' — the customer asked to stop calls. Confirm you will not call
  this number again, in the SAME turn. This is honoured permanently.
- 'handoff' — escalate_human was already called; the system closes.

Never call with reason 'completed' when no promise was captured AND no link
was sent — that call is rejected."""


def build_recovery_tools(call_state: dict):
    """Bind per-call state (the videopd closure-factory pattern).

    call_state keys: call_id, obligation_ref, amount_paise, recent_customer_text
    (list[str], maintained by the agent), flags dict mutated by tools:
    {promise_captured, link_sent, wants_end: reason|None}.
    """

    @function_tool(name="capture_promise", description=_CAPTURE_PROMISE_TOOL_DOC)
    async def capture_promise(
        ctx: RunContext,
        promised_date_words: Annotated[
            str, "The date exactly as the customer said it, e.g. 'pandrah tarikh' or '15 September'.",
        ],
        readback_confirmed: Annotated[
            bool, "True ONLY if the customer said yes to your read-back of date and amount.",
        ],
    ) -> str:
        today = datetime.now(timezone.utc).date()
        # Deterministic ratification against the customer's OWN words -- a
        # hallucinated promise never reaches the database.
        recent = " ".join(call_state.get("recent_customer_text", [])[-3:])
        iso_claim, _ = extract_promise_date(promised_date_words, today)
        iso_heard, conf = extract_promise_date(recent, today)
        if iso_heard is None or iso_claim != iso_heard:
            raise ToolError(
                "The date is not confirmed in the customer's own words. Ask "
                "plainly for the payment date again, then read it back."
            )
        if not readback_confirmed:
            raise ToolError(
                "Read the date and amount back in words and wait for a clear "
                "yes before calling capture_promise again."
            )
        with db_session() as conn:
            ptp_id = voice_repository.create_ptp(
                conn, obligation_ref=call_state["obligation_ref"],
                call_id=call_state["call_id"],
                amount_paise=call_state["amount_paise"],
                promised_for=iso_heard, verbatim=recent[-255:],
                confidence_bp=conf, readback_confirmed=True,
            )
        call_state["flags"]["promise_captured"] = True
        log.info("capture_promise ratified -> %s (%s)", ptp_id, iso_heard)
        return (
            f"Promise recorded for {iso_heard}. Thank the customer in one short "
            "sentence and call end_call with reason 'completed'."
        )

    @function_tool(name="send_payment_link", description=_SEND_PAYMENT_LINK_TOOL_DOC)
    async def send_payment_link(ctx: RunContext) -> str:
        # The executor's link sink owns real link creation; on-call we mark
        # intent and the closing pipeline sends it (keeps one code path).
        call_state["flags"]["link_sent"] = True
        amount = rupees_in_words(call_state["amount_paise"])
        return (
            f"Link queued for WhatsApp. Tell the customer a secure link for "
            f"{amount} is on its way on WhatsApp, in one sentence."
        )

    @function_tool(name="escalate_human", description=_ESCALATE_HUMAN_TOOL_DOC)
    async def escalate_human(ctx: RunContext) -> None:
        call_state["flags"]["wants_end"] = "handoff"
        return None  # None: no follow-up LLM reply -- the in-turn line stands

    @function_tool(name="end_call", description=_END_CALL_TOOL_DOC)
    async def end_call(
        ctx: RunContext,
        reason: Annotated[
            EndCallReason,
            "Why the call is ending — see the tool description for when to use each value.",
        ] = "completed",
    ) -> None:
        flags = call_state["flags"]
        if reason == "completed" and not (flags.get("promise_captured") or flags.get("link_sent")):
            # Deterministic gate (their pattern uses an LLM validator; ours can
            # be exact because the flags ARE the ground truth). Capped bounce.
            flags["end_rejects"] = flags.get("end_rejects", 0) + 1
            if flags["end_rejects"] <= 2:
                raise ToolError(
                    "Nothing was resolved yet: no promise captured and no link "
                    "sent. Offer the payment link, or ask for a payment date."
                )
            reason = "customer_unavailable"  # graceful premature close
        flags["wants_end"] = reason
        return None

    return [capture_promise, send_payment_link, escalate_human, end_call]
