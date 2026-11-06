"""HITL approval turns: gate persistence, the resume continuation
after a decision, and resume-time context reseeding.

Method bodies are verbatim from the monolithic agent.py (split 2026-08-05);
this mixin holds no state of its own — every attribute lives on
``ChatAgent`` (see ``core``)."""

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional, cast

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMContextMessage,
)
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
from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    diff_state_patch,
    render_client_context,
)
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    tool_results_to_user_blocks,
)
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
from app.ai.voice.agents.breeze_buddy.mcp import (
    close_mcp_pool,
)
from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    build_kb_system_message,
    fetch_full_kb_text_cached,
    resolve_kb_runtime,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    apply_state_reducers,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.chat_session import (
    insert_chat_message,
    update_chat_session_after_turn,
    upsert_agent_session_state_merge,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole, ToolApproval

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent


class ApprovalTurnMixin:
    # Assigned here AND in core — annotated identically on both
    # classes so pyrefly sees one consistent member.
    aiohttp_session: Optional[Any]
    mcp_pool: Optional[Dict[str, Any]]

    async def run_approval_turn(
        self: "ChatAgent",
        *,
        approval: ToolApproval,
        approved: bool,
        wire_status: str,
        decision_reason: Optional[str],
        synthetic_result: Optional[Dict[str, Any]],
        history: List[Dict[str, Any]],
        current_node: Optional[str],
        pending_sibling_ids: List[str],
    ) -> AsyncIterator[SSEEvent]:
        """Resume a turn that ended awaiting approval (HITL Pattern B).

        The caller (approve_chat_tool_handler) has ALREADY atomically
        claimed the approval row and, for deny/expired outcomes, persisted
        the synthetic tool_result row under the session lock BEFORE loading
        ``history`` — so the denial result replays via history and the
        decided-but-unpersisted crash window only exists on the approve
        path (closed below with a shielded error-row write).

        - ``approved=True``: execute the stored (post-injection) arguments
          verbatim, persist the result, then continue the LLM loop.
        - ``approved=False`` (denied / expired): no execution; continue the
          LLM loop so the model can acknowledge.
        - ``pending_sibling_ids`` non-empty: other gated calls from the same
          batch are still undecided — end the turn awaiting them WITHOUT
          invoking the LLM (the replayed context would have dangling
          tool_use blocks).
        """
        self.aiohttp_session = create_aiohttp_session()
        self.mcp_pool = {}
        # Fresh turn id: any NEW tool calls in the continued loop get fresh
        # idempotency hashes. The approved call itself replays the stored
        # args (original turn's hash) — intentional, it IS that operation.
        self._turn_id = uuid.uuid4().hex
        try:
            async for event in self._run_approval_turn_inner(
                approval=approval,
                approved=approved,
                wire_status=wire_status,
                decision_reason=decision_reason,
                synthetic_result=synthetic_result,
                history=history,
                current_node=current_node,
                pending_sibling_ids=pending_sibling_ids,
            ):
                yield event
        finally:
            if self.aiohttp_session is not None:
                await self.aiohttp_session.close()
                self.aiohttp_session = None
            await close_mcp_pool(self.mcp_pool)
            self.mcp_pool = None

    async def _run_approval_turn_inner(
        self: "ChatAgent",
        *,
        approval: ToolApproval,
        approved: bool,
        wire_status: str,
        decision_reason: Optional[str],
        synthetic_result: Optional[Dict[str, Any]],
        history: List[Dict[str, Any]],
        current_node: Optional[str],
        pending_sibling_ids: List[str],
    ) -> AsyncIterator[SSEEvent]:
        prep = await self._prepare_tools()
        node = self._resolve_node(prep.flow_config, current_node)
        node_name = cast(str, node["name"])
        # Resume turns get full-injection KB only: there is no new user
        # utterance to retrieve on, and the history tail is tool_use/tool_result
        # where extra messages break provider adapters.
        kb_message = await self._prepare_kb_message_for_resume()
        context = self._seed_resume_context(
            node, history, prep.global_funcs, kb_message=kb_message
        )

        yield SSEEvent(
            event="function_approval_resolved",
            data={
                "tool_call_id": approval.tool_call_id,
                "status": wire_status,
                "reason": decision_reason,
            },
        )

        transition_node: Optional[Dict[str, Any]] = None
        approved_done_label: Optional[str] = None
        if approved:
            # Step-progress line for the approved execution — the resume
            # turn brackets its tool run exactly like _cycle_loop does, so
            # the widget's step list covers HITL resumes too.
            running_label, approved_done_label = resolve_step_label(
                approval.function_name, self._flavor_scope
            )
            yield step_started_event(
                step_id=approval.tool_call_id,
                label=running_label,
                turn_id=self._turn_id,
            )
            call = FunctionCallFromLLM(
                function_name=approval.function_name,
                tool_call_id=approval.tool_call_id,
                arguments=dict(approval.arguments),
                context=None,
            )
            persist_task: Optional["asyncio.Task[None]"] = None
            try:
                # Stored args are dispatched verbatim — they were injected
                # at gate time and are exactly what the user approved.
                result_payload, transition_node = await self._dispatch_tool_call(
                    call,
                    node,
                    prep.global_funcs,
                    injected_args=dict(approval.arguments),
                )
                result_payload = self._verify_result(
                    approval.function_name, dict(approval.arguments), result_payload
                )
                # A `show` op in the continued LLM loop may bind to the
                # approved call's result — record it like _cycle_loop does.
                self._binding_store.record(
                    approval.function_name, approval.tool_call_id, result_payload
                )
                reducer_rules = (
                    self.template.configurations.state_reducers
                    if self.template.configurations
                    else []
                )
                self.agent_state = apply_state_reducers(
                    state_data=self.agent_state,
                    tool_name=approval.function_name,
                    tool_result=result_payload,
                    reducers=reducer_rules,
                )
                # Run the real result write as a task so a cancellation
                # landing during (or after) it can tell whether the row
                # already exists — writing a synthetic row on top would
                # answer the same tool_use twice, which providers reject
                # on every later replay (permanent session brick).
                persist_task = asyncio.create_task(
                    self._persist_tool_result_row(approval.tool_call_id, result_payload)
                )
                await asyncio.shield(persist_task)
                # Persist only the keys the reducers changed this turn (see
                # _cycle_loop note); never the whole row, never the client-
                # context keys a /context push owns.
                reducer_patch = diff_state_patch(
                    self._loaded_state_baseline, self.agent_state
                )
                if reducer_patch:
                    await upsert_agent_session_state_merge(
                        chat_session_id=self.session_id,
                        patch=reducer_patch,
                    )
            except asyncio.CancelledError:
                # Stop button / disconnect mid-execution. The row is already
                # DECIDED — without a persisted result the session history
                # would carry a dangling tool_use forever. Write the
                # synthetic row ONLY if the real write never landed (it may
                # have completed before, or kept running under the shield
                # after, the cancellation).
                real_write_landed = False
                if persist_task is not None:
                    try:
                        await asyncio.shield(persist_task)
                        real_write_landed = True
                    except asyncio.CancelledError:
                        # Second cancel mid-wait — the shielded task still
                        # runs to completion in the background; treat as
                        # landed to avoid the duplicate-answer brick (the
                        # repair backstop covers the lost-write case).
                        real_write_landed = True
                    except Exception:
                        real_write_landed = False
                if not real_write_landed:
                    await asyncio.shield(
                        self._persist_tool_result_row(
                            approval.tool_call_id,
                            {
                                "status": "error",
                                "error": "execution was interrupted before completing",
                            },
                        )
                    )
                raise
            context.add_message(
                cast(
                    LLMContextMessage,
                    {
                        "role": "tool",
                        "tool_call_id": approval.tool_call_id,
                        "content": json.dumps(result_payload, default=str),
                    },
                )
            )
        else:
            # Denied / expired: the synthetic result row was persisted by
            # the handler before history load, so it is already in
            # ``context`` via the replayed history.
            result_payload = synthetic_result or {
                "status": "denied",
                "reason": decision_reason or "the user did not approve this action",
            }

        yield SSEEvent(
            event="function_call_completed",
            data={
                "name": approval.function_name,
                "tool_call_id": approval.tool_call_id,
                "result_summary": _summarize_result(result_payload),
            },
        )
        if approved and approved_done_label is not None:
            step_summary, step_count = summarize_step_result(
                result_payload, self._flavor_scope
            )
            yield step_completed_event(
                step_id=approval.tool_call_id,
                status=resolve_step_status(result_payload),
                label=approved_done_label,
                summary=step_summary,
                count=step_count,
            )

        if transition_node is not None:
            node = transition_node
            node_name = cast(str, node.get("name") or node_name)
            self._apply_node_transition(context, node, prep.global_funcs)
            yield SSEEvent(event="node_transition", data={"to": node_name})

        # Persist node BEFORE a possible siblings early-return — the next
        # sibling's approval turn resolves session.current_node, which
        # would otherwise be stale after a transition here.
        await update_chat_session_after_turn(
            session_id=self.session_id, current_node=node_name or None
        )

        if pending_sibling_ids:
            # Other gated calls from the same batch still await decisions;
            # invoking the LLM now would replay dangling tool_use blocks.
            # ``assistant_idx`` is intentionally None: this branch returns
            # BEFORE _cycle_loop, so no LLM inference ran this turn and there
            # is no metrics row to key (_persist_turn_metrics early-returns on
            # None). The SDK settles each card off function_approval_resolved /
            # function_call_completed (both carry tool_call_id), so it needs no
            # assistant anchor here; emitting a prior turn's gate-row idx would
            # mis-attribute metrics to a row this turn never wrote.
            yield SSEEvent(
                event="turn_end",
                data={
                    "session_status": "ACTIVE",
                    "assistant_idx": None,
                    "awaiting_approval": True,
                    "pending_tool_call_ids": pending_sibling_ids,
                },
            )
            return

        async for event in self._cycle_loop(
            context, node, prep, first_cycle_fast=False
        ):
            yield event

    async def _persist_tool_result_row(
        self: "ChatAgent", tool_call_id: str, result_payload: Any
    ) -> None:
        """Persist one tool result as a USER row of tool_result blocks —
        the resume-path sibling of the coalesced batch write in
        ``_cycle_loop``."""
        await insert_chat_message(
            session_id=self.session_id,
            role=ChatMessageRole.USER,
            content=None,
            content_blocks=tool_results_to_user_blocks(
                [(tool_call_id, result_payload)]
            ),
        )

    async def _prepare_kb_message_for_resume(
        self: "ChatAgent",
    ) -> Optional["_KbMessage"]:
        """Full-injection KB message for approval-resume turns (or None).

        Resume turns (HITL approval) carry no new user utterance, so there
        is nothing to retrieve on — only full_injection mode applies here.
        Called from ``_seed_resume_context``; fail-open like all KB paths.
        """
        try:
            runtime = await resolve_kb_runtime(
                self.template.configurations if self.template else None
            )
            if runtime is None or runtime.mode != "full_injection":
                return None
            text = await fetch_full_kb_text_cached(runtime.config)
            if not text:
                return None
            return _KbMessage(
                message=build_kb_system_message(text, runtime.config),
                placement="prefix",
            )
        except Exception as e:
            logger.warning(
                f"ChatAgent {self.session_id}: KB resume prep failed (continuing): {e}"
            )
            return None

    def _seed_resume_context(
        self: "ChatAgent",
        node: Dict[str, Any],
        history: List[Dict[str, Any]],
        global_funcs: List[FlowsFunctionSchema],
        kb_message: Optional["_KbMessage"] = None,
    ) -> LLMContext:
        """Build the LLMContext for an approval-resume turn:
        ``[role, task, system_block?, …history…]`` — NO new user message.

        Unlike ``_seed_context``, the client-context ``system_block`` goes
        BEFORE history: the replayed history tail is an assistant
        tool_calls message (+ tool results), and wedging a system message
        between an assistant tool_calls and its tool responses is rejected
        by OpenAI and breaks the Anthropic adapter's role merge. The
        ``user_block`` variant is dropped entirely — it rides user turns
        and a resume turn has none.
        """
        role_messages, task_messages = self._render_node_messages(node)
        _user_block, system_block = render_client_context(
            self.agent_state,
            self._client_context_config,
            self._context_placement,
        )
        messages: List[Dict[str, Any]] = [
            *role_messages,
            *task_messages,
        ]
        if kb_message is not None and kb_message.placement == "prefix":
            messages.append(kb_message.message)
        if system_block:
            messages.append({"role": "system", "content": system_block})
        messages.extend(history)
        return LLMContext(
            messages=cast(List[LLMContextMessage], messages),
            tools=_tools_schema(node, global_funcs),
        )
