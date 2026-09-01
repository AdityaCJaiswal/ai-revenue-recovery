"""Operator endpoints: seed synthetic batches, inspect stored events."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...core.logging import get_logger
from ...repositories import event_repository, session
from ...services.decision_service import process_batch
from ...services.execution_service import execute_batch
from ...services.simulation_service import advance_days
from ...services.generator_service import generate_batch
from ...services.ingestion_service import ingest_events
from ...utils.money import format_inr

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/generate")
def generate(
    count: int = Query(500, ge=1, le=10_000),
    seed: int = Query(42),
):
    """Load a synthetic batch. Identical storage path to a real webhook."""
    events = generate_batch(count=count, seed=seed)
    saved = ingest_events(events)
    with session() as conn:
        total = event_repository.count(conn)
    log.info("generated %d, saved %d (%d duplicates)", len(events), saved, len(events) - saved)
    return {
        "generated": len(events),
        "saved": saved,
        "duplicates": len(events) - saved,
        "total_events": total,
        "cohort": "synthetic",
    }


@router.post("/process")
def process(limit: int = Query(1000, ge=1, le=10_000)):
    """Run the diagnose -> gate -> score pipeline over unprocessed events."""
    return process_batch(limit=limit)


@router.post("/execute")
def execute(limit: int = Query(2000, ge=1, le=10_000)):
    """Reserve -> conversion-check -> submit -> persist, exactly once."""
    return execute_batch(limit=limit)


@router.post("/simulate")
def simulate(days: int = Query(3, ge=1, le=30)):
    """Advance the simulated clock: outcomes land, PTPs resolve, control arm
    recovers organically. Deterministic -- same state in, same state out."""
    return advance_days(days=days)


@router.get("/events")
def list_events(limit: int = Query(50, ge=1, le=1_000)):
    with session() as conn:
        events = event_repository.list_recent(conn, limit)
        at_risk = event_repository.revenue_at_risk_paise(conn)
    return {
        "events": events,
        "revenue_at_risk_paise": at_risk,
        "revenue_at_risk_display": format_inr(at_risk),
        # Never let a rupee figure read as production revenue.
        "cohort_note": "test-mode + synthetic",
    }
