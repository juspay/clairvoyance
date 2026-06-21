"""
Human-in-the-loop approval gate for global functions.

A template author marks a global function (HTTP / builtin / custom) as gated
by adding an ``approval`` config to it (see ApprovalConfig in types.py). When
the LLM calls a gated function, execution is held until an explicit end-user
decision:

- Chat: ChatAgent gates pre-dispatch (Pattern B — the turn ends at the gate
  and the decision arrives on a dedicated endpoint), so the in-handler gate
  below is bypassed via the ``handles_approval_externally`` flag.
- Voice (Daily): the gate blocks in-process on the bot's ApprovalManager
  (Pattern C); the decision arrives as an RTVI client message.
- Telephony: no approval surface exists — ``approval.on_no_channel`` applies.
- Daily WITHOUT a working approval channel (e.g. RTVI disabled via
  ENABLE_BREEZE_BUDDY_DAILY_EVENTS=false): always DENY. The surface that is
  supposed to show an approval UI must never silently auto-execute.

Denials materialize as an LLM-visible tool result so the model can react.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    ApprovalConfig,
    ApprovalOnNoChannel,
    BaseGlobalFunction,
    GlobalFunctionType,
)
from app.core.logger import logger

# LLM-visible statuses for gated calls that did not execute. These are what
# the model reads in the tool result; the SSE/RTVI wire layer has its own
# enum (approved|denied|timeout|cancelled|superseded).
LLM_STATUS_DENIED = "denied"
LLM_STATUS_TIMEOUT = "timeout"
LLM_STATUS_NOT_DECIDED = "not_decided"

# Wire statuses (shared vocabulary with the SDK — see plan wire contract).
WIRE_STATUS_APPROVED = "approved"
WIRE_STATUS_DENIED = "denied"
WIRE_STATUS_TIMEOUT = "timeout"
WIRE_STATUS_CANCELLED = "cancelled"
WIRE_STATUS_SUPERSEDED = "superseded"

# Derived from the enum (mirrors builder.py:_GLOBAL_FUNCTION_TYPES) so a new
# GlobalFunctionType is gated by chat and voice in lockstep — a hardcoded set
# here would silently skip a 4th type that the builder/voice path still gates.
_GLOBAL_FUNCTION_TYPES = {t.value for t in GlobalFunctionType}


@dataclass
class ApprovalOutcome:
    """Result of a voice approval wait (see agent/approval.py)."""

    approved: bool
    status: str  # wire status: approved | denied | timeout | superseded
    reason: Optional[str] = None


def denial_result(status: str, reason: str) -> Dict[str, str]:
    """LLM-visible tool result for a gated call that did not execute."""
    return {"status": status, "reason": reason}


async def gate_call(
    bot_instance: Any,
    name: str,
    approval: Optional[ApprovalConfig],
    llm_args: Dict[str, Any],
    execute: Callable[[], Awaitable[Any]],
) -> Any:
    """Run ``execute()`` only after the approval gate for ``name`` clears.

    Channel-generic core shared by global functions (via
    :func:`gate_global_function`) and MCP tools (via the wrapper in
    ``mcp/__init__.py``). ``name`` is the LLM-visible call name; ``approval``
    is its ApprovalConfig (None => ungated => execute immediately).

    ``execute`` carries the full call side effects (filler audio, handler,
    post-actions / the real MCP round-trip) — a denied call therefore plays
    no filler and fires no side effects.

    CancelledError from the approval wait (user barge-in on a sync gated
    call, or pipeline teardown) propagates — pipecat's aggregator records
    the call as CANCELLED and never re-invokes the handler.
    """
    if approval is None:
        return await execute()

    # Chat gates pre-dispatch in ChatAgent._cycle_loop; checking this flag
    # FIRST prevents double-gating (ChatAgent never has an approval_manager,
    # but the explicit flag keeps the contract obvious).
    if getattr(bot_instance, "handles_approval_externally", False):
        return await execute()

    manager = getattr(bot_instance, "approval_manager", None)
    if manager is None:
        if getattr(bot_instance, "is_daily_mode", False):
            # Channel expected but unavailable (e.g. RTVI disabled by env
            # flag). Never auto-execute a gated call here.
            logger.error(
                f"[approval] '{name}' is approval-gated but this daily "
                "bot has no approval channel (RTVI unavailable — check "
                "ENABLE_BREEZE_BUDDY_DAILY_EVENTS). Denying."
            )
            return denial_result(LLM_STATUS_DENIED, "approval_channel_unavailable")

        # Telephony: no approval surface can exist.
        if approval.on_no_channel == ApprovalOnNoChannel.DENY:
            logger.warning(
                f"[approval] '{name}' denied on a channel with no "
                "approval surface (on_no_channel=deny)."
            )
            return denial_result(LLM_STATUS_DENIED, "no_approval_channel")

        logger.warning(
            f"[approval] EXECUTING approval-gated call '{name}' "
            "WITHOUT human approval — no approval surface on this channel "
            "(telephony) and on_no_channel=execute. "
            f"args_keys={sorted(llm_args.keys()) if llm_args else []}"
        )
        return await execute()

    # Voice gate: optional spoken announcement, then block on the decision.
    if approval.voice_announce:
        try:
            await TemplateContext(bot_instance).queue_tts_filler(
                approval.voice_announce
            )
        except Exception as e:
            logger.warning(f"[approval] Failed to queue voice_announce: {e}")

    outcome: ApprovalOutcome = await manager.request(name, llm_args, approval)
    if outcome.approved:
        return await execute()

    # Channel-consistent LLM statuses: superseded is NOT a refusal (a newer
    # request replaced this one — chat's supersede uses the same status so
    # the model never verbalizes "okay, I won't do X" for a moved-on call).
    if outcome.status == WIRE_STATUS_TIMEOUT:
        llm_status = LLM_STATUS_TIMEOUT
    elif outcome.status == WIRE_STATUS_SUPERSEDED:
        llm_status = LLM_STATUS_NOT_DECIDED
    else:
        llm_status = LLM_STATUS_DENIED
    return denial_result(
        llm_status,
        outcome.reason or "the user did not approve this action",
    )


async def gate_global_function(
    bot_instance: Any,
    func: BaseGlobalFunction,
    llm_args: Dict[str, Any],
    execute: Callable[[], Awaitable[Any]],
) -> Any:
    """Approval gate for a global function — thin shim over :func:`gate_call`.

    Reads the function's name + approval and delegates; keeps the existing
    call sites (``_make_global_wrapper``) unchanged.
    """
    return await gate_call(
        bot_instance, func.name, getattr(func, "approval", None), llm_args, execute
    )


def build_approval_map(flow: Any) -> Dict[str, ApprovalConfig]:
    """Map function name -> ApprovalConfig for every gated global function.

    Mirrors how GlobalFunctionRegistry discovers functions:
    - flow mode: top-level ``global_functions``
    - direct mode: flat ``functions`` entries with type http/builtin/custom,
      honoring the ``function_name`` -> ``name`` alias (builder.py routing)

    Also warns on unsupported placements so template authors are not
    silently surprised:
    - ``approval`` on a per-node function (not supported in v1, ignored)
    - a gated global whose name is shadowed by a per-node function (chat
      gates the call by name pre-dispatch, voice's wrapper gate only wraps
      globals — silent channel divergence)
    """
    approval_map: Dict[str, ApprovalConfig] = {}
    if not isinstance(flow, dict):
        return approval_map

    def _scan_entry(entry: Any, source: str) -> None:
        if not isinstance(entry, dict) or entry.get("approval") is None:
            return
        name = entry.get("name") or entry.get("function_name")
        if not name:
            logger.warning(
                f"[approval] Ignoring approval config on a nameless " f"{source} entry"
            )
            return
        try:
            approval_map[name] = ApprovalConfig.model_validate(entry["approval"])
        except Exception as e:
            logger.warning(
                f"[approval] Ignoring malformed approval config on "
                f"'{name}' ({source}): {e}"
            )

    for entry in flow.get("global_functions") or []:
        _scan_entry(entry, "global_functions")

    for entry in flow.get("functions") or []:
        if isinstance(entry, dict) and entry.get("type") in _GLOBAL_FUNCTION_TYPES:
            _scan_entry(entry, "functions (direct mode)")

    node_function_names = set()
    for node in flow.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for fn in node.get("functions") or []:
            if not isinstance(fn, dict):
                continue
            fn_name = fn.get("name") or fn.get("function_name")
            if fn_name:
                node_function_names.add(fn_name)
            if fn.get("approval") is not None:
                logger.warning(
                    f"[approval] 'approval' on per-node function "
                    f"'{fn_name}' is not supported in v1; ignored. Move the "
                    "function to global_functions to gate it."
                )

    for gated_name in approval_map:
        if gated_name in node_function_names:
            logger.warning(
                f"[approval] Gated global '{gated_name}' is shadowed by a "
                "per-node function of the same name. In nodes that define it, "
                "the per-node function runs UNGATED on BOTH chat and voice "
                "(per-node functions are not gateable in v1); the gate applies "
                "only in nodes where the global is reachable. Rename one to "
                "remove the ambiguity."
            )

    return approval_map


def build_client_tool_names(flow: Any) -> Set[str]:
    """Names of CLIENT-FULFILLED global functions (``client_fulfilled: true``).

    These are gated like approval functions — the chat turn pauses at the call —
    but resolved by the FRONTEND with DATA via the tool-result endpoint, not by a
    human decision. Scans the same surfaces as :func:`build_approval_map`
    (flow-mode ``global_functions`` + direct-mode ``functions``).
    """
    names: Set[str] = set()
    if not isinstance(flow, dict):
        return names

    def _scan(entry: Any) -> None:
        if not isinstance(entry, dict) or not entry.get("client_fulfilled"):
            return
        name = entry.get("name") or entry.get("function_name")
        if name:
            names.add(name)

    for entry in flow.get("global_functions") or []:
        _scan(entry)
    for entry in flow.get("functions") or []:
        if isinstance(entry, dict) and entry.get("type") in _GLOBAL_FUNCTION_TYPES:
            _scan(entry)
    return names
