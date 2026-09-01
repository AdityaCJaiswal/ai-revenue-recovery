"""HTTP layer. Aggregates route modules into a single router."""

from fastapi import APIRouter

from .routes import admin, decisions, health, voice, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(webhooks.router)
api_router.include_router(admin.router)
api_router.include_router(decisions.router)
api_router.include_router(voice.router)

__all__ = ["api_router"]
