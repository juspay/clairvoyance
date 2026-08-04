"""ChatAgent — direct LLM driver for one chat turn.

Stateless per turn: a fresh ``ChatAgent`` is constructed per ``POST /message``,
drives one user → assistant turn, then is discarded. The agent is a thin loop
around :func:`llm_driver.stream` — see ``docs/CHAT_MODE.md`` §4 + §9 for the
full architecture.
"""

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple, cast

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    diff_state_patch,
    render_client_context,
)
from app.ai.voice.agents.breeze_buddy.chat.disabled import CHAT_DISABLED_NAMES
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    assistant_turn_to_blocks,
    internal_text_block,
    plain_text_blocks,
    tool_results_to_user_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver
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
from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    build_kb_system_message,
    build_retrieval_query,
    fetch_full_kb_text_cached,
    fetch_kb_context_message,
    resolve_kb_runtime,
)
from app.ai.voice.agents.breeze_buddy.template.approval import build_approval_map
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
    upsert_agent_session_state_merge,
)
from app.database.accessor.breeze_buddy.tool_approvals import insert_tool_approval
from app.schemas.breeze_buddy.chat import ChatMessageRole, ToolApproval

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


def _partition_gated_calls(
    tool_calls: List[Any],
    approval_map: Dict[str, Any],
    node: Dict[str, Any],
) -> Tuple[List[Any], List[Any]]:
    """Split a tool-call batch into (gated, ungated) for HITL.

    A gated name shadowed by a per-node function in the CURRENT node is
    treated as UNGATED: in that node the LLM calls the per-node function
    (which the author did not gate), not the gated global of the same name.
    Non-shadow nodes are unaffected — a gated global is still gated. This
    keeps chat consistent with voice, whose wrapper gates only globals.
    """
    # ``node["functions"]`` holds FlowsFunctionSchema objects in flow mode
    # (FlowConfigBuilder._build_node runs every per-node function through
    # _build_function_schema) — NOT plain dicts. Match the idiom used by
    # _dispatch_tool_call / _tools_schema below: filter on FlowsFunctionSchema
    # and read ``.name``. The builder renames function_name→name before the
    # schema exists, so there is no alias to fall back to here.
    node_fn_names = {
        fn.name
        for fn in (node.get("functions") or [])
        if isinstance(fn, FlowsFunctionSchema)
    }
    gated = [
        c
        for c in tool_calls
        if c.function_name in approval_map and c.function_name not in node_fn_names
    ]
    ungated = [
        c
        for c in tool_calls
        if c.function_name not in approval_map or c.function_name in node_fn_names
    ]
    return gated, ungated


@dataclass
class _PreparedTools:
    """Per-turn tool surface shared by ``run_turn`` and ``run_approval_turn``."""

    flow_config: Dict[str, Any]
    global_funcs: List[FlowsFunctionSchema]
    tool_retention: Optional[Dict[str, str]]
    tool_projection: Optional[Dict[str, List[str]]]


