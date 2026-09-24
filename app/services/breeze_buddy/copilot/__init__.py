"""Buddy Copilot backend foundation."""

from app.services.breeze_buddy.copilot.scope import (
    CopilotScopeError,
    resolve_copilot_scope,
    validate_persisted_copilot_scope_access,
)

__all__ = [
    "CopilotScopeError",
    "resolve_copilot_scope",
    "validate_persisted_copilot_scope_access",
]
