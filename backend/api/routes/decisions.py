"""Read API for the dashboard: feed, drawer, blocked ledger, money strip."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...repositories import decision_repository, session

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("")
def feed(limit: int = Query(50, ge=1, le=500)):
    with session() as conn:
        return {"decisions": decision_repository.list_decisions(conn, limit)}


@router.get("/blocked")
def blocked(limit: int = Query(100, ge=1, le=1000)):
    """Everything the agent refused to do, and which rule said so."""
    with session() as conn:
        return {"blocked": decision_repository.blocked_ledger(conn, limit)}


@router.get("/metrics")
def metrics():
    with session() as conn:
        m = decision_repository.metrics(conn)
    m["cohort_note"] = "test-mode + synthetic"
    return m


@router.get("/{decision_id}")
def drawer(decision_id: str):
    """The decision drawer: diagnosis, every candidate with EV, every gate
    with its rule_source."""
    with session() as conn:
        d = decision_repository.get_decision(conn, decision_id)
    if d is None:
        raise HTTPException(404, "no such decision")
    return d
