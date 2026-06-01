"""
Workflow Engine for Dynamic Flow Configuration

This package provides the core infrastructure for loading, building, and executing
dynamic conversation flows from database configurations.
"""

from app.ai.voice.agents.breeze_buddy.template.builder import (
    FlowConfigBuilder,
)
from app.ai.voice.agents.breeze_buddy.template.context import (
    TemplateContext,
    with_context,
)
from app.ai.voice.agents.breeze_buddy.template.hooks import (
    Hook,
    HookRegistry,
    UpdateOutcomeInDatabaseHook,
)
from app.ai.voice.agents.breeze_buddy.template.transition import (
    transition_handler,
)

__all__ = [
    "FlowConfigBuilder",
    "Hook",
    "HookRegistry",
    "TemplateContext",
    "UpdateOutcomeInDatabaseHook",
    "transition_handler",
    "with_context",
]
