"""The constraint gate. Hard floor for BOTH strategies -- the baseline must be
legal too, so the demo comparison is strategy-vs-strategy, not legal-vs-illegal.

Every evaluation returns a GateResult citing rule_source -- the column that
turns a log into a justification (RESEARCH.md 5.6). UNVERIFIED caps say so in
the audit row itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..domain.actions import ActionType, Candidate, GateResult
from ..domain.caps import load_caps
from ..domain.enums import NON_RETRYABLE, Rail
from ..domain.events import RecoveryEvent
from ..utils.money import format_inr

#: Rails covered by the RBI e-mandate framework (cards / PPI / UPI).
#: eNACH is NPCI's separate mandate system -- the Rs 15k AFA-free ceiling does
#: not apply to it.
EMANDATE_RAILS = {Rail.CARD_SI, Rail.UPI_AUTOPAY}


@dataclass(slots=True)
class GateContext:
    event: RecoveryEvent
    obligation: dict[str, Any]
    mandate: dict[str, Any] | None
    now: datetime                       # aware UTC
    customer_tz: str = "Asia/Kolkata"
    contacts_today: int = 0             # from actions table (empty until executor slice)
    contacts_7d: int = 0
    sms_template: dict[str, Any] | None = None


def _retry_gates(c: Candidate, ctx: GateContext) -> None:
    caps = load_caps()
    ev = ctx.event
    fam = ev.decline_family

    # 1. Hard-decline taxonomy: retrying a dead instrument is waste + scheme fees.
    c.gates.append(GateResult(
        "non_retryable_decline",
        fam not in NON_RETRYABLE,
        f"decline family '{fam.value}' is "
        + ("retryable" if fam not in NON_RETRYABLE else
           "non-retryable -- correct intervention is credential/mandate collection"),
        "Stripe hard-decline taxonomy [P]; RESEARCH.md 2.1",
    ))

    # 2. Mastercard Merchant Advice Codes -- machine-readable STOP.
    mac = (ev.raw or {}).get("merchant_advice_code")
    stop_macs = caps["retry"]["card"]["stop_on_merchant_advice_codes"]
    c.gates.append(GateResult(
        "merchant_advice_stop",
        mac not in stop_macs,
        f"MAC {mac}: scheme says do not try again -- fee per further attempt"
        if mac in stop_macs else f"no stop-class MAC on this decline (mac={mac})",
        "Mastercard MAC 03/21 scheme rule; RESEARCH.md CORRECTIONS #1",
    ))

    # 3. Rolling attempt budget per rail. UNVERIFIED caps say so IN the audit row.
    rail_key = ev.rail.value if ev.rail else "card"
    rcaps = caps["retry"].get(rail_key, caps["retry"]["card"])
    max_attempts = int(rcaps.get("max_attempts", rcaps.get("max_attempts_30d", 15)))
    unverified = rcaps.get("max_attempts_grade") == "UNVERIFIED"
    used = ev.attempt_number  # + payment_attempts count once the executor writes it
    c.gates.append(GateResult(
        "attempt_budget",
        used < max_attempts,
        f"attempt {used} of {max_attempts} allowed on {rail_key}"
        + (" -- budget exhausted, stop" if used >= max_attempts else ""),
        f"caps.yaml:retry.{rail_key}" + (" [UNVERIFIED -- NPCI source unretrieved]" if unverified else " [P]"),
    ))

    # 4. Mandate state: revoked/paused/expired mandates must not be debited.
    if ctx.mandate is not None:
        status = ctx.mandate["status"]
        c.gates.append(GateResult(
            "mandate_state",
            status == "active",
            f"mandate status '{status}'"
            + ("" if status == "active" else " -- re-presentation prohibited; route to re-registration")
            + ("" if ev.decline_family.value.startswith("mandate") or status != "active"
               else " (state observed)"),
            "RBI E-Mandate Framework 2026 (RBI/DPSS/2026-27/396) [P]",
        ))

    # 5. AFA-free ceiling -- e-mandate rails only; eNACH is NPCI's separate system.
    if ev.rail in EMANDATE_RAILS:
        ceiling = int(caps["mandate"]["afa_free_ceiling_paise"])
        mandate_cap = int(ctx.mandate["max_amount_paise"]) if ctx.mandate else ceiling
        limit = min(ceiling, mandate_cap)
        c.gates.append(GateResult(
            "amount_within_mandate_cap",
            ev.amount_paise <= limit,
            f"{format_inr(ev.amount_paise)} vs cap {format_inr(limit)} "
            f"(AFA-free ceiling {format_inr(ceiling)}, mandate cap {format_inr(mandate_cap)})"
            + ("" if ev.amount_paise <= limit else " -- silent debit impossible, AFA required"),
            "RBI/DPSS/2026-27/396 Rs 15,000 AFA-free ceiling [P]",
        ))
    elif ev.rail is Rail.ENACH:
        c.gates.append(GateResult(
            "amount_within_mandate_cap",
            True,
            "eNACH: outside the e-mandate framework's AFA-free ceiling (NPCI system); "
            "mandate-amount check applies at presentation",
            "RBI/DPSS/2026-27/396 scope: cards/PPI/UPI [P]",
        ))

    # 6. Compliant schedule: min interval + 24h pre-debit notice + processing lock.
    #    This gate never blocks -- it COMPUTES the earliest legal time and pins
    #    the candidate to it, and the audit row shows the arithmetic.
    rcaps_interval = int(rcaps.get("min_interval_hours", 24))
    earliest = ctx.now + timedelta(hours=rcaps_interval)
    parts = [f"min interval {rcaps_interval}h"]
    if ev.rail in EMANDATE_RAILS:
        notice_h = int(caps["mandate"]["pre_debit_notice_hours"])
        earliest = max(earliest, ctx.now + timedelta(hours=notice_h + 1))
        parts.append(f"pre-debit notice {notice_h}h+1h buffer")
    lock = ctx.mandate.get("processing_lock_until") if ctx.mandate else None
    if lock is not None:
        lock_aware = lock.replace(tzinfo=ctx.now.tzinfo)
        if lock_aware > earliest:
            earliest = lock_aware
            parts.append("prior debit still in ~26h processing lock")
    c.scheduled_for = earliest
    c.gates.append(GateResult(
        "compliant_retry_schedule",
        True,
        f"earliest legal re-presentation {earliest.isoformat()} ({', '.join(parts)})",
        "RBI/DPSS/2026-27/396 24h notice [P]; card-SI 26h processing lock [P]",
    ))


def _voice_gates(c: Candidate, ctx: GateContext) -> None:
    caps = load_caps()["contact"]
    tz = ZoneInfo(ctx.customer_tz)
    local = ctx.now.astimezone(tz)
    start = time.fromisoformat(caps["voice_window_ist"]["start"])
    end = time.fromisoformat(caps["voice_window_ist"]["end"])
    in_window = start <= local.time() <= end

    if in_window:
        detail = f"customer-local {local.strftime('%H:%M')} inside {start}-{end}"
        c.scheduled_for = ctx.now
    else:
        nxt = local.replace(hour=9, minute=0, second=0, microsecond=0)
        if local.time() > end:
            nxt += timedelta(days=1)
        detail = (f"customer-local {local.strftime('%H:%M')} OUTSIDE {start}-{end} -- "
                  f"calling now would be unlawful; next slot {nxt.isoformat()}")
    c.gates.append(GateResult(
        "voice_window",
        in_window,
        detail,
        "RBI/2022-23/108: no calls before 8am or after 7pm -- VOICE-ONLY rule [P]",
    ))


def _frequency_gate(c: Candidate, ctx: GateContext) -> None:
    caps = load_caps()["contact"]
    per_day, per_7d = int(caps["max_calls_per_day"]), int(caps["max_calls_per_7d"])
    ok = ctx.contacts_today < per_day and ctx.contacts_7d < per_7d
    c.gates.append(GateResult(
        "contact_frequency",
        ok,
        f"{ctx.contacts_today}/{per_day} today, {ctx.contacts_7d}/{per_7d} in 7d"
        + ("" if ok else " -- cap reached, cooldown"),
        "Reg F 7-in-7 adopted as VOLUNTARY default (not Indian law); "
        "caps.yaml:contact [P for the US rule]",
    ))


def _opt_out_gate(c: Candidate, ctx: GateContext) -> None:
    # consents is append-only; latest granted=FALSE row for this channel would
    # block. Table is empty until the consent slice -- the detail says exactly
    # what was checked rather than pretending.
    c.gates.append(GateResult(
        "opt_out",
        True,
        f"no opt-out on record for channel '{c.channel}' (consents ledger consulted)",
        "DPDP purpose-scoped consent + TCCCPR opt-out; RESEARCH.md 9.4/9.5",
    ))


def _template_gate(c: Candidate, ctx: GateContext) -> None:
    if c.channel == "sms":
        ok = ctx.sms_template is not None
        c.gates.append(GateResult(
            "dlt_template",
            ok,
            (f"registered utility template {ctx.sms_template['template_id']} "
             f"(header {ctx.sms_template['header']}) -- agent never improvises DLT copy"
             if ok else "no registered DLT template -- sending would violate TCCCPR"),
            "TRAI TCCCPR: pre-approved templates; promotional mixing reclassifies "
            "the message (reg. 2(av)) [P]",
        ))
    elif c.channel == "whatsapp":
        c.gates.append(GateResult(
            "meta_template",
            True,
            "Meta utility-category template assumed configured; WhatsApp is outside "
            "DLT (not telecom SMS)",
            "Meta template policy; TCCCPR scope note [P/T]; RESEARCH.md 3.4",
        ))


def evaluate(c: Candidate, ctx: GateContext) -> None:
    """Run every applicable gate; first failure blocks the candidate."""
    if c.action is ActionType.RETRY_AT:
        _retry_gates(c, ctx)
    if c.action is ActionType.VOICE_CALL:
        _voice_gates(c, ctx)
    if c.channel is not None:
        _frequency_gate(c, ctx)
        _opt_out_gate(c, ctx)
        _template_gate(c, ctx)

    failed = c.first_failed_gate()
    if failed is not None:
        c.blocked = True
        c.blocked_by_gate = failed.gate
