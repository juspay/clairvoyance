"""
Update Outcome Handler

A built-in global function that allows the LLM to update the lead's call outcome
in the database at any point during the conversation — independent of node transitions.

Reuses the existing update_outcome_in_database hook (fire-and-forget via asyncio.create_task).
The handler returns immediately with {"status": "success"} so the LLM experiences
near-zero latency. The actual DB write happens in the background.
"""

import asyncio
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookRegistry
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.core.logger import logger


async def update_outcome(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Update the lead's outcome in the database (fire-and-forget).

    Delegates to the existing update_outcome_in_database hook with no
    expected_fields — the hook's fallback path uses args directly.
    Whatever the LLM passes (outcome, driver_id, driver_name, etc.)
    flows through the same hook logic that node-level functions use.

    Args:
        context: Handler context with bot state access
        args: LLM function arguments. Must include "outcome" (str).
              Any additional fields are stored in meta_data.outcome.*.

    Returns:
        Dict with status "success" (always, since DB write is async)
    """
    outcome = args.get("outcome")
    if not outcome:
        logger.warning("[update_outcome] No outcome provided in args, skipping")
        return {"status": "error", "error": "outcome argument is required"}

    hook = HookRegistry.get("update_outcome_in_database")
    if not hook:
        logger.error(
            "[update_outcome] update_outcome_in_database hook not found in registry"
        )
        return {"status": "error", "error": "hook not found"}

    # Empty expected_fields → hook falls back to using args directly
    hook_config = HookConfig(name="update_outcome_in_database")

    logger.info(
        f"[update_outcome] Scheduling background outcome update: "
        f"outcome='{outcome}' for lead {context.lead.id if context.lead else 'unknown'}"
    )

    asyncio.create_task(
        hook.safe_execute(context, args, "update_call_outcome", hook_config),
        name=f"update_outcome_{context.lead.id if context.lead else 'unknown'}_{outcome}",
    )

    return {"status": "success", "outcome": outcome}
