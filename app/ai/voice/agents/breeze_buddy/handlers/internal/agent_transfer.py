"""connect_to_agent — agent-to-agent transfer builtin.

The LLM calls this to hand the live call off to another template mid-call. The
handler does NOT rebuild anything: it validates the target, snapshots handoff
state, sets ``bot.pending_transfer``, suppresses the serializer auto-hangup, and
ends the current pipeline task without dropping the call. ``Agent.run``'s
generation loop then rebuilds the pipeline from the target template via the same
cold-start code path.

Every failure returns an error result to the LLM *before* any teardown — a bad
target leaves the current agent fully healthy.
"""

from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader
from app.ai.voice.agents.breeze_buddy.template.types import (
    FlowMode,
    GlobalBuiltinFunction,
)
from app.ai.voice.agents.breeze_buddy.template.utils import validate_template_compat
from app.ai.voice.agents.breeze_buddy.utils.agent_transfer import (
    PendingAgentTransfer,
    suppress_auto_hangup,
)
from app.core.logger import logger


async def connect_to_agent(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalBuiltinFunction] = None,
) -> Any:
    """Hand the live call off to another agent template (full pipeline rebuild).

    Args:
        context: Handler context (bot state access).
        args: LLM function args — ``target`` (required alias) and optional
            ``handoff_summary``.
        function_config: This builtin's own function entry — carries the
            ``agent_transfer`` config (targets, max_transfers, context_mode).

    Returns:
        ``{"status": "transferring", "target": ...}`` once committed, or
        ``{"status": "error", "error": ...}`` (call continues on the current agent).
    """
    bot = context.bot

    # ── Guards: every failure returns an error result; the call continues. ──
    # Transfer config is declared on this function's own entry (single source of
    # truth), not on ConfigurationModel.
    cfg = function_config.agent_transfer if function_config else None
    if not cfg:
        return {"status": "error", "error": "agent_transfer not configured"}
    if bot.pending_transfer:
        return {"status": "error", "error": "transfer already in progress"}
    if bot.transfer_count >= cfg.max_transfers:
        return {"status": "error", "error": "max transfers reached for this call"}

    target_alias = args.get("target", "")
    target_template_id = cfg.targets.get(target_alias)
    if not target_template_id:
        return {
            "status": "error",
            "error": f"unknown target '{target_alias}'",
            "available_targets": list(cfg.targets.keys()),
        }

    # ── Load + render + validate the target BEFORE any teardown. ──
    # Same loader as call start: template_id resolves the target, reseller/merchant
    # re-apply scoping, {placeholder} vars come from the SAME lead payload.
    try:
        loader = FlowConfigLoader()
        template, template_vars = await loader.load_template(
            reseller_id=bot.lead.reseller_id,
            template=bot.lead.template,
            merchant_id=bot.lead.merchant_id,
            call_payload=bot.lead.payload,
            template_id=target_template_id,
        )
        validate_template_compat(template)
        if template.flow.get("mode") == FlowMode.IVR.value:
            return {"status": "error", "error": "cannot transfer to an IVR template"}
        if "voice" not in (template.supported_channels or ["voice"]):
            return {"status": "error", "error": "target does not support voice"}
    except Exception as e:
        logger.error(f"[agent_transfer] target load failed: {e}", exc_info=True)
        return {"status": "error", "error": f"target template unavailable: {e}"}

    # ── Build handoff messages per context_mode. ──
    handoff: List[Dict[str, str]] = []
    summary = (args.get("handoff_summary") or "").strip()
    if cfg.context_mode == "full" and bot.context:
        handoff = [
            m
            for m in bot.context.messages
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]
    if cfg.context_mode in ("summary", "full"):
        note = "This call was just transferred to you from another agent." + (
            f" Handoff summary: {summary}" if summary else ""
        )
        handoff.append({"role": "system", "content": note})

    # ── Commit: from here the current pipeline is going down. ──
    bot.pending_transfer = PendingAgentTransfer(
        template=template,
        template_vars=template_vars,
        handoff_messages=handoff,
    )
    suppress_auto_hangup(bot.transport)
    logger.info(
        f"[agent_transfer] {bot.template.name} -> {template.name} "
        f"(call {context.call_sid}, generation {bot.generation})"
    )
    await bot.task.stop_when_done()  # EndFrame: drains in-flight, then stops

    # Return a 2-tuple (result, inert node) so pipecat-flows treats this as an
    # edge function and sets run_llm=False — otherwise the current agent's LLM
    # runs one more turn on this result and speaks again (e.g. the orchestrator
    # re-greets) before the EndFrame drains. The node never actually runs: no
    # messages/functions and respond_immediately=False, and the pipeline ends first.
    transferring_node = {
        "name": "__transferring__",
        "role_messages": [],
        "task_messages": [],
        "functions": [],
        "pre_actions": [],
        "post_actions": [],
        "respond_immediately": False,
    }
    return {"status": "transferring", "target": template.name}, transferring_node
