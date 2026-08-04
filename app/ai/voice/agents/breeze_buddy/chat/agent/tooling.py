"""Per-turn tool surface + dispatch: flow/global/MCP schema prep,
single-call dispatch, deterministic verification, read-only fan-out.

Method bodies are verbatim from the monolithic agent.py (split 2026-08-05);
this mixin holds no state of its own — every attribute lives on
``ChatAgent`` (see ``core``)."""

import asyncio
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional, Set, Tuple

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat_flows import FlowsFunctionSchema

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
from app.ai.voice.agents.breeze_buddy.chat.disabled import CHAT_DISABLED_NAMES
from app.ai.voice.agents.breeze_buddy.chat.sse import (
    SSEEvent,
    step_completed_event,
    step_started_event,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    resolve_step_label,
    resolve_step_status,
    summarize_step_result,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.verification import (
    run_tool_verifiers,
    verification_error_envelope,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import is_read_only
from app.ai.voice.agents.breeze_buddy.chat.tools.result_annotators import (
    run_result_annotators,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.result_normalizer import normalize
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    resolve_show_op,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    build_render_ui_schema,
    build_revise_plan_schema,
    render_ui_components,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    ShowResolverFn,
)
from app.ai.voice.agents.breeze_buddy.mcp import (
    get_mcp_global_functions_cached,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import with_context
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    inject_tool_args,
)
from app.core.logger import logger

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent


class ToolDispatchMixin:
    async def _prepare_tools(self: "ChatAgent") -> _PreparedTools:
        """Build the per-turn tool surface (flow config, wrapped handlers,
        global + MCP functions, retention policy). Shared by ``run_turn``
        and ``run_approval_turn``."""
        flow_builder = FlowConfigBuilder(
            disabled_names=CHAT_DISABLED_NAMES,
            quiet=True,
            render_ui_mode=self._render_ui_enabled,
            quick_replies_mode=self._quick_replies_mode,
            ui_flavor_groups=self._ui_flavor_groups,
        )
        # Wrap before build_flow_config: _build_function_schema captures the
        # handler reference into a closure, so post-build wrapping is a no-op.
        for handler_name, handler_func in flow_builder.handler_map.items():
            flow_builder.handler_map[handler_name] = with_context(self)(handler_func)
        flow_config = flow_builder.build_flow_config(
            self.template, ui_allowlist=self._ui_allowlist
        )
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

        # Append MCP tools (no CHAT_DISABLED_NAMES filtering — MCP names are
        # external/dynamic and never overlap the voice-only set). Discovery
        # is cached per (template_id, URL hash) in Redis with a 300s TTL,
        # so the list_tools round-trip happens at most once per template
        # per TTL window across the whole pod fleet — not per turn.
        mcp_config = (
            self.template.configurations.mcp if self.template.configurations else None
        )
        if mcp_config and mcp_config.servers:
            mcp_funcs, mcp_approvals = await get_mcp_global_functions_cached(
                mcp_config,
                self.template_vars,
                self.template.id,
                mcp_pool=self.mcp_pool,
            )
            existing_names = {fn.name for fn in global_funcs}
            unique = [fn for fn in mcp_funcs if fn.name not in existing_names]
            global_funcs.extend(unique)
            logger.info(
                f"[BUDDY_MCP] chat: added {len(unique)} MCP tools as global functions"
            )
            # HITL: gate MCP tools by NAME, alongside flow global functions.
            # This is the only place the final registered names exist (the
            # __init__ build_approval_map runs before MCP load), so the merge
            # lives here rather than in build_approval_map. Everything
            # downstream (partition, pending-row persist, approval endpoint,
            # resume re-dispatch) is name-keyed and type-agnostic — it gates
            # MCP tools with no further change. Only tools that actually
            # registered (survived the collision-dedup above) are gated; a
            # gated tool dropped on a name clash is logged, never silently
            # un-gated-by-surprise.
            if mcp_approvals:
                unique_names = {fn.name for fn in unique}
                for name, cfg in mcp_approvals.items():
                    if name in unique_names:
                        self._approval_map[name] = cfg
                    else:
                        logger.warning(
                            f"[BUDDY_MCP] chat: gated MCP tool '{name}' was "
                            "dropped on a name collision with an existing "
                            "function; it will NOT be approval-gated."
                        )

        # RFC-002 engine tools: render_ui (UI as a function call) and
        # revise_plan (the only path off an enforced plan). Registered as
        # ordinary global functions so dispatch, persistence (native
        # function_call/response replay), approval partitioning, and the
        # step rail all apply with zero special cases. Neither is ever
        # approval-gated; neither is read_only (so they never fan out —
        # ordering with sibling calls is meaningful).
        if self._render_ui_enabled:
            rui_components = render_ui_components(self._ui_allowlist, self._catalog_v2)
            if self._quick_replies_mode == "off":
                # quick_replies='off': the component vanishes from the
                # schema enum and docs; execute rejects it as unknown.
                rui_components = [c for c in rui_components if c != "QuickReplies"]
            if rui_components:
                global_funcs.append(
                    build_render_ui_schema(
                        rui_components,
                        self._render_ui_handler,
                        trusted_urls=sorted(self._trusted_link_urls),
                        quick_replies_mode=self._quick_replies_mode,
                        flavor_groups=self._ui_flavor_groups,
                    )
                )
        if self._plan_enforcement:
            global_funcs.append(build_revise_plan_schema(self._revise_plan_handler))

        # Aggregate per-tool context-retention policy across every MCP server
        # the template declares. Used by llm_driver to compact stale
        # tool_result blocks in the messages array before each LLM call —
        # bounds input-token cost as a session accumulates tool calls.
        # Tools not in the map default to ``session`` (no compaction).
        #
        # ``tool_projection`` is the companion keep-list map: for a
        # ``last_turn_only`` tool with an entry, the compactor keeps an identity
        # projection (whitelisted paths) instead of a bare stub, so durable
        # referents (product url/handle/price, variant ids) survive across
        # turns at ~1% of the tokens — fixing "give me the link"/"re-add it"
        # follow-ups that would otherwise force a re-search or a hallucinated URL.
        tool_retention: Dict[str, str] = {}
        tool_projection: Dict[str, List[str]] = {}
        if mcp_config and mcp_config.servers:
            for server in mcp_config.servers:
                if server.tool_context_retention:
                    tool_retention.update(server.tool_context_retention)
                if server.tool_context_projection:
                    tool_projection.update(server.tool_context_projection)

        return _PreparedTools(
            flow_config=flow_config,
            global_funcs=global_funcs,
            tool_retention=tool_retention or None,
            tool_projection=tool_projection or None,
        )

    async def _dispatch_tool_call(
        self: "ChatAgent",
        call: FunctionCallFromLLM,
        node: Dict[str, Any],
        global_funcs: List[FlowsFunctionSchema],
        injected_args: Optional[Dict[str, Any]] = None,
    ) -> tuple[Any, Optional[Dict[str, Any]]]:
        """Find the wrapper handler for ``call`` and invoke it.

        FlowConfigBuilder built each handler as ``(llm_args, flow_manager)``.
        Chat has no FlowManager — pass None; the wrappers in our codebase
        capture transition_to/hooks/function_config in closures and never
        read the second arg.

        Lookup falls through per-node functions first, then globals — same
        precedence as voice's FlowManager (per-node shadow globals when
        names collide).

        ``injected_args`` is the post-injection argument dict from
        :func:`inject_tool_args` (template-declared session-state fills).
        When omitted, the LLM-provided ``call.arguments`` are used as-is.
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

        args_for_handler = (
            injected_args if injected_args is not None else dict(call.arguments)
        )
        try:
            raw = await handler_fn(args_for_handler, None)
        except Exception as exc:
            # loguru: no ``exc_info`` kwarg — it would re-format the message
            # and crash on brace-bearing tool/provider error text.
            logger.exception(
                f"ChatAgent {self.session_id}: handler {call.function_name!r} "
                f"raised: {exc}"
            )
            return ({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, None)

        # transition_handler returns (result, next_node). Global / HTTP
        # functions return the response payload directly. Registered result
        # annotators (RFC-003 baseline: matched_via / matched_variant on
        # search results) run over the normalized payload — additive keys
        # only, before verification / binding store / LLM context.
        if (
            isinstance(raw, tuple)
            and len(raw) == 2
            and (raw[1] is None or isinstance(raw[1], dict))
        ):
            normalized = normalize(call.function_name, raw[0])
            return (
                run_result_annotators(call.function_name, args_for_handler, normalized),
                raw[1],
            )
        normalized = normalize(call.function_name, raw)
        return (
            run_result_annotators(call.function_name, args_for_handler, normalized),
            None,
        )

    def _verify_result(
        self: "ChatAgent",
        tool_name: str,
        injected_args: Dict[str, Any],
        result_payload: Any,
    ) -> Any:
        """Run registered deterministic post-condition verifiers (Phase 2).

        A failure converts the result into a structured error envelope
        BEFORE the binding store / reducers / LLM context see it — the
        model gets a precise, actionable error; a `show` op can never
        hydrate off an unverified result. Verifiers are pure code checks
        registered by flavor modules (see chat/verification.py).
        """
        failure = run_tool_verifiers(tool_name, injected_args, result_payload)
        if failure is None:
            return result_payload
        logger.warning(
            f"ChatAgent {self.session_id}: {tool_name} failed verification — "
            f"{failure}"
        )
        return verification_error_envelope(failure, result_payload)

    def _should_fan_out(self: "ChatAgent", calls: List[FunctionCallFromLLM]) -> bool:
        """True when this cycle's ungated batch dispatches CONCURRENTLY:
        2+ calls, every one annotated ``read_only`` (template overrides >
        flavor registry > destructive default), and the template hasn't
        disabled the fan-out
        (``configurations.tool_execution.parallel_read_only=false``).
        Mutations never fan out — order is meaningful."""
        if len(calls) < 2:
            return False
        configurations = getattr(self.template, "configurations", None)
        tool_execution = getattr(configurations, "tool_execution", None)
        if getattr(tool_execution, "parallel_read_only", None) is False:
            return False
        return all(is_read_only(c.function_name, self.template) for c in calls)

    async def _fan_out_read_only(
        self: "ChatAgent",
        calls: List[FunctionCallFromLLM],
        node: Dict[str, Any],
        global_funcs: List[FlowsFunctionSchema],
        arg_injection_rules: List[Any],
        results: Dict[str, Tuple[Any, Optional[Dict[str, Any]]]],
    ) -> AsyncIterator[SSEEvent]:
        """Dispatch an all-read-only batch concurrently (Phase 2 fan-out).

        Yields the step/tool wire events — step_started for every call up
        front (the lines appear together, which is the honest rendering of
        a parallel batch), then function_call_completed + step_completed in
        COMPLETION order as each dispatch lands. Verified results are
        written into ``results`` keyed by tool_call_id; the caller
        post-processes them in ORIGINAL call order, so binding-store /
        context / reducer semantics are identical to sequential dispatch.

        Injections are computed sequentially before dispatch (they read
        agent_state, which read-only calls don't mutate). A dispatch
        exception cancels the remaining tasks and re-raises — same turn
        outcome as a sequential failure; the next turn's history repair
        answers any dangling tool_use rows.
        """
        injected: Dict[str, Dict[str, Any]] = {}
        done_labels: Dict[str, str] = {}
        for call in calls:
            injected[call.tool_call_id] = inject_tool_args(
                tool_name=call.function_name,
                args=dict(call.arguments),
                state_data=self.agent_state,
                chat_session_id=self.session_id,
                injections=arg_injection_rules,
                turn_id=self._turn_id,
            )
            running_label, done_label = resolve_step_label(call.function_name)
            done_labels[call.tool_call_id] = done_label
            yield step_started_event(
                step_id=call.tool_call_id,
                label=running_label,
                turn_id=self._turn_id,
            )

        tasks: Dict["asyncio.Task[Any]", FunctionCallFromLLM] = {
            asyncio.create_task(
                self._dispatch_tool_call(
                    call, node, global_funcs, injected_args=injected[call.tool_call_id]
                )
            ): call
            for call in calls
        }
        pending: Set["asyncio.Task[Any]"] = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    call = tasks[task]
                    # A raised dispatch propagates here (and cancels the
                    # rest via finally) — sequential parity.
                    result_payload, transition_node = task.result()
                    result_payload = self._verify_result(
                        call.function_name,
                        injected[call.tool_call_id],
                        result_payload,
                    )
                    results[call.tool_call_id] = (result_payload, transition_node)
                    yield SSEEvent(
                        event="function_call_completed",
                        data={
                            "name": call.function_name,
                            "tool_call_id": call.tool_call_id,
                            "result_summary": _summarize_result(result_payload),
                        },
                    )
                    step_summary, step_count = summarize_step_result(result_payload)
                    yield step_completed_event(
                        step_id=call.tool_call_id,
                        status=resolve_step_status(result_payload),
                        label=done_labels[call.tool_call_id],
                        summary=step_summary,
                        count=step_count,
                    )
        finally:
            for task in pending:
                task.cancel()

    def _show_resolver(self: "ChatAgent") -> Optional[ShowResolverFn]:
        """Per-op resolver for catalog-v2 ``show`` ops, or ``None`` when this
        session didn't negotiate v2 (show ops then drop with telemetry —
        the allowlist pruning in ``__init__`` already produced the more
        specific ``show_component_disabled`` reason at parse time)."""
        if not self._catalog_v2:
            return None
        store = self._binding_store
        allowlist = self._ui_allowlist

        def _resolve(op: Dict[str, Any]):
            return resolve_show_op(op, store, allowlist)

        return _resolve
