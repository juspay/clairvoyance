"""Utility functions for observers."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookConfig, HookRegistry
from app.ai.voice.agents.breeze_buddy.template.types import (
    ActionType,
    FieldConfig,
    FieldSource,
    FlowAction,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_results,
)


def is_alert_action(action: Optional[FlowAction]) -> bool:
    """Alerts are notifications, not terminal actions — they never end a call."""
    return action is not None and action.type == ActionType.ALERT


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


async def record_detection(
    agent_context: Any,
    evaluation_config_id: Optional[str],
    observer_name: str,
    detection: Dict[str, Any],
) -> None:
    """Persist one observer detection to ``evaluation_result``.

    Best-effort: an analytics write must never stop the observer's real action,
    so every failure path returns quietly.

    ``evaluation_config_id`` is a NOT NULL FK, so observers still coming from
    the legacy template JSON have no row to point at and cannot be stored.
    """
    lead = getattr(agent_context, "lead", None)
    template = getattr(agent_context, "template", None)
    template_id = getattr(lead, "template_id", None) or getattr(template, "id", None)
    if not lead or not template_id:
        return

    source_id = str(getattr(lead, "id", None) or getattr(lead, "call_id", ""))
    reseller_id = getattr(lead, "reseller_id", None)
    if not source_id or not reseller_id:
        return

    if not evaluation_config_id:
        logger.warning(
            f"Observer {observer_name} detected but has no evaluation_config row; "
            "detection not recorded"
        )
        return

    detected_at = datetime.now(timezone.utc)
    try:
        await save_evaluation_results(
            evaluation_config_id=evaluation_config_id,
            evaluation_type="OBSERVER",
            source_id=source_id,
            reseller_id=str(reseller_id),
            merchant_id=getattr(lead, "merchant_id", None),
            template_id=str(template_id),
            started_at=getattr(lead, "call_initiated_time", None) or detected_at,
            results=[detection],
        )
    except Exception:
        logger.exception(f"Failed to record observer detection for {observer_name}")
