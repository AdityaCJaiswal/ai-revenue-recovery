"""Writers for voice_calls, voice_utterances, promises_to_pay."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pymysql

Connection = pymysql.connections.Connection


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def start_call(conn: Connection, *, action_id: str, asr_model: str, tts_model: str,
               now: datetime) -> str:
    call_id = f"vc_{uuid.uuid4().hex[:16]}"
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO voice_calls (id, action_id, started_at, asr_model, tts_model)
               VALUES (%s,%s,%s,%s,%s)""",
            (call_id, action_id, _utc(now), asr_model, tts_model),
        )
    conn.commit()
    return call_id


def mark_disclosure(conn: Connection, call_id: str, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE voice_calls SET ai_disclosure_given_at=%s WHERE id=%s AND ai_disclosure_given_at IS NULL",
            (_utc(now), call_id),
        )
    conn.commit()


def log_utterance(conn: Connection, *, call_id: str, turn_index: int, speaker: str,
                  drafted: str | None, spoken: str | None, verdict: str | None,
                  blocked_reason: str | None, latency_ms: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO voice_utterances
                   (voice_call_id, turn_index, speaker, drafted_text, spoken_text,
                    screening_verdict, blocked_reason, latency_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (call_id, turn_index, speaker, drafted, spoken, verdict,
             blocked_reason, latency_ms),
        )
    conn.commit()


def end_call(conn: Connection, call_id: str, *, disposition: str,
             distress: bool, dispute: bool, handoff: bool, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE voice_calls SET ended_at=%s,
                   duration_seconds=TIMESTAMPDIFF(SECOND, started_at, %s),
                   disposition=%s, distress_detected=%s, dispute_raised=%s,
                   handoff_to_human_at=CASE WHEN %s THEN %s ELSE handoff_to_human_at END
               WHERE id=%s""",
            (_utc(now), _utc(now), disposition, distress, dispute, handoff, _utc(now), call_id),
        )
    conn.commit()


def create_ptp(conn: Connection, *, obligation_ref: str, call_id: str,
               amount_paise: int, promised_for: str, verbatim: str,
               confidence_bp: int, readback_confirmed: bool) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM obligations WHERE external_ref=%s", (obligation_ref,))
        ob = cur.fetchone()
        if ob is None:
            return None
        ptp_id = f"ptp_{uuid.uuid4().hex[:16]}"
        cur.execute(
            """INSERT INTO promises_to_pay
                   (id, obligation_id, voice_call_id, amount_paise, promised_for_date,
                    verbatim, extraction_confidence_bp, readback_confirmed)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ptp_id, ob["id"], call_id, amount_paise, promised_for, verbatim,
             confidence_bp, readback_confirmed),
        )
    conn.commit()
    return ptp_id


def call_view(conn: Connection, call_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM voice_calls WHERE id=%s", (call_id,))
        call = cur.fetchone()
        if call is None:
            return None
        cur.execute(
            "SELECT * FROM voice_utterances WHERE voice_call_id=%s ORDER BY turn_index",
            (call_id,),
        )
        call["utterances"] = list(cur.fetchall())
        cur.execute("SELECT * FROM promises_to_pay WHERE voice_call_id=%s", (call_id,))
        call["promises"] = list(cur.fetchall())
    return call
