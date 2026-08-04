"""DIRECT ui_intent execution surface used by the intent router —
no LLM in the loop; persistence and state semantics mirror agent turns.

Method bodies are verbatim from the monolithic agent.py (split 2026-08-05);
this mixin holds no state of its own — every attribute lives on
``ChatAgent`` (see ``core``)."""

import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from pipecat.frames.frames import FunctionCallFromLLM

from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import (  # noqa: F401
    _CHIPS_NUDGE,
    _MAX_TOOL_CYCLES,
    _chip_labels,
    _KbMessage,
    _partition_gated_calls,
    _PreparedTools,
    _summarize_result,
    _tools_schema,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    apply_state_reducers,
    inject_tool_args,
)

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent


class DirectDispatchMixin:
    async def prepare_direct_dispatch(
        self: "ChatAgent", current_node: Optional[str]
    ) -> Tuple[_PreparedTools, Dict[str, Any]]:
        """Build the tool surface + resolve the node for a no-LLM dispatch.

        Same ``_prepare_tools`` the LLM loop uses — one pipeline, so direct
        intents inherit idempotency keys, transforms, and projections with
        no second cart-mutation semantics (RFC-001 §8). Caller must have
        set ``aiohttp_session`` / ``mcp_pool`` first (as ``run_turn`` does).
        """
        prep = await self._prepare_tools()
        node = self._resolve_node(prep.flow_config, current_node)
        return prep, node

    async def run_direct_tool(
        self: "ChatAgent",
        *,
        tool_name: str,
        args: Dict[str, Any],
        node: Dict[str, Any],
        prep: _PreparedTools,
        turn_id: str,
    ) -> Tuple[FunctionCallFromLLM, Any]:
        """Execute ONE tool through the existing pipeline, no LLM involved.

        inject_tool_args → dispatch (handler closures run
        apply_result_pipeline) → binding-store record → state reducers —
        the exact per-call sequence of ``_cycle_loop``. Returns ``(call,
        result_payload)``; ``call.arguments`` carries the post-injection
        args so persistence records exactly what ran (mirroring the
        approval-gate contract).
        """
        arg_injection_rules = (
            self.template.configurations.tool_arg_injection
            if self.template.configurations
            else []
        )
        injected_args = inject_tool_args(
            tool_name=tool_name,
            args=args,
            state_data=self.agent_state,
            chat_session_id=self.session_id,
            injections=arg_injection_rules,
            turn_id=turn_id,
        )
        call = FunctionCallFromLLM(
            function_name=tool_name,
            tool_call_id=f"intent_{uuid.uuid4().hex[:24]}",
            arguments=injected_args,
            context=None,
        )
        result_payload, _transition = await self._dispatch_tool_call(
            call, node, prep.global_funcs, injected_args=injected_args
        )
        result_payload = self._verify_result(tool_name, injected_args, result_payload)
        self._binding_store.record(tool_name, call.tool_call_id, result_payload)
        reducer_rules = (
            self.template.configurations.state_reducers
            if self.template.configurations
            else []
        )
        self.agent_state = apply_state_reducers(
            state_data=self.agent_state,
            tool_name=tool_name,
            tool_result=result_payload,
            reducers=reducer_rules,
        )
        return call, result_payload
