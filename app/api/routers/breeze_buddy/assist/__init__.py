"""Buddy Assist HTTP surface: onboarding (install + stream), the blueprint, the
probe, and research."""

from app.api.routers.breeze_buddy.assist.blueprint import router as blueprint_router
from app.api.routers.breeze_buddy.assist.brand import router as brand_router
from app.api.routers.breeze_buddy.assist.fields import router as fields_router
from app.api.routers.breeze_buddy.assist.onboarding import router as onboarding_router
from app.api.routers.breeze_buddy.assist.probe import router as probe_router
from app.api.routers.breeze_buddy.assist.research import router as research_router

__all__ = [
    "blueprint_router",
    "brand_router",
    "fields_router",
    "onboarding_router",
    "probe_router",
    "research_router",
]
