"""The turn cycle loop — the heart of the agent: stream, tool batches,
steps/plan SSE, persistence of turn rows, chips flow control.

Method bodies are verbatim from the monolithic agent.py (split 2026-08-05);
this mixin holds no state of its own — every attribute lives on
``ChatAgent`` (see ``core``)."""

import json
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    cast,
)

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMContextMessage,
    LLMSpecificMessage,
)
from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import (  # noqa: F401
    _ANSWER_NUDGE,
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
)
from app.ai.voice.agents.breeze_buddy.chat.guardrails import ChatOutputGuard
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    assistant_turn_to_blocks,
    internal_text_block,
    plain_text_blocks,
    tool_results_to_user_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.signatures import (
    gemini_signature_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import (
    SSEEvent,
    plan_event,
    step_completed_event,
    step_started_event,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    resolve_step_label,
    resolve_step_status,
    summarize_step_result,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import is_read_only
from app.ai.voice.agents.breeze_buddy.chat.ui.healer import (
    HealerContext,
    make_healer_fn,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    RENDER_UI_TOOL_NAME,
    REVISE_PLAN_TOOL_NAME,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    TextOut,
    process_op_line,
    strip_ui_stream_markers,
    summarize_ui_ops,
    ui_op_dropped_event,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _is_tool_success,
    apply_state_reducers,
    inject_tool_args,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    insert_chat_message,
    update_chat_session_after_turn,
    upsert_agent_session_state_merge,
)
from app.database.accessor.breeze_buddy.tool_approvals import insert_tool_approval
from app.schemas.breeze_buddy.chat import ChatMessageRole

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent


# render_ui / revise_plan are the engine's own harness tools: they present
# or re-plan, they never fetch. A cycle that calls only these is not doing
# work the model is waiting on, so prose beside them is a reply, not an
# announcement of work to come.
_HARNESS_TOOLS = frozenset({RENDER_UI_TOOL_NAME, REVISE_PLAN_TOOL_NAME})


class CycleLoopMixin:
    async def _finish_guardrail_output_block(
        self: "ChatAgent",
        *,
        node_name: str,
        approved_before_cycle: List[str],
        approved_in_cycle: List[str],
        turn_ui_ops: List[Dict[str, Any]],
        redirect_message: str,
    ) -> AsyncIterator[SSEEvent]:
        """Persist only approved prose plus the trusted fixed redirect."""
        approved = strip_ui_stream_markers("".join(approved_before_cycle)).strip()
        current = "".join(approved_in_cycle).strip()
        text_parts = [part for part in (approved, current) if part]
        redirect = redirect_message.strip()
        if redirect:
            prefix = " " if text_parts else ""
            yield SSEEvent(
                event="assistant_token", data={"delta": f"{prefix}{redirect}"}
            )
            text_parts.append(redirect)
        visible_text = " ".join(text_parts).strip()

        assistant_idx: Optional[int] = None
        final_ui_blocks = self._row_ui_blocks(turn_ui_ops)
        if visible_text or final_ui_blocks:
            if self._internal_turn:
                persisted_blocks = (
                    [internal_text_block(visible_text)] if visible_text else []
                )
            else:
                persisted_blocks = (
                    plain_text_blocks(visible_text) if visible_text else []
                )
            stored = await insert_chat_message(
                session_id=self.session_id,
                role=ChatMessageRole.ASSISTANT,
                content=None if self._internal_turn else (visible_text or None),
                content_blocks=persisted_blocks,
                ui_blocks=final_ui_blocks,
            )
            assistant_idx = stored.idx if stored else None
            if visible_text and not self._internal_turn:
                yield SSEEvent(
                    event="assistant_message",
                    data={"idx": assistant_idx, "content": visible_text},
                )

        await update_chat_session_after_turn(
            session_id=self.session_id,
            current_node=node_name or None,
        )
        logger.info(f"ChatAgent {self.session_id}: output blocked by Guardrail")
        yield SSEEvent(
            event="turn_end",
            data={"session_status": "ACTIVE", "assistant_idx": assistant_idx},
        )

    async def _cycle_loop(
        self: "ChatAgent",
        context: LLMContext,
        node: Dict[str, Any],
        prep: _PreparedTools,
        *,
        first_cycle_fast: bool = True,
    ) -> AsyncIterator[SSEEvent]:
        """The LLM ↔ tool loop for one turn (shared by ``run_turn`` and the
        continuation of ``run_approval_turn``). ``context`` must already be
        seeded; this loop drives LLM cycles, dispatches ungated tool calls,
        and ends the turn early (``turn_end {awaiting_approval}``) when the
        LLM calls an approval-gated function.

        ``first_cycle_fast`` grades cycle 1 down to minimal thinking (see
        the override at the stream call). Approval continuations pass False
        — their "first" cycle follows a freshly-approved tool result, i.e.
        it's post-tool reasoning, not routing."""
        global_funcs = prep.global_funcs
        node_name = cast(str, node["name"])

        assistant_text_chunks: List[str] = []
        # Prose splits into two kinds, and only one of them ends a turn.
        # NARRATION is prose from a cycle that ALSO called tools ("Let me
        # find those for you") — it streams ahead of the work and is not
        # an answer to anything. An ANSWER is prose from a cycle that
        # called nothing: the model had its results and spoke.
        #
        # Both club into the single user-facing row below (one bubble per
        # turn — splitting them into two rows re-introduces the duplicate
        # reply on resume fixed 2026-07-31). The distinction exists purely
        # so the turn-completion test below reads intent instead of
        # inspecting whether ANY text happens to exist.
        answered = False
        answer_retried = False
        # ui_ops accumulator (Sprint 1.7) — captures each SpecStream op the
        # LLM emits during this user turn. Persisted alongside the assistant
        # message so the widget resume path can repaint Tiles/Carousels
        # after a page refresh. Reset per cycle so each persisted row
        # carries only the ops produced by THAT cycle.
        turn_ui_ops: List[Dict[str, Any]] = []
        # Per-cycle LLM-specific context messages (Gemini thought
        # signatures). Appended to the in-memory context in stream order
        # (the driver never mutates context itself) AND persisted on the
        # cycle's assistant row so signatures survive the stateless-turn
        # DB round-trip — Vertex Gemini 3 + thinking rejects a replayed
        # functionCall part that lacks its thought_signature.
        cycle_context_messages: List[LLMSpecificMessage] = []
        skip_force_once = False
        # Set when the forced-final chips path persists the turn's prose row
        # EARLY (before the chips cycle) — the after-loop turn_end reuses it
        # as the metrics anchor when no later row is written.
        early_final_idx: Optional[int] = None
        for cycle in range(1, _MAX_TOOL_CYCLES + 1):
            tool_calls: List[FunctionCallFromLLM] = []
            cycle_approved_chunks: List[str] = []
            output_guard = ChatOutputGuard(
                self.guardrail_coordinator,
                released_any=self._turn_prose_streamed,
            )
            output_blocked = False
            turn_ui_ops = []
            cycle_context_messages = []
            finish_reason: Optional[str] = None
            # Snapshot BEFORE the stream: a <plan> extracted mid-stream arms
            # the enforcer for the NEXT cycle — this flag tells the no-call
            # branch below whether arming happened during this one.
            plan_was_constraining = (
                self._plan_enforcement and self._plan_enforcer.constraining
            )
            # Set the moment an ENFORCED plan arms mid-stream: the rest of
            # this cycle's prose is dropped (see the text branch below).
            suppress_cycle_prose = False

            # Forced tool choice for THIS cycle (RFC-002): an active enforced
            # plan constrains to {current step's tool, revise_plan}; else a
            # pending forced think-step constrains to render_ui (whose
            # {decision:'no_ui'} payload keeps display the model's judgment);
            # else a pending final chips cycle constrains to render_ui with
            # the chips-slot restriction (QuickReplies or no_ui only).
            # ``skip_force_once`` is the MALFORMED_FUNCTION_CALL fallback —
            # one unforced retry instead of a bricked turn.
            allowed: Optional[List[str]] = None
            self._in_chips_cycle = False
            if skip_force_once:
                skip_force_once = False
            elif self._plan_enforcement and self._plan_enforcer.constraining:
                allowed = self._plan_enforcer.allowed_names(REVISE_PLAN_TOOL_NAME)
            elif self._need_render_ui_think and self._render_ui_enabled:
                allowed = [RENDER_UI_TOOL_NAME]
            elif self._chips_pending and self._render_ui_enabled:
                allowed = [RENDER_UI_TOOL_NAME]
                self._in_chips_cycle = True
                self._chips_cycles += 1
            if self._plan_enforcement:
                # revise_plan is visible ONLY while a plan is active
                # (Decision 3) — rebuild the cycle's tool schema either way
                # so a plan finishing mid-turn hides it again.
                cycle_funcs = (
                    global_funcs
                    if self._plan_enforcer.active
                    else [fn for fn in global_funcs if fn.name != REVISE_PLAN_TOOL_NAME]
                )
                context.set_tools(_tools_schema(node, cycle_funcs))

            response_stream = llm_driver.stream(
                self._llm,
                context,
                log_label=f"chat#{self.session_id[:8]}",
                tool_context_retention=prep.tool_retention,
                tool_context_projection=prep.tool_projection,
                allowed_function_names=allowed,
                # Cycle-graded thinking (2026-07-30 latency pass): cycle 1
                # of a fresh turn is ROUTING — greet in prose or pick the
                # first tool call — which minimal handles as reliably as
                # medium (probed live: tool selection identical incl. the
                # answer-from-memory bait, ~1s faster to first token).
                # Every post-tool cycle keeps the template's level, so
                # grounding, variant math, and UI authoring reason at full
                # depth. The chips cycle picks 2-4 labels — minimal too.
                thinking_level_override=(
                    "minimal"
                    if (self._in_chips_cycle or (first_cycle_fast and cycle == 1))
                    else None
                ),
            )
            async for kind, payload in response_stream:
                if kind == "text":
                    text = cast(str, payload)
                    # Plan-as-emission (Phase 2): strip any <plan>…</plan>
                    # declarations FIRST (they never reach prose, context,
                    # or persistence) and surface them as plan events for
                    # the widget's skeleton lines.
                    text, plans = self._plan_extractor.feed(text)
                    for plan in plans:
                        yield self._plan_sse(plan)
                        if self._plan_enforcement:
                            # Harness-held plan state (RFC-002 Decision 4):
                            # from the next cycle on, off-plan calls are
                            # impossible at the API layer. Fails open on
                            # unknown tool names (plan stays advisory).
                            self._plan_known_tools = self._known_tool_names(
                                node, global_funcs
                            )
                            self._plan_enforcer.start(plan, self._plan_known_tools)
                            if self._plan_enforcer.constraining:
                                suppress_cycle_prose = True
                    if (
                        suppress_cycle_prose
                        or self._in_chips_cycle
                        or self._suppress_extra_prose
                    ):
                        # Post-plan prose in the SAME cycle is pseudo-call
                        # chatter, not shopper prose — Flash sometimes TYPES
                        # the call it planned (`path:default_api:...{...}`)
                        # as text right after the <plan> marker, and the
                        # prompt forbids narrating the plan anyway. Real
                        # prose belongs to the cycle that ends the turn.
                        # Chips-cycle text is likewise junk: the final reply
                        # already streamed and its bubble is anchored — any
                        # trailing tokens here would paint after it.
                        # _suppress_extra_prose: the reply was delivered and
                        # a banned mid-turn chips call followed it — anything
                        # the model says now would be a duplicate reply.
                        continue
                    if not text:
                        continue
                    # Strip <ui_stream>…</ui_stream> from the user-facing
                    # prose stream. Each TextOut becomes an
                    # assistant_token; each JsonlOpLine is healed →
                    # catalog-validated → emitted as one or more SSE
                    # events (ui_op + optional healer_applied/ui_op_dropped).
                    healer_ctx = HealerContext(
                        session_data=self.agent_state,
                        known_ids=self._known_ui_ids,
                    )
                    healer = make_healer_fn(healer_ctx)
                    for out in self._ui_extractor.feed(text):
                        if isinstance(out, TextOut):
                            guarded = await output_guard.feed(out.value)
                            for chunk in guarded.chunks:
                                cycle_approved_chunks.append(chunk)
                                yield SSEEvent(
                                    event="assistant_token", data={"delta": chunk}
                                )
                            if guarded.blocked:
                                output_blocked = True
                                break
                        elif self._render_ui_enabled:
                            # Hard cutover (RFC-002 Phase D): render_ui
                            # sessions accept UI ONLY via the render_ui
                            # function call — the dual-read window is
                            # closed. A text-channel op line drops
                            # observably (ui_op_dropped telemetry + the
                            # metrics drops row), never renders.
                            yield ui_op_dropped_event(out.raw, "text_channel_retired")
                        else:
                            for ev in process_op_line(
                                out.raw,
                                session_state=self.agent_state,
                                healer=healer,
                                known_ids=self._known_ui_ids,
                                allowlist=self._ui_allowlist,
                                show_resolver=self._show_resolver(),
                            ):
                                # Capture successful ui_op emissions for the
                                # widget resume path (persisted on the
                                # assistant row via ui_blocks).
                                if ev.event == "ui_op":
                                    op_payload = (
                                        ev.data.get("op")
                                        if isinstance(ev.data, dict)
                                        else None
                                    )
                                    if isinstance(op_payload, dict):
                                        turn_ui_ops.append(op_payload)
                                yield ev
                    if output_blocked:
                        break
                elif kind == "tool_call":
                    call = cast(FunctionCallFromLLM, payload)
                    tool_calls.append(call)
                elif kind == "context_message":
                    # LLM-specific context message (Gemini thought
                    # signature). Added in stream order — BEFORE the
                    # assistant tool_calls message this loop appends after
                    # the stream closes — matching pipecat's pipeline
                    # ordering; the adapter re-applies it by bookmark.
                    ctx_msg = cast(LLMSpecificMessage, payload)
                    context.add_message(ctx_msg)
                    cycle_context_messages.append(ctx_msg)
                elif kind == "finish_reason":
                    finish_reason = cast(str, payload)

            if output_blocked:
                await cast(Any, response_stream).aclose()
                async for event in self._finish_guardrail_output_block(
                    node_name=node_name,
                    approved_before_cycle=assistant_text_chunks,
                    approved_in_cycle=cycle_approved_chunks,
                    turn_ui_ops=turn_ui_ops,
                    redirect_message=output_guard.redirect_message,
                ):
                    yield event
                return

            # Release the plan extractor's held tail (a partial "<plan"
            # prefix is ordinary prose; an unterminated block is dropped) —
            # it flows through the SAME ui-extractor path as live chunks.
            plan_tail = self._plan_extractor.flush()
            if plan_tail and not suppress_cycle_prose and not self._in_chips_cycle:
                for out in self._ui_extractor.feed(plan_tail):
                    if isinstance(out, TextOut):
                        guarded = await output_guard.feed(out.value)
                        for chunk in guarded.chunks:
                            cycle_approved_chunks.append(chunk)
                            yield SSEEvent(
                                event="assistant_token", data={"delta": chunk}
                            )
                        if guarded.blocked:
                            output_blocked = True
                            break

            # Drain any text the UI extractor held while deciding whether a
            # partial marker was prose. It must pass the same output gate
            # before tool dispatch or persistence.
            if not output_blocked:
                for out in self._ui_extractor.flush():
                    if not isinstance(out, TextOut):
                        continue
                    guarded = await output_guard.feed(out.value)
                    for chunk in guarded.chunks:
                        cycle_approved_chunks.append(chunk)
                        yield SSEEvent(event="assistant_token", data={"delta": chunk})
                    if guarded.blocked:
                        output_blocked = True
                        break

            if not output_blocked:
                guarded_tail = await output_guard.flush()
                for chunk in guarded_tail.chunks:
                    cycle_approved_chunks.append(chunk)
                    yield SSEEvent(event="assistant_token", data={"delta": chunk})
                output_blocked = guarded_tail.blocked

            if output_blocked:
                async for event in self._finish_guardrail_output_block(
                    node_name=node_name,
                    approved_before_cycle=assistant_text_chunks,
                    approved_in_cycle=cycle_approved_chunks,
                    turn_ui_ops=turn_ui_ops,
                    redirect_message=output_guard.redirect_message,
                ):
                    yield event
                return

            # Tool activity is surfaced only after all preceding prose has
            # passed the output gate. A blocked cycle never starts its calls.
            for call in tool_calls:
                yield SSEEvent(
                    event="function_call_started",
                    data={
                        "name": call.function_name,
                        "args": dict(call.arguments),
                        "tool_call_id": call.tool_call_id,
                    },
                )

            # No DATA tool in this cycle means the model already had
            # everything it needed when it spoke — this is the answer.
            # render_ui / revise_plan don't count: they present or re-plan,
            # they never fetch, and the normal shape of a reply is prose +
            # a render_ui in one cycle.
            cycle_answers = not any(
                call.function_name not in _HARNESS_TOOLS for call in tool_calls
            )
            # Narration belongs to the gate row this cycle is about to
            # write (below), IN FRONT of the tool_use it preceded — which
            # is where it actually happened. Accumulating it here too
            # persisted it TWICE, and the LLM sees both copies on replay:
            # once correctly pre-tool, once fused to the answer. Turn 2
            # then mimicked the fused shape and stopped announcing early
            # (measured 2026-08-09: 3/3 sessions, turn 1 announced, turn 2
            # did not). Each piece of prose is written exactly once.
            #
            # Both flags key off approved, visible prose. A lone "\n" or a
            # cycle whose text was entirely <ui_stream> markers carries no
            # prose at all. Three such shapes are live-observed here: an empty Vertex
            # candidate (see the MALFORMED branch below), a <plan>-only
            # cycle when the template has plan_enforcement off, and the
            # marker-mimicry bug (see ``_ui_summary``). Counting one of
            # those as "the model answered" skips the recovery nudge below
            # and lets the turn end with an empty reply.
            cycle_prose = "".join(cycle_approved_chunks).strip()
            if cycle_prose:
                self._turn_prose_streamed = True
                if cycle_answers:
                    assistant_text_chunks.extend(cycle_approved_chunks)
                    answered = True
                    # Mirrored onto the agent so the render_ui handler can
                    # read the same fact — a chips-only call must know
                    # whether a REPLY exists, not merely whether prose does.
                    self._turn_answered = True
            if allowed and not tool_calls:
                if self._in_chips_cycle:
                    # The forced chips cycle produced no call. Never retry
                    # it unforced — an unforced tail cycle would stream a
                    # SECOND prose bubble after the final reply. Skip chips
                    # (observable via force_fallback) and end the turn: the
                    # reply is already on screen and persisted.
                    self._chips_pending = False
                    logger.warning(
                        f"ChatAgent {self.session_id}: final chips cycle "
                        f"returned no call "
                        f"(finish_reason={finish_reason}); skipping chips"
                    )
                    yield SSEEvent(
                        event="force_fallback",
                        data={
                            "allowed": allowed,
                            "finish_reason": finish_reason,
                            "context": "final_quick_replies",
                        },
                    )
                    break
                # Forced cycle produced no function call — the current-gen
                # Gemini failure mode is MALFORMED_FUNCTION_CALL / an empty
                # candidate, not ignoring the constraint. Retry ONCE
                # unforced (+ telemetry) instead of ending the turn broken.
                if not self._force_retry_used:
                    self._force_retry_used = True
                    skip_force_once = True
                    logger.warning(
                        f"ChatAgent {self.session_id}: forced cycle "
                        f"(allowed={allowed}) returned no call "
                        f"(finish_reason={finish_reason}); retrying unforced"
                    )
                    yield SSEEvent(
                        event="force_fallback",
                        data={
                            "allowed": allowed,
                            "finish_reason": finish_reason,
                        },
                    )
                    continue
                logger.error(
                    f"ChatAgent {self.session_id}: forced cycle failed twice "
                    f"(finish_reason={finish_reason}); ending turn unforced"
                )
            if not tool_calls:
                if (
                    self._plan_enforcement
                    and self._plan_enforcer.constraining
                    and not plan_was_constraining
                ):
                    # The plan armed DURING this (unforced) cycle but the
                    # model made no real call — Flash sometimes TYPES the
                    # pseudo-call as prose right after declaring a plan.
                    # Don't end the turn: the next cycle is constrained to
                    # the plan's first step, where mode=ANY produces a real
                    # call or trips the MALFORMED fallback above. Bounded:
                    # that fallback fires at most once, then the turn ends.
                    logger.warning(
                        f"ChatAgent {self.session_id}: plan armed with no "
                        f"call this cycle; continuing into enforced cycle "
                        f"{cycle + 1}"
                    )
                    continue
                if (
                    not answered
                    and not answer_retried
                    and not self._internal_turn
                    and self._turn_prose_streamed
                ):
                    # Narration without an answer: the model opened with
                    # "Let me check…", did its work, and stopped without
                    # replying (live 2026-08-09 — a search that matched
                    # nothing; the opener sat in context and it behaved as
                    # if it had already spoken). Ending here would leave
                    # the shopper an acknowledgement and no reply. Give it
                    # exactly one unforced cycle to answer — bounded like
                    # the MALFORMED-call fallback, so a mute model costs
                    # one extra cycle, never a loop.
                    answer_retried = True
                    # Un-arm prose suppression for the cycle we are about to
                    # buy. Suppression drops text before it is streamed,
                    # accumulated OR persisted, so a recovery cycle that ran
                    # with it still set could not produce a reply by
                    # construction — it would burn an LLM call and end the
                    # turn exactly as mute as before.
                    self._suppress_extra_prose = False
                    logger.warning(
                        f"ChatAgent {self.session_id}: turn produced "
                        "narration but no answer; nudging once"
                    )
                    context.add_message(
                        cast(
                            LLMContextMessage,
                            {"role": "user", "content": _ANSWER_NUDGE},
                        )
                    )
                    await insert_chat_message(
                        session_id=self.session_id,
                        role=ChatMessageRole.USER,
                        content=None,
                        content_blocks=[internal_text_block(_ANSWER_NUDGE)],
                    )
                    continue
                chips_prose = strip_ui_stream_markers(
                    "".join(assistant_text_chunks)
                ).strip()
                if (
                    self._render_ui_enabled
                    and self._quick_replies_mode == "forced_final"
                    and not self._internal_turn
                    and not self._chips_attempted
                    and not self._quick_replies_rendered
                    # `answered`, not `chips_prose` alone: chips follow a
                    # REPLY. Keyed on "is there any text" they would fire on
                    # a turn whose only prose was the opener, ending it with
                    # an acknowledgement and four suggestions.
                    and answered
                    # …and `chips_prose` as well, because this branch
                    # PERSISTS it as the turn's reply row and emits it as
                    # assistant_message. `answered` already implies non-empty
                    # stripped prose; keeping the original guard makes that
                    # an invariant of this branch rather than a fact one has
                    # to re-derive from the flag's definition upstream.
                    and chips_prose
                ):
                    # Forced final chips cycle (template quick_replies=
                    # 'forced_final'): the reply is done — persist it NOW as
                    # its own row and anchor the bubble, so the chips the
                    # NEXT forced cycle authors paint (live) and persist
                    # (resume) strictly BELOW it. One extra cycle, decided
                    # AFTER the prose exists — chips grounded in what was
                    # actually said, and "when do chips come?" becomes
                    # deterministic: every eligible turn ends in QuickReplies
                    # or an explicit no_ui.
                    self._chips_attempted = True
                    self._chips_pending = True
                    prose_blocks = plain_text_blocks(chips_prose)
                    if cycle_context_messages:
                        prose_blocks.extend(
                            gemini_signature_blocks(cycle_context_messages)
                        )
                    stored = await insert_chat_message(
                        session_id=self.session_id,
                        role=ChatMessageRole.ASSISTANT,
                        content=chips_prose,
                        content_blocks=prose_blocks,
                        ui_blocks=self._row_ui_blocks(turn_ui_ops),
                    )
                    early_final_idx = stored.idx if stored else None
                    yield SSEEvent(
                        event="assistant_message",
                        data={"idx": early_final_idx, "content": chips_prose},
                    )
                    assistant_text_chunks.clear()
                    context.add_message(
                        cast(
                            LLMContextMessage,
                            {"role": "assistant", "content": chips_prose},
                        )
                    )
                    if self._held_chips:
                        # Rider flush (2026-08-03): chips were authored
                        # mid-turn and harvested — render them below the
                        # final prose NOW, through the exact validation
                        # path the chips cycle uses, and end the turn.
                        # Saves the forced cycle (~2s tail + one LLM call)
                        # on every turn where the model attached a rider.
                        async for ev in self._flush_held_chips():
                            yield ev
                        if self._quick_replies_rendered:
                            break
                        # Flush rejected (bad labels) — fall through to
                        # the forced cycle rather than ending chipless
                        # (live 2026-08-03: single-label riders died on
                        # the old min_length=2 with no fallback).
                    # No rider — fall back to the forced chips cycle. The
                    # nudge rides an internal USER row (widget read paths
                    # filter it — see _sanitize_messages_for_widget, applied
                    # on both the /chat and /widget resume routes; the LLM
                    # sees it live and on replay — Vertex requires
                    # user/model alternation).
                    context.add_message(
                        cast(
                            LLMContextMessage,
                            {"role": "user", "content": _CHIPS_NUDGE},
                        )
                    )
                    await insert_chat_message(
                        session_id=self.session_id,
                        role=ChatMessageRole.USER,
                        content=None,
                        content_blocks=[internal_text_block(_CHIPS_NUDGE)],
                    )
                    continue
                break

            # Strip <ui_stream> markers before persistence so the LLM
            # doesn't see its own prior JSONL ops on replay. The compact
            # UI summary rides on a separate visibility=internal text
            # block — the LLM keeps the referential memory ("the green
            # one") on its next turn, but every widget-facing read path
            # filters it out so it never shows up in the chat bubble.
            # ``.strip()`` matches the turn-end row's twin: without it a
            # cycle whose only text was whitespace persisted a blank
            # VISIBLE block and a non-null content column — an empty bubble
            # on resume for every turn the model led with a stray newline.
            visible_text = "".join(cycle_approved_chunks).strip()
            ui_summary = self._ui_summary(turn_ui_ops)

            # In-memory LLM context still gets the augmented text — this
            # branch loops back into another LLM call within the same
            # /message, so the model needs the rendered-UI memory now.
            llm_context_text = visible_text
            if ui_summary:
                llm_context_text = (visible_text.rstrip() + "\n\n" + ui_summary).strip()

            # Persist the assistant turn-step with full Anthropic-shape
            # blocks [text? + tool_use*]. This is the load-bearing fix
            # for cross-turn identifier loss — on the next /message the
            # history loader replays tool_use.input verbatim, so the
            # LLM sees its own prior cart_id / checkout_id / etc.
            # Prose on a gate row is ALWAYS demoted to an internal block:
            # streamed text accumulates in assistant_text_chunks and
            # persists visibly ONCE on the turn's user-facing row (the
            # pre-chips prose row or the turn-end row). A visible copy
            # here made resume replay show the reply twice whenever prose
            # preceded a tool call (live 2026-07-31: greeting + banned
            # mid-turn chips shared cycle 1 — the same greeting persisted
            # on this gate row AND the pre-chips row). The LLM still sees
            # internal blocks in replayed context; only widget-facing
            # reads filter them.
            assistant_blocks = assistant_turn_to_blocks("", tool_calls)
            if visible_text:
                # Narration (prose ahead of a DATA call) is the shopper's
                # first bubble and is persisted ONLY here, so it renders on
                # resume exactly where it streamed — above the component.
                # Prose beside a harness-only call (render_ui) is part of
                # the ANSWER, which the turn-end row owns; keep that copy
                # internal or resume replays the reply twice (2026-07-31).
                assistant_blocks.insert(
                    0,
                    (
                        internal_text_block(visible_text)
                        if (cycle_answers or self._internal_turn)
                        else plain_text_blocks(visible_text)[0]
                    ),
                )
            if ui_summary and assistant_blocks:
                # Insert the internal summary block right after the
                # visible text block so concatenation order on read
                # matches what the LLM previously saw in-context.
                insert_at = 1 if assistant_blocks[0].get("type") == "text" else 0
                assistant_blocks.insert(insert_at, internal_text_block(ui_summary))
            if cycle_context_messages and assistant_blocks:
                # Persist this cycle's Gemini thought signatures on the
                # same row as the tool_use blocks they annotate — the
                # history loader decodes them back into LLMSpecificMessage
                # entries adjacent to this assistant message.
                assistant_blocks.extend(gemini_signature_blocks(cycle_context_messages))
            gate_assistant_idx: Optional[int] = None
            if assistant_blocks:
                gate_row = await insert_chat_message(
                    session_id=self.session_id,
                    role=ChatMessageRole.ASSISTANT,
                    # Narration is this row's own bubble, so the
                    # denormalised column carries it. Answer prose still
                    # belongs to the turn's user-facing row (None here) —
                    # see the block-visibility choice above.
                    content=(
                        None
                        if (cycle_answers or self._internal_turn)
                        else (visible_text or None)
                    ),
                    content_blocks=assistant_blocks,
                    # Tool-rendered ops are HELD off mid-turn gate rows (they
                    # used to scatter across whichever gate row came next):
                    # they persist together on the turn's user-facing row —
                    # which also lets a later same-turn ProductGrid merge
                    # mutate the pending op before anything hits the DB.
                    ui_blocks=self._row_ui_blocks(turn_ui_ops, include_tool_ops=False),
                )
                gate_assistant_idx = gate_row.idx if gate_row else None
                if visible_text and not (cycle_answers or self._internal_turn):
                    # This gate row was written VISIBLE — it is the shopper's
                    # narration bubble, a row of its own. Every other visible
                    # assistant row on every surface is announced with an
                    # assistant_message carrying its idx (the early-chips row
                    # above, the turn-end row, the intent router's row); this
                    # one was the sole exception, so the live stream and a
                    # later resume disagreed: one bubble live, two after a
                    # reload, and a client that commits its bubble from
                    # assistant_message.content (the documented contract —
                    # docs/CHAT_MODE.md "Full assistant turn") dropped the
                    # narration text entirely at turn end.
                    yield SSEEvent(
                        event="assistant_message",
                        data={"idx": gate_assistant_idx, "content": visible_text},
                    )

            # Mirror LLMAssistantContextAggregator: append assistant message
            # carrying tool_calls, then a tool-result per call. Universal
            # OpenAI shape; per-provider adapter converts on next request.
            context.add_message(
                cast(
                    LLMContextMessage,
                    {
                        "role": "assistant",
                        "content": llm_context_text or None,
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

            # Generic argument-injection — read template-declared rules
            # and merge session state into outgoing tool args (e.g. a
            # missing cart_id is filled from agent_state.data.cart_id).
            # only_if_missing semantics: the LLM's explicit value wins.
            arg_injection_rules = (
                self.template.configurations.tool_arg_injection
                if self.template.configurations
                else []
            )

            # HITL partition: approval-gated calls do NOT execute now — the
            # turn ends after the ungated siblings finish, and each gated
            # call waits for its decision on the approval endpoint. Node-aware
            # (see _partition_gated_calls): a per-node function shadows a
            # same-named gated global, so it stays UNGATED — matching voice.
            gated_calls, ungated_calls = _partition_gated_calls(
                tool_calls, self._approval_map, node
            )

            if self._suppress_extra_prose and any(
                c.function_name != RENDER_UI_TOOL_NAME for c in tool_calls
            ):
                # The turn wasn't over after all — a REAL tool ran after the
                # banned mid-turn chips attempt, so the model must stay free
                # to narrate its new results (the suppression exists only to
                # kill duplicate sign-off prose).
                self._suppress_extra_prose = False

            next_node: Optional[Dict[str, Any]] = None
            tool_result_pairs: List[Tuple[str, Any]] = []
            # (call, result) in ORIGINAL call order — post-dispatch
            # bookkeeping (binding store, context messages, reducers) is
            # order-sensitive and runs identically for both dispatch modes.
            executed: List[Tuple[FunctionCallFromLLM, Any, Optional[Dict[str, Any]]]]
            if self._should_fan_out(ungated_calls):
                results: Dict[str, Tuple[Any, Optional[Dict[str, Any]]]] = {}
                async for event in self._fan_out_read_only(
                    ungated_calls, node, global_funcs, arg_injection_rules, results
                ):
                    yield event
                executed = [
                    (call, *results[call.tool_call_id]) for call in ungated_calls
                ]
            else:
                executed = []
                # Mutations run SOLO (2026-08-03): in a parallel batch every
                # call was authored from the SAME pre-batch snapshot, so a
                # second state mutation is blind to the first — for UCP
                # carts (full-replace line_items) the second update_cart
                # silently REVERTS the first. Policy: the first mutating
                # call executes; every later mutating call in the batch is
                # deferred with a structured soft error telling the model
                # to re-issue against the fresh result. Reads still execute
                # (post-mutation state is fresher, never stale); harness
                # tools (render_ui / revise_plan) are neutral.
                _NEUTRAL_TOOLS = {RENDER_UI_TOOL_NAME, REVISE_PLAN_TOOL_NAME}
                mutated_by: Optional[str] = None
                for call in ungated_calls:
                    is_mutation = (
                        call.function_name not in _NEUTRAL_TOOLS
                        and not is_read_only(
                            call.function_name, self.template, self._flavor_scope
                        )
                    )
                    if is_mutation and mutated_by is not None:
                        deferred = {
                            "status": "error",
                            "soft": True,
                            "error": (
                                f"not executed — {mutated_by} already changed "
                                "state in this step and this call was authored "
                                "before seeing its result. Review that result "
                                "and re-issue this call if it is still needed."
                            ),
                        }
                        running_label, done_label = resolve_step_label(
                            call.function_name, self._flavor_scope
                        )
                        yield step_started_event(
                            step_id=call.tool_call_id,
                            label=running_label,
                            turn_id=self._turn_id,
                        )
                        executed.append((call, deferred, None))
                        yield SSEEvent(
                            event="function_call_completed",
                            data={
                                "name": call.function_name,
                                "tool_call_id": call.tool_call_id,
                                "result_summary": _summarize_result(deferred),
                            },
                        )
                        yield step_completed_event(
                            step_id=call.tool_call_id,
                            status=resolve_step_status(deferred),
                            label=done_label,
                            summary=None,
                            count=None,
                        )
                        continue
                    if is_mutation:
                        mutated_by = call.function_name
                    injected_args = inject_tool_args(
                        tool_name=call.function_name,
                        args=dict(call.arguments),
                        state_data=self.agent_state,
                        chat_session_id=self.session_id,
                        injections=arg_injection_rules,
                        turn_id=self._turn_id,
                    )
                    # Step-progress layer (widget step lines) — one step per
                    # tool execution, keyed on tool_call_id so step_completed
                    # flips the same line in place. Sits ABOVE
                    # function_call_started/completed (the tool-level wire
                    # events), which stay unchanged.
                    running_label, done_label = resolve_step_label(
                        call.function_name, self._flavor_scope
                    )
                    yield step_started_event(
                        step_id=call.tool_call_id,
                        label=running_label,
                        turn_id=self._turn_id,
                    )
                    result_payload, transition_node = await self._dispatch_tool_call(
                        call, node, global_funcs, injected_args=injected_args
                    )
                    result_payload = self._verify_result(
                        call.function_name, injected_args, result_payload
                    )
                    executed.append((call, result_payload, transition_node))
                    yield SSEEvent(
                        event="function_call_completed",
                        data={
                            "name": call.function_name,
                            "tool_call_id": call.tool_call_id,
                            "result_summary": _summarize_result(result_payload),
                        },
                    )
                    step_summary, step_count = summarize_step_result(
                        result_payload, self._flavor_scope
                    )
                    yield step_completed_event(
                        step_id=call.tool_call_id,
                        status=resolve_step_status(result_payload),
                        label=done_label,
                        summary=step_summary,
                        count=step_count,
                    )
                    # render_ui / revise_plan side effects (hydrated ui ops,
                    # ui_decision / plan_updated events) drain immediately
                    # after the call that produced them — the grid paints
                    # before the next dispatch, not after the cycle.
                    for side_event in self._drain_tool_side_effects():
                        yield side_event

            reducer_rules = (
                self.template.configurations.state_reducers
                if self.template.configurations
                else []
            )
            for call, result_payload, transition_node in executed:
                # RFC-002 bookkeeping. ``success`` = the post-pipeline result
                # passed verification (deterministic gates own step-complete,
                # not the model's say-so). Use the canonical envelope read so
                # the plan enforcer / render_ui arming agree with the step
                # rail, binding store, and reducers — all of which treat BOTH
                # status="error" AND status="failed" as failure (a "failed"
                # backend body reaches here intact through result_normalizer).
                call_success = _is_tool_success(result_payload)
                if self._plan_enforcement:
                    self._plan_enforcer.on_tool_result(call.function_name, call_success)
                if (
                    self._render_ui_enabled
                    and call_success
                    and call.function_name in self._render_ui_force_after
                ):
                    # Forced think-step armed: the NEXT cycle must call
                    # render_ui (render or an explicit, reasoned no_ui) —
                    # unless an enforced plan still has earlier steps.
                    self._need_render_ui_think = True
                if call.function_name == RENDER_UI_TOOL_NAME and call_success:
                    self._need_render_ui_think = False
                # Make this turn's successful post-pipeline result bind-
                # addressable for `show` ops (error envelopes are skipped
                # inside record — a bind can never hydrate a failed call).
                self._binding_store.record(
                    call.function_name, call.tool_call_id, result_payload
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
                # Apply template-declared reducers to lift identifiers
                # off the tool result into session state (e.g.
                # update_cart's cart.id → state.data.cart_id). Engine
                # is commerce-blind; rules live in template JSON.
                self.agent_state = apply_state_reducers(
                    state_data=self.agent_state,
                    tool_name=call.function_name,
                    tool_result=result_payload,
                    reducers=reducer_rules,
                )
                tool_result_pairs.append((call.tool_call_id, result_payload))
                if transition_node is not None and next_node is None:
                    next_node = transition_node
            # Belt-and-braces: side effects appended by any dispatch path
            # that didn't drain inline (fan-out never carries render_ui, but
            # a future path must not silently swallow a rendered op).
            for side_event in self._drain_tool_side_effects():
                yield side_event

            # Persist the coalesced tool_result user-row + the updated
            # session state. Both go to Postgres so a crash before the
            # next LLM call doesn't lose either.
            if tool_result_pairs:
                await insert_chat_message(
                    session_id=self.session_id,
                    role=ChatMessageRole.USER,
                    content=None,
                    content_blocks=tool_results_to_user_blocks(tool_result_pairs),
                )
            # Persist ONLY the keys this turn's reducers changed vs the state
            # loaded at turn start (never the whole row, never the client-
            # context keys) so a concurrent lock-free /context push of an
            # untouched allowlisted key isn't clobbered. Skip the write
            # entirely when nothing changed.
            reducer_patch = diff_state_patch(
                self._loaded_state_baseline, self.agent_state
            )
            if reducer_patch:
                await upsert_agent_session_state_merge(
                    chat_session_id=self.session_id,
                    patch=reducer_patch,
                )

            if next_node is not None:
                node = next_node
                node_name = cast(str, node.get("name") or node_name)
                self._apply_node_transition(context, node, global_funcs)
                yield SSEEvent(event="node_transition", data={"to": node_name})

            if gated_calls:
                # Order is load-bearing: the ungated results + agent state
                # are already persisted above (their side effects ran), and
                # any ungated transition has been applied to ``node_name``.
                # Now record each gated call as PENDING and end the turn —
                # the decision arrives on POST .../session/{id}/approval.
                pending_ids: List[str] = []
                for call in gated_calls:
                    approval_cfg = self._approval_map[call.function_name]
                    # Inject NOW so the persisted row holds exactly the
                    # arguments that will run on approval (idempotency hash
                    # bakes in this turn's id — resume replays it verbatim).
                    injected_args = inject_tool_args(
                        tool_name=call.function_name,
                        args=dict(call.arguments),
                        state_data=self.agent_state,
                        chat_session_id=self.session_id,
                        injections=arg_injection_rules,
                        turn_id=self._turn_id,
                    )
                    row = await insert_tool_approval(
                        session_id=self.session_id,
                        tool_call_id=call.tool_call_id,
                        function_name=call.function_name,
                        arguments=injected_args,
                        prompt=approval_cfg.prompt,
                        expiry_secs=approval_cfg.chat_expiry_secs,
                    )
                    pending_ids.append(call.tool_call_id)
                    yield SSEEvent(
                        event="function_approval_requested",
                        data={
                            "tool_call_id": call.tool_call_id,
                            "name": call.function_name,
                            "args": injected_args,
                            "prompt": approval_cfg.prompt,
                            "expires_at": (row.expires_at.isoformat() if row else None),
                        },
                    )

                await update_chat_session_after_turn(
                    session_id=self.session_id, current_node=node_name or None
                )
                # The response-end Guardrail path already drained the UI
                # extractor before any tool dispatch.
                self._ui_extractor.flush()
                # ``assistant_idx`` carries the gate-time assistant row so
                # turn metrics persist (the turn DID consume an LLM call)
                # and the client has a stable anchor for the partial bubble.
                yield SSEEvent(
                    event="turn_end",
                    data={
                        "session_status": "ACTIVE",
                        "assistant_idx": gate_assistant_idx,
                        "awaiting_approval": True,
                        "pending_tool_call_ids": pending_ids,
                    },
                )
                return

            if self._chips_attempted:
                # A forced chips cycle just dispatched. Chips are the turn's
                # LAST frame by design — a resolved outcome (QuickReplies
                # rendered or an explicit no_ui) ends the turn with no
                # further LLM cycle. An invalid call got a structured error
                # response instead: allow exactly ONE corrective forced
                # cycle, then give up (chips skipped, turn still healthy).
                if not self._chips_pending or self._chips_cycles >= 2:
                    self._chips_pending = False
                    break
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
            # Flush extractor state — any unclosed <ui_stream> block is
            # dropped with a log warning by the extractor itself.
            for _ in self._ui_extractor.flush():
                pass
            yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
            return

        # The response-end Guardrail path already drained the UI extractor
        # before tool dispatch and persistence.
        self._ui_extractor.flush()

        # Reconstruct prose-only history (strips every
        # <ui_stream>…</ui_stream>) so saved messages never carry SpecStream
        # ops forward into future turns. A compact UI summary rides on a
        # separate visibility=internal block so the LLM keeps referential
        # memory of what the shopper saw ("the green one"), while every
        # widget-facing read path filters it out. The SSE wire and the
        # denormalised `content` column both carry visible prose only.
        visible_text = strip_ui_stream_markers("".join(assistant_text_chunks)).strip()
        ui_summary = self._ui_summary(turn_ui_ops)
        persisted_blocks: List[Dict[str, Any]] = []
        if visible_text:
            # Internal turns (enrich_product's overlay blurb): the prose
            # streamed live into the overlay but must never replay as a
            # thread bubble — persist it internal-only so the LLM keeps
            # the context while resume filters the row out.
            if self._internal_turn:
                persisted_blocks.append(internal_text_block(visible_text))
            else:
                persisted_blocks.extend(plain_text_blocks(visible_text))
        if ui_summary:
            persisted_blocks.append(internal_text_block(ui_summary))
        if cycle_context_messages and persisted_blocks:
            # Final cycle's Gemini thought signatures (text-bookmarked —
            # the last cycle produced no tool calls). Best-effort memory
            # for later turns; skipped when there's no row to ride on
            # (Vertex only enforces signatures on functionCall parts).
            persisted_blocks.extend(gemini_signature_blocks(cycle_context_messages))
        final_assistant_idx: Optional[int] = None
        final_ui_blocks = self._row_ui_blocks(turn_ui_ops)
        if not persisted_blocks and final_ui_blocks:
            # render_ui-only turn with no prose at all: persist a row anyway
            # so the resume path can repaint the tool-rendered UI.
            persisted_blocks = []
        if persisted_blocks or final_ui_blocks:
            stored = await insert_chat_message(
                session_id=self.session_id,
                role=ChatMessageRole.ASSISTANT,
                content=None if self._internal_turn else (visible_text or None),
                content_blocks=persisted_blocks,
                ui_blocks=final_ui_blocks,
            )
            final_assistant_idx = stored.idx if stored else None
            # Only emit a bubble when there's actual visible prose. A
            # summary-only row (the LLM rendered UI without narrating)
            # still gets persisted for next-turn LLM memory but doesn't
            # create an empty chat bubble on the wire.
            if visible_text:
                yield SSEEvent(
                    event="assistant_message",
                    data={
                        "idx": final_assistant_idx,
                        "content": visible_text,
                    },
                )

        await update_chat_session_after_turn(
            session_id=self.session_id, current_node=node_name or None
        )
        if early_final_idx is not None:
            # Forced-final chips path: the prose row (persisted early, its
            # bubble already anchored via assistant_message) is the turn's
            # user-facing message — keep it as the metrics/anchor idx even
            # when a chips-only ui row was written after it.
            final_assistant_idx = early_final_idx
        # ``assistant_idx`` (additive) keys this turn's metrics row
        # (chat_turn_metrics, migration 032) to the assistant message it
        # produced — including UI-only turns that emit no assistant_message
        # bubble. ``None`` when the turn produced no assistant row. Existing
        # clients ignore the extra field.
        yield SSEEvent(
            event="turn_end",
            data={"session_status": "ACTIVE", "assistant_idx": final_assistant_idx},
        )

    def _plan_sse(self: "ChatAgent", plan: List[str]) -> SSEEvent:
        """Build the plan_started / plan_updated event for one parsed
        ``<plan>`` declaration. Labels resolve through the same step-label
        registry the live step lines use, so a pending skeleton line and
        the step_started that later claims it render identically."""
        seq = self._plans_emitted
        self._plans_emitted += 1
        steps = []
        for i, tool in enumerate(plan):
            running_label, _done = resolve_step_label(tool, self._flavor_scope)
            steps.append(
                {"id": f"plan-{seq}-{i}", "tool": tool, "label": running_label}
            )
        return plan_event(
            steps=steps,
            turn_id=getattr(self, "_turn_id", None),
            revised=seq > 0,
        )

    # ------------------------------------------------------------------
    # RFC-002: render_ui / revise_plan handlers + side-effect drains
    # ------------------------------------------------------------------

    def _known_tool_names(
        self: "ChatAgent", node: Dict[str, Any], global_funcs: List[FlowsFunctionSchema]
    ) -> Set[str]:
        """Every function name callable this turn (per-node + globals) —
        the universe plans are validated against."""
        names = {fn.name for fn in global_funcs}
        names.update(
            fn.name
            for fn in (node.get("functions") or [])
            if isinstance(fn, FlowsFunctionSchema)
        )
        return names

    def _drain_tool_side_effects(self: "ChatAgent") -> List[SSEEvent]:
        """Hand-off from tool handlers (which cannot yield) to the SSE
        stream: companion events first (ui_decision / plan_updated), then
        each hydrated op — which also queues for ui_blocks persistence."""
        events: List[SSEEvent] = []
        while self._pending_tool_sse:
            events.append(self._pending_tool_sse.pop(0))
        while self._pending_ui_ops:
            op = self._pending_ui_ops.pop(0)
            self._unpersisted_tool_ui_ops.append(op)
            events.append(SSEEvent(event="ui_op", data={"op": op}))
        return events

    def _ui_summary(self: "ChatAgent", turn_ui_ops: List[Dict[str, Any]]) -> str:
        """The legacy ``[ui rendered: …]`` marker for this row — or nothing.

        RFC-002 Phase B: render_ui sessions NEVER write the marker. Their
        UI memory is the render_ui function response (replayed as a native
        function_call/response pair), and the marker in replayed history is
        what bred the F1 mimicry bug — the model typing the marker instead
        of rendering. Fleet text-channel sessions keep it: it's still their
        only cross-turn record of what the shopper saw."""
        if self._render_ui_enabled:
            return ""
        return summarize_ui_ops(turn_ui_ops)

    def _row_ui_blocks(
        self: "ChatAgent",
        turn_ui_ops: List[Dict[str, Any]],
        include_tool_ops: bool = True,
    ) -> Optional[List[Dict[str, Any]]]:
        """ui_blocks for the assistant row being persisted RIGHT NOW:
        text-channel ops from the current cycle + (unless the caller is a
        mid-turn gate row) any render_ui-hydrated ops not yet persisted
        (cleared here — each op persists exactly once, on the turn's
        user-facing row). Holding tool ops off gate rows keeps them
        mutable in memory for same-turn ProductGrid merges."""
        tool_ops: List[Dict[str, Any]] = []
        if include_tool_ops:
            tool_ops = self._unpersisted_tool_ui_ops
            self._unpersisted_tool_ui_ops = []
        if self._internal_turn:
            return None
        merged = [*turn_ui_ops, *tool_ops]
        return merged or None
