"""Buddy Assist HTTP surface: onboarding (install + stream), the blueprint, and research."""

from app.api.routers.breeze_buddy.assist.blueprint import router as blueprint_router
from app.api.routers.breeze_buddy.assist.onboarding import router as onboarding_router
from app.api.routers.breeze_buddy.assist.research import router as research_router

__all__ = ["blueprint_router", "onboarding_router", "research_router"]
