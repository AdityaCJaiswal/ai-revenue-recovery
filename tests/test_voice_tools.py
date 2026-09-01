"""Checks for the LLM-mode function tools: the deterministic ratification and
the end_call outcome gate. These are the two places a hallucinating LLM could
corrupt the record -- so they get their own runnable checks.

    python tests/test_voice_tools.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents.llm import ToolError  # noqa: E402

from backend.services.voice.tools import build_recovery_tools  # noqa: E402


def _state(**flags):
    return {
        "call_id": "call_x", "obligation_ref": "sub_x", "amount_paise": 49900,
        "recent_customer_text": [], "flags": dict(flags),
    }


def _tool(tools, name):
    return next(t for t in tools if getattr(t, "__name__", "") == name or name in repr(t))


def test_capture_promise_rejects_date_absent_from_transcript():
    state = _state()
    state["recent_customer_text"] = ["haan thik hai", "abhi busy hoon"]  # no date
    capture = build_recovery_tools(state)[0]
    try:
        asyncio.run(capture(None, promised_date_words="pandrah tarikh", readback_confirmed=True))
        raise AssertionError("hallucinated promise was accepted")
    except ToolError:
        pass
    assert "promise_captured" not in state["flags"]


def test_capture_promise_rejects_mismatched_date():
    state = _state()
    state["recent_customer_text"] = ["main dus tarikh ko kar dunga"]  # customer said 10th
    capture = build_recovery_tools(state)[0]
    try:
        asyncio.run(capture(None, promised_date_words="pandrah tarikh", readback_confirmed=True))
        raise AssertionError("mismatched date was accepted")
    except ToolError:
        pass


def test_capture_promise_rejects_without_readback():
    state = _state()
    state["recent_customer_text"] = ["pandrah tarikh ko pakka"]
    capture = build_recovery_tools(state)[0]
    try:
        asyncio.run(capture(None, promised_date_words="pandrah tarikh", readback_confirmed=False))
        raise AssertionError("promise without read-back was accepted")
    except ToolError:
        pass


def test_end_call_completed_requires_an_outcome_then_downgrades():
    state = _state()
    end_call = build_recovery_tools(state)[3]
    for _ in range(2):  # two bounces with corrective feedback
        try:
            asyncio.run(end_call(None, reason="completed"))
            raise AssertionError("empty-handed 'completed' was accepted")
        except ToolError:
            pass
    asyncio.run(end_call(None, reason="completed"))  # third: graceful downgrade
    assert state["flags"]["wants_end"] == "customer_unavailable"


def test_end_call_completed_passes_after_link_sent():
    state = _state()
    tools = build_recovery_tools(state)
    send_link, end_call = tools[1], tools[3]
    coaching = asyncio.run(send_link(None))
    assert "WhatsApp" in coaching
    asyncio.run(end_call(None, reason="completed"))
    assert state["flags"]["wants_end"] == "completed"


def test_opt_out_never_bounces():
    state = _state()
    end_call = build_recovery_tools(state)[3]
    asyncio.run(end_call(None, reason="opt_out"))
    assert state["flags"]["wants_end"] == "opt_out"


if __name__ == "__main__":
    checks = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in checks:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(checks)} checks passed")
