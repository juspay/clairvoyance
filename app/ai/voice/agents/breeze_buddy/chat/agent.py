"""ChatAgent — direct LLM driver for one chat turn.

Stateless per turn: a fresh ``ChatAgent`` is constructed per ``POST /message``,
drives one user → assistant turn, then is discarded. The agent is a thin loop
around :func:`llm_driver.stream` — see ``docs/CHAT_MODE.md`` §4 + §9 for the
full architecture.
"""

import json
from typing import Any, AsyncIterator, Dict, List, Optional, cast

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat import llm_driver
from app.ai.voice.agents.breeze_buddy.chat.disabled import CHAT_DISABLED_NAMES
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import with_context
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.utils import render_messages_with_vars
from app.ai.voice.agents.breeze_buddy.utils.language_utils.prompt_injections import (
    inject_language_rules,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.chat_session import (
    insert_chat_message,
    update_chat_session_after_turn,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole

# Each tool-call → handler → re-invoke counts as one cycle. Real flows rarely
# cross 3; this guard stops a pathological template (handler always returns
# a transition that loops back) from burning unbounded LLM calls.
_MAX_TOOL_CYCLES = 8


class ChatAgent:
    """Single-turn driver. Construct, ``run_turn``, discard."""

    def __init__(
        self,
        *,
        session_id: str,
        template: TemplateModel,
        llm: Any,
        template_vars: Optional[dict] = None,
    ) -> None:
        self.session_id = session_id
        self.template = template
        self.template_vars = template_vars or {}
        self._llm = llm
        # TemplateContext reads these off the bot. flow_config is set in
        # run_turn; vad_analyzer=None lets template/{vad,interruption}.py
        # bare-attribute checks short-circuit; lead/call_sid stay None
        # because chat has no call lifecycle. aiohttp_session is required
        # by http_function_handler — held per-turn and closed in run_turn's
        # finally so the transient ClientSession's connector doesn't leak.
        self.flow_config: Optional[Dict[str, Any]] = None
        self.vad_analyzer = None
        self.lead = None
        self.call_sid: Optional[str] = None
        self.aiohttp_session: Optional[Any] = None

    async def run_turn(
        self,
        *,
        user_content: str,
        history: List[Dict[str, Any]],
        current_node: Optional[str],
    ) -> AsyncIterator[SSEEvent]:
        # Per-turn aiohttp session for global HTTP function calls. Created
        # lazily, closed in finally below so the ClientSession's TCP
        # connector is always released — even if the generator is closed
        # mid-stream by the SSE client disconnecting.
        self.aiohttp_session = create_aiohttp_session()
        try:
            async for event in self._run_turn_inner(
                user_content=user_content,
                history=history,
                current_node=current_node,
            ):
                yield event
        finally:
            if self.aiohttp_session is not None:
                await self.aiohttp_session.close()
                self.aiohttp_session = None

    async def _run_turn_inner(
        self,
        *,
        user_content: str,
        history: List[Dict[str, Any]],
        current_node: Optional[str],
    ) -> AsyncIterator[SSEEvent]:
        flow_builder = FlowConfigBuilder(disabled_names=CHAT_DISABLED_NAMES, quiet=True)
        # Wrap before build_flow_config: _build_function_schema captures the
        # handler reference into a closure, so post-build wrapping is a no-op.
        for handler_name, handler_func in flow_builder.handler_map.items():
            flow_builder.handler_map[handler_name] = with_context(self)(handler_func)
        flow_config = flow_builder.build_flow_config(self.template)
        self.flow_config = flow_config

        # Global functions live alongside per-node ones for the LLM. In direct
        # mode this is the channel for *any* tools (the synthesized node has
        # no per-node functions); in flow mode it's the always-available
        # cross-node tool set. ``build_global_functions`` already runs
        # ``filter_disabled_identifiers`` against CHAT_DISABLED_NAMES, so
        # voice-only entries (warm_transfer, end_conversation, etc.) never
        # reach the LLM in chat. ``bot_instance=self`` mirrors voice so global
        # function adapters that need the bot for post-action context resolve
        # against the ChatAgent (which carries the same flow_config / lead /
        # call_sid / vad_analyzer attribute surface those adapters read).
        global_funcs: List[FlowsFunctionSchema] = flow_builder.build_global_functions(
            self.template.flow, bot_instance=self
        )

        node = self._resolve_node(flow_config, current_node)
        node_name = cast(str, node["name"])

        # Persist user message before the LLM call so a crash mid-stream
        # still leaves the user's input in history.
        user_msg = await insert_chat_message(
            session_id=self.session_id,
            role=ChatMessageRole.USER,
            content=user_content,
        )
        yield SSEEvent(
            event="user_committed",
            data={"idx": user_msg.idx if user_msg else None, "content": user_content},
        )

        context = self._seed_context(node, history, user_content, global_funcs)

        assistant_text_chunks: List[str] = []
        for cycle in range(1, _MAX_TOOL_CYCLES + 1):
            tool_calls: List[FunctionCallFromLLM] = []
            turn_text: List[str] = []

            async for kind, payload in llm_driver.stream(
                self._llm, context, log_label=f"chat#{self.session_id[:8]}"
            ):
                if kind == "text":
                    text = cast(str, payload)
                    turn_text.append(text)
                    yield SSEEvent(event="assistant_token", data={"delta": text})
                elif kind == "tool_call":
                    call = cast(FunctionCallFromLLM, payload)
                    tool_calls.append(call)
                    yield SSEEvent(
                        event="function_call_started",
                        data={
                            "name": call.function_name,
                            "args": dict(call.arguments),
                            "tool_call_id": call.tool_call_id,
                        },
                    )

            assistant_text_chunks.extend(turn_text)
            if not tool_calls:
                break

            # Mirror LLMAssistantContextAggregator: append assistant message
            # carrying tool_calls, then a tool-result per call. Universal
            # OpenAI shape; per-provider adapter converts on next request.
            context.add_message(
                cast(
                    LLMContextMessage,
                    {
                        "role": "assistant",
                        "content": "".join(turn_text) or None,
                        "tool_calls": [
                            {
                                "id": call.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": call.function_name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in tool_calls
                        ],
                    },
                )
            )

            next_node: Optional[Dict[str, Any]] = None
            for call in tool_calls:
                result_payload, transition_node = await self._dispatch_tool_call(
                    call, node, global_funcs
                )
                context.add_message(
                    cast(
                        LLMContextMessage,
                        {
                            "role": "tool",
                            "tool_call_id": call.tool_call_id,
                            "content": json.dumps(result_payload, default=str),
                        },
                    )
                )
                yield SSEEvent(
                    event="function_call_completed",
                    data={
                        "name": call.function_name,
                        "tool_call_id": call.tool_call_id,
                        "result_summary": _summarize_result(result_payload),
                    },
                )
                if transition_node is not None and next_node is None:
                    next_node = transition_node

            if next_node is not None:
                node = next_node
                node_name = cast(str, node.get("name") or node_name)
                self._apply_node_transition(context, node, global_funcs)
                yield SSEEvent(event="node_transition", data={"to": node_name})
        else:
            # Loop ran to completion without ``break`` — every cycle produced
            # a tool call. Bail out rather than burning more LLM calls.
            # Persist whatever node we last transitioned into so the next
            # ``/message`` resumes from there instead of replaying tool
            # calls from a stale node.
            await update_chat_session_after_turn(
                session_id=self.session_id, current_node=node_name or None
            )
            logger.error(
                f"ChatAgent {self.session_id}: exceeded {_MAX_TOOL_CYCLES} "
                "tool-call cycles without a user-facing reply"
            )
            yield SSEEvent(
                event="error",
                data={
                    "code": "tool_cycle_limit",
                    "message": (
                        f"Exceeded {_MAX_TOOL_CYCLES} tool-call cycles "
                        "without a user-facing reply"
                    ),
                },
            )
            yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
            return

        assistant_text = "".join(assistant_text_chunks).strip()
        if assistant_text:
            stored = await insert_chat_message(
                session_id=self.session_id,
                role=ChatMessageRole.ASSISTANT,
                content=assistant_text,
            )
            yield SSEEvent(
                event="assistant_message",
                data={
                    "idx": stored.idx if stored else None,
                    "content": assistant_text,
                },
            )

        await update_chat_session_after_turn(
            session_id=self.session_id, current_node=node_name or None
        )
        yield SSEEvent(event="turn_end", data={"session_status": "ACTIVE"})

    def _resolve_node(
        self, flow_config: Dict[str, Any], current_node: Optional[str]
    ) -> Dict[str, Any]:
        target = current_node or flow_config["initial_node"]
        if target not in flow_config["nodes"]:
            logger.warning(
                f"ChatAgent {self.session_id}: current_node '{target}' missing "
                f"from template; falling back to '{flow_config['initial_node']}'"
            )
            target = flow_config["initial_node"]
        return cast(Dict[str, Any], flow_config["nodes"][target])

    def _render_node_messages(
        self, node: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Render role + task messages with template_vars and inject language
        rules — chat parity with FlowConfigLoader.load_template."""
        role_messages = render_messages_with_vars(
            list(node.get("role_messages", [])), self.template_vars
        )
        role_messages = inject_language_rules(
            role_messages,
            self.template_vars.get("language_name", "English"),
            getattr(
                getattr(self.template, "configurations", None),
                "payload_based_language_selection",
                False,
            ),
        )
        task_messages = render_messages_with_vars(
            list(node.get("task_messages", [])), self.template_vars
        )
        return role_messages, task_messages

    def _seed_context(
        self,
        node: Dict[str, Any],
        history: List[Dict[str, Any]],
        user_content: str,
        global_funcs: List[FlowsFunctionSchema],
    ) -> LLMContext:
        """Build initial LLMContext: [role, task, …history…, new user]."""
        role_messages, task_messages = self._render_node_messages(node)
        messages: List[Dict[str, Any]] = [
            *role_messages,
            *task_messages,
            *history,
            {"role": "user", "content": user_content},
        ]
        return LLMContext(
            messages=cast(List[LLMContextMessage], messages),
            tools=_tools_schema(node, global_funcs),
        )

    def _apply_node_transition(
        self,
        context: LLMContext,
        next_node: Dict[str, Any],
        global_funcs: List[FlowsFunctionSchema],
    ) -> None:
        """Mid-turn transition — append new task_messages + swap tools.

        Mirrors FlowManager._set_node: role_messages stay from the original
        node (only sent on the very first node); task_messages append so the
        previous node's instructions remain in scope for the model.
        """
        _, task_messages = self._render_node_messages(next_node)
        for msg in task_messages:
            context.add_message(cast(LLMContextMessage, msg))
        context.set_tools(_tools_schema(next_node, global_funcs))

    async def _dispatch_tool_call(
        self,
        call: FunctionCallFromLLM,
        node: Dict[str, Any],
        global_funcs: List[FlowsFunctionSchema],
    ) -> tuple[Any, Optional[Dict[str, Any]]]:
        """Find the wrapper handler for ``call`` and invoke it.

        FlowConfigBuilder built each handler as ``(llm_args, flow_manager)``.
        Chat has no FlowManager — pass None; the wrappers in our codebase
        capture transition_to/hooks/function_config in closures and never
        read the second arg.

        Lookup falls through per-node functions first, then globals — same
        precedence as voice's FlowManager (per-node shadow globals when
        names collide).
        """
        candidates: List[FlowsFunctionSchema] = [
            *(
                fn
                for fn in node.get("functions") or []
                if isinstance(fn, FlowsFunctionSchema)
            ),
            *global_funcs,
        ]
        handler_fn: Any = next(
            (fn.handler for fn in candidates if fn.name == call.function_name),
            None,
        )
        if handler_fn is None:
            logger.error(
                f"ChatAgent {self.session_id}: no handler for '{call.function_name}'"
            )
            return (
                {"status": "error", "error": f"No handler for {call.function_name!r}"},
                None,
            )

        try:
            raw = await handler_fn(dict(call.arguments), None)
        except Exception as exc:
            logger.error(
                f"ChatAgent {self.session_id}: handler {call.function_name!r} "
                f"raised: {exc}",
                exc_info=True,
            )
            return ({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, None)

        # transition_handler returns (result, next_node). Global / HTTP
        # functions return the response payload directly.
        if (
            isinstance(raw, tuple)
            and len(raw) == 2
            and (raw[1] is None or isinstance(raw[1], dict))
        ):
            return raw[0], raw[1]
        return raw, None


def _tools_schema(
    node: Dict[str, Any], global_funcs: List[FlowsFunctionSchema]
) -> ToolsSchema:
    """ToolsSchema concatenating per-node ``functions`` with globals.

    ``FlowsFunctionSchema.to_function_schema()`` strips flow-only fields
    (handler, cancel_on_interruption, timeout_secs); ToolsSchema accepts
    plain FunctionSchema and FlowsDirectFunction interchangeably.

    Direct mode synthesizes a node with empty ``functions`` and lets every
    tool flow through ``global_funcs`` (which the builder populates from
    ``flow.functions``). Flow mode keeps both lists — per-node first so a
    naming collision shadows the global, matching ``_dispatch_tool_call``.
    """
    standard: List[Any] = [
        fn.to_function_schema() if isinstance(fn, FlowsFunctionSchema) else fn
        for fn in (node.get("functions") or [])
    ]
    standard.extend(fn.to_function_schema() for fn in global_funcs)
    return ToolsSchema(standard_tools=standard)


def _summarize_result(value: Any) -> Any:
    """Coerce a tool result to JSON-clean for the SSE payload. Full payload
    still lands in DB / logs."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)


__all__ = ["ChatAgent"]
