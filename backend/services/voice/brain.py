"""The call brain: what the agent says next.

Two implementations behind one function signature:
- RuleBrain: deterministic Hinglish state machine. Runs with ZERO keys, fully
  reproducible on stage -- the wifi-dead fallback AND the pre-credentials path.
- (Sarvam LLM brain plugs into the same seam once keys land -- the pipeline,
  screening, and PTP flow do not change.)

Pipeline rules from RESEARCH.md 8: agent lines carry Devanagari for the TTS
path; amounts and dates are expanded to words before TTS; PTP extraction is
followed by a spoken READ-BACK confirmation -- the correctness check that is
also the compliance artefact (8.6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ...utils.money import format_inr
from .screening import CustomerSignals

DISCLOSURE = (
    "नमस्ते! मैं Razorpay Revenue Recovery की ओर से एक AI सहायक बोल रहा हूँ। "
    "यह कॉल गुणवत्ता के लिए रिकॉर्ड हो सकती है।"
)


@dataclass(slots=True)
class CallState:
    stage: str = "greet"          # greet -> reason -> offer -> ptp_date -> readback -> close
    ptp_amount_paise: int = 0
    ptp_date: str | None = None   # ISO
    ptp_verbatim: str = ""
    readback_confirmed: bool = False
    turns: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrainReply:
    text: str
    stage: str
    capture_ptp: bool = False
    end_call: bool = False
    disposition: str | None = None


_YES = re.compile(r"\b(haan|ha\b|yes|sahi|theek|ok|okay|bilkul|kar (dunga|dungi)|pakka|ji\b|जी|हाँ|ठीक)\b", re.I)
_NO = re.compile(r"\b(nahi|nahin|no\b|nhi|नहीं|galat)\b", re.I)

# "pandrah tarikh" / "15 tarikh" / "kal" / "parso" / "salary ke baad" -> a date.
_DIGIT_DATE = re.compile(r"\b([0-9]{1,2})\s*(?:tarikh|tareekh|ko|th|st|nd|rd)?\b")
_HINDI_NUMS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5, "panch": 5, "chhe": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12,
    "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17,
    "atharah": 18, "unnis": 19, "bees": 20, "pachchis": 25, "tees": 30,
}


def extract_promise_date(text: str, today: date) -> tuple[str | None, int]:
    """Code-mixed date extraction. Returns (ISO date, confidence bp).

    No vendor solves this at the API layer (RESEARCH.md 8.6) -- and no regex
    fully does either, which is exactly why the READ-BACK step exists. This
    handles the common Hinglish forms; everything else asks again.
    """
    t = text.lower()
    if re.search(r"\b(aaj|today|abhi)\b", t):
        return today.isoformat(), 9000
    if re.search(r"\b(kal|tomorrow)\b", t):
        return (today + timedelta(days=1)).isoformat(), 8500
    if re.search(r"\b(parso|day after)\b", t):
        return (today + timedelta(days=2)).isoformat(), 8000
    # An EXPLICIT date always outranks the salary heuristic: "pandrah tarikh ko
    # salary aane ke baad" means the 15th, with salary as the reason.
    for word, num in _HINDI_NUMS.items():
        if re.search(rf"\b{word}\b\s*(tarikh|tareekh|ko)", t):
            return _day_to_date(num, today), 7500
    m = _DIGIT_DATE.search(t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            return _day_to_date(day, today), 7000
    if re.search(r"\bsalary\b.*\b(baad|after|aane)\b|\b(month end|mahine ke (ant|end))\b", t):
        # salary-cycle heuristic (no explicit date): 1st of next month
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return nxt.isoformat(), 6000
    return None, 0


def _day_to_date(day: int, today: date) -> str:
    """Nearest future occurrence of that day-of-month."""
    try:
        candidate = today.replace(day=day)
    except ValueError:
        candidate = (today.replace(day=1) + timedelta(days=32)).replace(day=min(day, 28))
    if candidate < today:
        candidate = (candidate.replace(day=1) + timedelta(days=32)).replace(day=min(day, 28))
    return candidate.isoformat()


_UNITS = ["", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ"]


def rupees_in_words(paise: int) -> str:
    """Amounts are expanded to words before TTS -- number normalisation is the
    weak point of every TTS surveyed (RESEARCH.md 8.4). Hybrid Hindi phrasing."""
    r = paise // 100
    if r >= 100000:
        lakh, rest = divmod(r, 100000)
        out = f"{lakh} लाख"
        if rest >= 1000:
            out += f" {rest // 1000} हज़ार"
        return out + " रुपये"
    if r >= 1000:
        thousands, rest = divmod(r, 1000)
        out = f"{thousands} हज़ार"
        if rest:
            out += f" {rest}"
        return out + " रुपये"
    return f"{r} रुपये"


def _date_in_words(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} तारीख़"


class RuleBrain:
    """Deterministic Hinglish collections dialogue. Every line it drafts still
    passes the screening gate -- the brain is not trusted either."""

    def reply(self, state: CallState, customer_text: str | None,
              signals: CustomerSignals | None, ctx: dict[str, Any],
              today: date) -> BrainReply:
        amount_words = rupees_in_words(ctx["amount_paise"])
        product = ctx.get("product", "आपकी सदस्यता")

        if signals is not None and signals.cease:
            return BrainReply(
                "ठीक है, मैं समझ गया। हम इस नंबर पर दोबारा कॉल नहीं करेंगे। धन्यवाद।",
                "close", end_call=True, disposition="opt_out")
        if signals is not None and signals.needs_handoff:
            return BrainReply(
                "मैं समझता हूँ, यह ज़रूरी है। मैं आपको अभी अपने सीनियर सहकर्मी से जोड़ रहा हूँ "
                "जो इसे ठीक से देखेंगे। धन्यवाद।",
                "close", end_call=True, disposition="handoff")

        if state.stage == "greet":
            return BrainReply(
                f"{DISCLOSURE} {product} का {amount_words} का भुगतान पूरा नहीं हो पाया है। "
                "क्या अभी बात करने का सही समय है?",
                "reason")
        if state.stage == "reason":
            if customer_text and _NO.search(customer_text) and not _YES.search(customer_text):
                return BrainReply(
                    "कोई बात नहीं। मैं आपको WhatsApp पर एक सुरक्षित payment link भेज देता हूँ, "
                    "आप अपनी सुविधा से भुगतान कर सकते हैं। धन्यवाद!",
                    "close", end_call=True, disposition="link_sent")
            return BrainReply(
                f"धन्यवाद। {amount_words} का भुगतान कब तक कर पाएँगे? "
                "आप तारीख़ बता दीजिए, मैं उसी दिन का reminder सेट कर दूँगा।",
                "ptp_date")
        if state.stage == "ptp_date":
            iso, conf = extract_promise_date(customer_text or "", today)
            if iso is None:
                return BrainReply(
                    "माफ़ कीजिए, तारीख़ स्पष्ट नहीं हुई। कृपया बताइए — किस तारीख़ तक "
                    "भुगतान कर पाएँगे? जैसे पंद्रह तारीख़, या कल।",
                    "ptp_date")
            state.ptp_date = iso
            state.ptp_verbatim = customer_text or ""
            state.meta["confidence_bp"] = conf
            # READ-BACK: "pandrah tareekh, yaani 15 tarikh — sahi hai?"
            return BrainReply(
                f"समझ गया — {_date_in_words(iso)}, यानी {iso} तक {amount_words}। सही है?",
                "readback")
        if state.stage == "readback":
            if customer_text and _YES.search(customer_text):
                state.readback_confirmed = True
                return BrainReply(
                    f"बहुत बढ़िया! {_date_in_words(state.ptp_date or '')} का promise नोट कर लिया है। "
                    "मैं WhatsApp पर payment link भी भेज रहा हूँ। समय देने के लिए धन्यवाद!",
                    "close", capture_ptp=True, end_call=True, disposition="ptp")
            return BrainReply(
                "ठीक है, फिर से बताइए — किस तारीख़ तक भुगतान कर पाएँगे?",
                "ptp_date")
        return BrainReply("धन्यवाद, आपका दिन शुभ हो!", "close", end_call=True,
                          disposition="completed")