@dataclass
class _KbMessage:
    """This turn's ephemeral knowledge base message + where it seeds.

    ``prefix`` = right after task messages (full injection, stable across
    turns → prompt-cache friendly). ``tail`` = just before the user turn
    (per-turn retrieved chunks). Never persisted to chat_message.
    """

    message: Dict[str, Any]
    placement: str  # "prefix" | "tail"


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
        # Snapshot of the state as loaded at turn start. The turn writer
        # persists only the keys its reducers CHANGED vs this baseline (see
        # ``diff_state_patch``), not the whole loaded row — so it can't
        # re-assert a stale allowlisted key (e.g. ``cart_id``) and clobber a
        # concurrent lock-free ``/context`` push of that same key. Deep-copied
        # so an in-place mutation of a nested state value can never make the
        # baseline compare equal to a genuinely-changed current value.
        self._loaded_state_baseline: Dict[str, Any] = copy.deepcopy(self.agent_state)
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
        # HITL: chat gates approval pre-dispatch (Pattern B — the turn ends
        # at the gated call; the decision arrives on the dedicated approval
        # endpoint). This flag makes the voice in-handler gate
        # (template/approval.py) a pass-through for ChatAgent, since the
        # global-function adapters receive bot_instance=self and would
        # otherwise double-gate.
        self.handles_approval_externally = True
        # Chat runs inject_tool_args / apply_state_reducers itself inside
        # _cycle_loop; this flag tells the shared global-function wrapper NOT
        # to re-apply them (voice has no such loop, so it lets the wrapper do
        # it). Prevents double-application of the SessionStatePolicy.
        self.handles_state_externally = True
        # function name -> ApprovalConfig for every gated global function.
        self._approval_map = build_approval_map(self.template.flow or {})

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

        # Knowledge base context for this turn. In-memory only — like the
        # client-context blocks it is EPHEMERAL (never persisted to
        # chat_message), so nothing leaks into replayed history. Chat can
        # afford a looser retrieval budget than voice.
        kb_message = await self._prepare_kb_message(user_content, history)

        context = self._seed_context(
            node, history, user_content, prep.global_funcs, kb_message=kb_message
        )
        async for event in self._cycle_loop(context, node, prep):
            yield event

    async def _prepare_kb_message(
        self,
        user_content: str,
        history: List[Dict[str, Any]],
    ) -> Optional[_KbMessage]:
        """Resolve the template's KB mode and fetch this turn's KB message.

        Returns None when no KB is attached or on any failure (fail-open).
        Tool mode needs nothing here — the builder synthesizes the
        query_knowledge_base function instead.
        """
        try:
            runtime = await resolve_kb_runtime(
                self.template.configurations if self.template else None
            )
            if runtime is None or runtime.mode == "tool":
                return None
            if runtime.mode == "full_injection":
                text = await fetch_full_kb_text_cached(runtime.config)
                if not text:
                    return None
                return _KbMessage(
                    message=build_kb_system_message(text, runtime.config),
                    placement="prefix",
                )
            # auto_retrieve
            recent_user_turns = [
                m["content"]
                for m in history
                if isinstance(m, dict)
                and m.get("role") == "user"
                and isinstance(m.get("content"), str)
            ]
            query = build_retrieval_query(
                user_content, recent_user_turns, runtime.config
            )
            message = await fetch_kb_context_message(runtime.config, query, timeout=1.0)
            if message is None:
                return None
            return _KbMessage(message=message, placement="tail")
        except Exception as e:
            logger.warning(
                f"ChatAgent {self.session_id}: KB prep failed (continuing): {e}"
            )
            return None

    async def _prepare_tools(self) -> _PreparedTools:
        """Build the per-turn tool surface (flow config, wrapped handlers,
        global + MCP functions, retention policy). Shared by ``run_turn``
        and ``run_approval_turn``."""
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
        """The LLM ↔ tool loop for one turn (shared by ``run_turn`` and the
        continuation of ``run_approval_turn``). ``context`` must already be
        seeded; this loop drives LLM cycles, dispatches ungated tool calls,
        and ends the turn early (``turn_end {awaiting_approval}``) when the
        LLM calls an approval-gated function."""
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
            gate_assistant_idx: Optional[int] = None
            if assistant_blocks:
                gate_row = await insert_chat_message(
                    session_id=self.session_id,
                    role=ChatMessageRole.ASSISTANT,
                    content=visible_text or None,
                    content_blocks=assistant_blocks,
                    ui_blocks=turn_ui_ops or None,
                )
                gate_assistant_idx = gate_row.idx if gate_row else None

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

            next_node: Optional[Dict[str, Any]] = None
            tool_result_pairs: List[Tuple[str, Any]] = []
            for call in ungated_calls:
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
                # Drain any held marker carry (mirrors the normal turn end).
                for out in self._ui_extractor.flush():
                    if isinstance(out, TextOut):
                        yield SSEEvent(
                            event="assistant_token", data={"delta": out.value}
                        )
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

    async def run_approval_turn(
        self,
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
        self,
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
        if approved:
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

        async for event in self._cycle_loop(context, node, prep):
            yield event

    async def _persist_tool_result_row(
        self, tool_call_id: str, result_payload: Any
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

    async def _prepare_kb_message_for_resume(self) -> Optional["_KbMessage"]:
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
        self,
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
        stt_config = (
            getattr(self.template.configurations, "stt_configuration", None)
            if self.template.configurations
            else None
        )
        payload_selection = False
        if stt_config is not None:
            payload_selection = stt_config.payload_based_language_selection
        else:
            payload_selection = getattr(
                getattr(self.template, "configurations", None),
                "payload_based_language_selection",
                False,
            )

        role_messages = inject_language_rules(
            role_messages,
            self.template_vars.get("language_name", "English"),
            payload_selection,
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
        kb_message: Optional["_KbMessage"] = None,
    ) -> LLMContext:
        """Build initial LLMContext: [role, task, kb_full?, …history…,
        system_block?, kb_chunks?, new user].

        Client-pushed facts (offers, cart summary, …) are rendered here:
        ``user_block`` rides the user turn as untrusted data (cache-safe,
        injection-safe); ``system_block`` (trusted, opt-in) is appended as
        a system message right before the user turn. Both are EPHEMERAL —
        never persisted to chat_message, re-derived from agent_state every
        turn so they always reflect current truth (latest-wins).

        KB placement follows the same logic: full-injection text sits right
        after task messages (stable across turns → prompt-cache friendly);
        per-turn retrieved chunks sit just before the user turn (fresh each
        turn, invalidates only the suffix). Both are ephemeral like the
        client-context blocks.
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
        ]
        if kb_message is not None and kb_message.placement == "prefix":
            messages.append(kb_message.message)
        messages.extend(history)
        if system_block:
            messages.append({"role": "system", "content": system_block})
        if kb_message is not None and kb_message.placement == "tail":
            messages.append(kb_message.message)
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
