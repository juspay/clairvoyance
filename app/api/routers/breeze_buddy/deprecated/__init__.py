"""
Deprecated endpoints for backward compatibility.

This module contains all legacy endpoints that are maintained for backward compatibility
but are deprecated in favor of newer RESTful endpoints.

All endpoints in this module will:
- Log deprecation warnings
- Include deprecation notices in documentation
- Function identically to their original implementations
- Eventually be removed in a future major version

Deprecation Timeline:
- Phase 1 (Current): Mark as deprecated, log warnings, add migration guides
- Phase 2 (Future): Add sunset headers with removal date
- Phase 3 (Future): Remove deprecated endpoints entirely

For new implementations, please use the modern RESTful endpoints in the parent routers.
"""

from fastapi import APIRouter

from .leads import router as deprecated_leads_router
from .outbound_numbers import router as deprecated_numbers_router

# Create main deprecated router
router = APIRouter(tags=["deprecated"])

# Include all deprecated sub-routers
router.include_router(deprecated_leads_router, prefix="")
router.include_router(deprecated_numbers_router, prefix="")
