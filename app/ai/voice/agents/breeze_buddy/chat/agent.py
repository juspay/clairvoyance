"""ChatAgent — direct LLM driver for one chat turn.

Stateless per turn: a fresh ``ChatAgent`` is constructed per ``POST /message``,
drives one user → assistant turn, then is discarded. The agent is a thin loop
around :func:`llm_driver.stream` — see ``docs/CHAT_MODE.md`` §4 + §9 for the
full architecture.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple, cast

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat import llm_driver
from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    assistant_turn_to_blocks,
    internal_text_block,
    plain_text_blocks,
    tool_results_to_user_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.client_context import render_client_context
from app.ai.voice.agents.breeze_buddy.chat.disabled import CHAT_DISABLED_NAMES
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.tool_result_normalizer import normalize
from app.ai.voice.agents.breeze_buddy.chat.ui_healer import (
    HealerContext,
    make_healer_fn,
)
from app.ai.voice.agents.breeze_buddy.chat.ui_stream import (
    TextOut,
    UiStreamExtractor,
    process_op_line,
    strip_ui_stream_markers,
    summarize_ui_ops,
)
from app.ai.voice.agents.breeze_buddy.mcp import (
    MCPPool,
    close_mcp_pool,
    get_mcp_global_functions_cached,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import with_context
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    apply_state_reducers,
    inject_tool_args,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import resolve_allowlist
from app.ai.voice.agents.breeze_buddy.template.utils import render_messages_with_vars
from app.ai.voice.agents.breeze_buddy.utils.language_utils.prompt_injections import (
    inject_language_rules,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.chat_session import (
    insert_chat_message,
    update_chat_session_after_turn,
    upsert_agent_session_state,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole

# Each tool-call → handler → re-invoke counts as one cycle. The guard stops a
# pathological template (handler always returns a transition that loops back)
# from burning unbounded LLM calls. Set to 20 (was 8): legitimate multi-item
# flows — e.g. "build a pink combo: top + bottom + socks" — fan out several
# searches plus cart calls in a single turn and were tripping the old cap
# mid-task, ending the turn with no reply. Identity projection (see
# ``tool_context_projection``) keeps prior-search data in context so the model
# stops re-searching what it already found, which keeps real turns well under
# this ceiling; 20 is headroom, not a target.
_MAX_TOOL_CYCLES = 20


@dataclass
class _PreparedTools:
    """Per-turn tool surface built once by ``_prepare_tools``."""

    flow_config: Dict[str, Any]
    global_funcs: List[FlowsFunctionSchema]
    tool_retention: Optional[Dict[str, str]]
    tool_projection: Optional[Dict[str, List[str]]]


class ChatAgent:
    """Single-turn driver. Construct, ``run_turn``, discard."""

    def __init__(
        self,
        *,
        session_id: str,
        template: TemplateModel,
        llm: Any,
        template_vars: Optional[dict] = None,
        agent_state: Optional[Dict[str, Any]] = None,
        context_placement: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.template = template
        self.template_vars = template_vars or {}
        self._llm = llm
        # Generic per-session state dict — the canonical store for any
        # identifiers the template's reducers care about (cart_id,
        # checkout_id, etc.). Loaded from agent_session_state on turn
        # start; mutated by apply_state_reducers after each tool result;
        # upserted to Postgres before the next turn. Empty dict on first
        # turn or when the row doesn't exist yet.
        self.agent_state: Dict[str, Any] = dict(agent_state or {})
        # Client-pushed context (storefront → /widget/session/{id}/context).
        # Policy comes off the template; ``context_placement`` is an optional
        # per-turn render override carried by a piggyback ``context.placement``.
        # Both feed render_client_context in _seed_context. config None →
        # feature inert (no allowlist).
        self._client_context_config = (
            self.template.configurations.client_context
            if self.template.configurations
            else None
        )
        self._context_placement = context_placement
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
        # Per-turn MCP client pool — one MCPClient per server, opened on
        # the first tool call and reused for every subsequent call within
        # the same turn. Closed in run_turn's finally so the StreamableHTTP
        # connection is released even if the SSE consumer disconnects
        # mid-stream. ``None`` between turns; ``{}`` while a turn is live.
        self.mcp_pool: Optional[MCPPool] = None
        # Streaming extractor for <ui_stream>…</ui_stream> blocks in
        # assistant text. Each complete JSONL line inside the marker is
        # healed (S1.2) → catalog-validated → emitted as a `ui_op` SSE event.
        self._ui_extractor = UiStreamExtractor()
        # Session-wide UI tree id registry. The healer uses this to detect
        # duplicate ids (and rename to ``id__2`` / ``id__3``). Across-turn
        # persistence is deferred until session UI state hydration ships
        # — for now ids only collide within a single turn.
        self._known_ui_ids: Set[str] = set()
        # Per-template primitive allowlist resolved from
        # ``configurations.ui_catalog``. Drives server-side primitive_disabled
        # validation drops. Templates without ui_catalog config default to
        # the 'core' group via ``resolve_allowlist``.
        ui_cat = (
            self.template.configurations.ui_catalog
            if self.template.configurations
            else None
        )
        if ui_cat is not None:
            self._ui_allowlist: Set[str] = resolve_allowlist(
                enabled_groups=ui_cat.enabled_groups,
                enabled_primitives=ui_cat.enabled_primitives,
                disabled_primitives=ui_cat.disabled_primitives,
            )
        else:
            self._ui_allowlist = resolve_allowlist()

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
        # Per-turn MCP client pool — same lifecycle pattern. Empty dict
        # so handlers can stash clients on first acquire; closed below.
        self.mcp_pool = {}
        # Per-turn identifier for the idempotency-hash injection generator
        # (see template/session_state.py:_gen_idempotency_hash). Every
        # tool dispatch within this turn shares this id, so a retry of
        # the same intent produces the same idempotency key. The next
        # turn gets a fresh uuid → different key → upstream treats it
        # as a new operation even with identical args.
        self._turn_id = uuid.uuid4().hex
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
            await close_mcp_pool(self.mcp_pool)
            self.mcp_pool = None

    async def _run_turn_inner(
        self,
        *,
        user_content: str,
        history: List[Dict[str, Any]],
        current_node: Optional[str],
    ) -> AsyncIterator[SSEEvent]:
        prep = await self._prepare_tools()
        node = self._resolve_node(prep.flow_config, current_node)

        # Persist user message before the LLM call so a crash mid-stream
        # still leaves the user's input in history. We also write the
        # canonical Anthropic-shape [text] block so the loader has a
        # single source of truth on the next turn.
        user_msg = await insert_chat_message(
            session_id=self.session_id,
            role=ChatMessageRole.USER,
            content=user_content,
            content_blocks=plain_text_blocks(user_content),
        )
        yield SSEEvent(
            event="user_committed",
            data={"idx": user_msg.idx if user_msg else None, "content": user_content},
        )

        context = self._seed_context(node, history, user_content, prep.global_funcs)
        async for event in self._cycle_loop(context, node, prep):
            yield event

    async def _prepare_tools(self) -> _PreparedTools:
        """Build the per-turn tool surface (flow config, wrapped handlers,
        global + MCP functions, retention policy). Extracted from
        ``_run_turn_inner`` so the seeding and the cycle loop stay readable."""
        flow_builder = FlowConfigBuilder(disabled_names=CHAT_DISABLED_NAMES, quiet=True)
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
            mcp_funcs = await get_mcp_global_functions_cached(
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

    async def _cycle_loop(
        self,
        context: LLMContext,
        node: Dict[str, Any],
        prep: _PreparedTools,
    ) -> AsyncIterator[SSEEvent]:
        """The LLM ↔ tool loop for one turn, extracted from
        ``_run_turn_inner``. ``context`` must already be seeded; this loop
        drives LLM cycles and dispatches tool calls until the model
        produces a user-facing reply (or the cycle cap trips)."""
        global_funcs = prep.global_funcs
        node_name = cast(str, node["name"])

        assistant_text_chunks: List[str] = []
        # ui_ops accumulator (Sprint 1.7) — captures each SpecStream op the
        # LLM emits during this user turn. Persisted alongside the assistant
        # message so the widget resume path can repaint Tiles/Carousels
        # after a page refresh. Reset per cycle so each persisted row
        # carries only the ops produced by THAT cycle.
        turn_ui_ops: List[Dict[str, Any]] = []
        for cycle in range(1, _MAX_TOOL_CYCLES + 1):
            tool_calls: List[FunctionCallFromLLM] = []
            turn_text: List[str] = []
            turn_ui_ops = []

            async for kind, payload in llm_driver.stream(
                self._llm,
                context,
                log_label=f"chat#{self.session_id[:8]}",
                tool_context_retention=prep.tool_retention,
                tool_context_projection=prep.tool_projection,
            ):
                if kind == "text":
                    text = cast(str, payload)
                    turn_text.append(text)
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
                            yield SSEEvent(
                                event="assistant_token", data={"delta": out.value}
                            )
                        else:
                            for ev in process_op_line(
                                out.raw,
                                session_state=self.agent_state,
                                healer=healer,
                                known_ids=self._known_ui_ids,
                                allowlist=self._ui_allowlist,
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

            # Strip <ui_stream> markers before persistence so the LLM
            # doesn't see its own prior JSONL ops on replay. The compact
            # UI summary rides on a separate visibility=internal text
            # block — the LLM keeps the referential memory ("the green
            # one") on its next turn, but every widget-facing read path
            # filters it out so it never shows up in the chat bubble.
            visible_text = strip_ui_stream_markers("".join(turn_text))
            ui_summary = summarize_ui_ops(turn_ui_ops)

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
            assistant_blocks = assistant_turn_to_blocks(visible_text, tool_calls)
            if ui_summary and assistant_blocks:
                # Insert the internal summary block right after the
                # visible text block so concatenation order on read
                # matches what the LLM previously saw in-context.
                insert_at = 1 if assistant_blocks[0].get("type") == "text" else 0
                assistant_blocks.insert(insert_at, internal_text_block(ui_summary))
            if assistant_blocks:
                await insert_chat_message(
                    session_id=self.session_id,
                    role=ChatMessageRole.ASSISTANT,
                    content=visible_text or None,
                    content_blocks=assistant_blocks,
                    ui_blocks=turn_ui_ops or None,
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

            next_node: Optional[Dict[str, Any]] = None
            tool_result_pairs: List[Tuple[str, Any]] = []
            for call in tool_calls:
                injected_args = inject_tool_args(
                    tool_name=call.function_name,
                    args=dict(call.arguments),
                    state_data=self.agent_state,
                    chat_session_id=self.session_id,
                    injections=arg_injection_rules,
                    turn_id=self._turn_id,
                )
                result_payload, transition_node = await self._dispatch_tool_call(
                    call, node, global_funcs, injected_args=injected_args
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
                reducer_rules = (
                    self.template.configurations.state_reducers
                    if self.template.configurations
                    else []
                )
                self.agent_state = apply_state_reducers(
                    state_data=self.agent_state,
                    tool_name=call.function_name,
                    tool_result=result_payload,
                    reducers=reducer_rules,
                )
                tool_result_pairs.append((call.tool_call_id, result_payload))
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
            await upsert_agent_session_state(
                chat_session_id=self.session_id,
                data=self.agent_state,
            )

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
            # Flush extractor state — any unclosed <ui_stream> block is
            # dropped with a log warning by the extractor itself.
            for _ in self._ui_extractor.flush():
                pass
            yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
            return

        # Drain any held marker carry. Trailing prose held mid-marker is
        # forwarded; an unmatched <ui_stream> open is dropped (with warning).
        for out in self._ui_extractor.flush():
            if isinstance(out, TextOut):
                yield SSEEvent(event="assistant_token", data={"delta": out.value})
            # JsonlOpLine items from flush() shouldn't happen — defensive
            # path drops them (flush only ever yields TextOut today).

        # Reconstruct prose-only history (strips every
        # <ui_stream>…</ui_stream>) so saved messages never carry SpecStream
        # ops forward into future turns. A compact UI summary rides on a
        # separate visibility=internal block so the LLM keeps referential
        # memory of what the shopper saw ("the green one"), while every
        # widget-facing read path filters it out. The SSE wire and the
        # denormalised `content` column both carry visible prose only.
        visible_text = strip_ui_stream_markers("".join(assistant_text_chunks)).strip()
        ui_summary = summarize_ui_ops(turn_ui_ops)
        persisted_blocks: List[Dict[str, Any]] = []
        if visible_text:
            persisted_blocks.extend(plain_text_blocks(visible_text))
        if ui_summary:
            persisted_blocks.append(internal_text_block(ui_summary))
        final_assistant_idx: Optional[int] = None
        if persisted_blocks:
            stored = await insert_chat_message(
                session_id=self.session_id,
                role=ChatMessageRole.ASSISTANT,
                content=visible_text or None,
                content_blocks=persisted_blocks,
                ui_blocks=turn_ui_ops or None,
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
        # ``assistant_idx`` (additive) keys this turn's metrics row
        # (chat_turn_metrics, migration 032) to the assistant message it
        # produced — including UI-only turns that emit no assistant_message
        # bubble. ``None`` when the turn produced no assistant row. Existing
        # clients ignore the extra field.
        yield SSEEvent(
            event="turn_end",
            data={"session_status": "ACTIVE", "assistant_idx": final_assistant_idx},
        )

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
        """Build initial LLMContext: [role, task, …history…, new user].

        Client-pushed facts (offers, cart summary, …) are rendered here:
        ``user_block`` rides the user turn as untrusted data (cache-safe,
        injection-safe); ``system_block`` (trusted, opt-in) is appended as
        a system message right before the user turn. Both are EPHEMERAL —
        never persisted to chat_message, re-derived from agent_state every
        turn so they always reflect current truth (latest-wins).
        """
        role_messages, task_messages = self._render_node_messages(node)
        user_block, system_block = render_client_context(
            self.agent_state,
            self._client_context_config,
            self._context_placement,
        )
        user_text = f"{user_block}\n\n{user_content}" if user_block else user_content
        messages: List[Dict[str, Any]] = [
            *role_messages,
            *task_messages,
            *history,
        ]
        if system_block:
            messages.append({"role": "system", "content": system_block})
        messages.append({"role": "user", "content": user_text})
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
            return normalize(call.function_name, raw[0]), raw[1]
        return normalize(call.function_name, raw), None


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
