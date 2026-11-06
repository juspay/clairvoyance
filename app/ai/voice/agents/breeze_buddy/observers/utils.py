"""Utility functions for observers."""

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookConfig, HookRegistry
from app.ai.voice.agents.breeze_buddy.template.types import FieldConfig, FieldSource
from app.core.logger import logger


async def set_outcome(
    context: TemplateContext, outcome: str, triggered_by: str = ""
) -> None:
    """Set call outcome via update_outcome_in_database hook.

    Same path template function hooks use (confirm_order → CONFIRM, etc.).

    Args:
        context: TemplateContext for the current call
        outcome: Outcome value (e.g., "VOICEMAIL", "HALLUCINATION")
        triggered_by: Who triggered this (e.g., "voicemail_detector")
    """
    hook = HookRegistry.get("update_outcome_in_database")
    if not hook:
        logger.warning(
            f"set_outcome: 'update_outcome_in_database' hook not found, "
            f"outcome '{outcome}' not persisted"
        )
        return
    hook_config = HookConfig(
        name="update_outcome_in_database",
        expected_fields={
            "outcome": FieldConfig(source=FieldSource.STATIC, value=outcome)
        },
    )
    await hook.safe_execute(
        context,
        {"outcome": outcome},
        triggered_by or "set_outcome",
        hook_config,
    )
