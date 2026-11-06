"""Buddy Copilot backend foundation."""

from app.services.breeze_buddy.copilot.scope import (
    CopilotScopeError,
    resolve_copilot_scope,
)

__all__ = [
    "CopilotScopeError",
    "resolve_copilot_scope",
]
