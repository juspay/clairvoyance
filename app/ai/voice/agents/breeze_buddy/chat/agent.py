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
from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMContextMessage,
    LLMSpecificMessage,
)
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
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.signatures import (
    gemini_signature_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import (
    SSEEvent,
    plan_event,
    step_completed_event,
    step_started_event,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.enforcer import PlanEnforcer
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    resolve_step_label,
    resolve_step_status,
    summarize_step_result,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.plan import PlanExtractor
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
    BindingStore,
    resolve_show_op,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.healer import (
    HealerContext,
    make_healer_fn,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    RENDER_UI_TOOL_NAME,
    REVISE_PLAN_TOOL_NAME,
    build_render_ui_schema,
    build_revise_plan_schema,
    execute_render_ui,
    render_ui_components,
    summarize_render,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    ShowResolverFn,
    TextOut,
    UiStreamExtractor,
    process_op_line,
    strip_ui_stream_markers,
    summarize_ui_ops,
    ui_op_dropped_event,
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
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    CATALOG_VERSION_V2,
    data_bound_names,
    resolve_allowlist,
)
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

# The forced final chips cycle's user-role nudge (quick_replies=
# 'forced_final'). It rides an internal USER row so live context and
# next-turn replay stay identical AND user/model alternation holds around
# the chips function call (Vertex rejects consecutive model contents);
# widget-facing read paths filter internal blocks, so it never shows.
_CHIPS_NUDGE = (
    "(final check: call render_ui with quick_replies=[2-4 SHORT follow-ups "
    "(<=4 words each) grounded in your reply], or decision='no_ui'. Never "
    "duplicate an action already available on UI rendered this turn — "
    "cards already carry Add to cart / View.)"
)


def _chip_labels(raw: Any) -> List[str]:
    """Lift a `quick_replies` arg (strings canonical; {'label': …} dicts
    tolerated) into clean labels — same tolerance as execute_render_ui's
    extraction, kept tiny here for the rider-harvest path."""
    if not isinstance(raw, list):
        return []
    labels: List[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            labels.append(entry.strip())
        elif isinstance(entry, dict) and isinstance(entry.get("label"), str):
            if entry["label"].strip():
                labels.append(entry["label"].strip())
    return labels[:5]


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
        catalog_version: Optional[str] = None,
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
        # Plan-as-emission (Phase 2): strips <plan>["tool",…]</plan>
        # declarations from the prose stream → plan_started/plan_updated
        # SSE. Counter distinguishes the first declaration from revisions.
        self._plan_extractor = PlanExtractor()
        self._plans_emitted = 0
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
        # Catalog-version negotiation (RFC-001 §3.4). Only sessions that
        # declared "v2" at create time may see data-bound components: on a
        # v1 (or unversioned / voice) session they're pruned from the
        # allowlist, which simultaneously (a) keeps the "Data-bound
        # components" prompt subsection out of the system prompt, (b) drops
        # LLM `show` ops with `show_component_disabled`, and (c) leaves v1
        # templates rendering exactly as before (no prompt regression).
        self._catalog_v2 = catalog_version == CATALOG_VERSION_V2
        if not self._catalog_v2:
            self._ui_allowlist -= data_bound_names()
        # Per-turn record of successful post-pipeline tool results, keyed
        # (tool_name, tool_use_id) — what `show`-op bindings resolve
        # against. Populated in _cycle_loop after each ungated dispatch;
        # a fresh agent per turn means the store can never leak stale
        # results across turns (the UI-freshness invariant, RFC-001 §8).
        self._binding_store = BindingStore()
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
        # Internal AGENT_TURN intent turn (IntentPolicy.internal — e.g.
        # enrich_product's overlay blurb): every row this turn persists is
        # visibility=internal (content=None, internal text blocks, no
        # ui_blocks) and no user_committed fires. The live SSE stream is
        # unchanged — the widget owns routing. Set per-turn by run_turn.
        self._internal_turn = False
        # --- RFC-002: render_ui function tool + forced think-step + plan
        # enforcement. All template-gated; fleet templates without the flags
        # behave exactly as before.
        configurations = self.template.configurations
        self._render_ui_enabled = (
            bool(getattr(configurations, "render_ui_tool", None)) and self._catalog_v2
        )
        force_after = getattr(configurations, "render_ui_force_after", None)
        self._render_ui_force_after: Set[str] = set(force_after or ["search_catalog"])
        # LinkButton URL allowlist (template config) — the other trusted
        # source is THIS turn's tool results, checked at execute time.
        self._trusted_link_urls: Set[str] = set(
            getattr(configurations, "trusted_link_urls", None) or []
        )
        self._plan_enforcement = bool(getattr(configurations, "plan_enforcement", None))
        self._plan_enforcer = PlanEnforcer()
        self._plan_known_tools: Set[str] = set()
        # Handler → cycle-loop hand-off (tool handlers cannot yield SSE):
        # hydrated render_ui ops + companion events (ui_decision,
        # plan_updated) drain right after the dispatching call completes.
        self._pending_ui_ops: List[Dict[str, Any]] = []
        self._pending_tool_sse: List[SSEEvent] = []
        # Hydrated render_ui ops awaiting persistence — held until the
        # turn's USER-FACING row (gate rows skip them via include_tool_ops=
        # False), then cleared, so resume repaints them exactly once.
        self._unpersisted_tool_ui_ops: List[Dict[str, Any]] = []
        # The turn's rendered ProductGrid op (first one wins). A SECOND
        # ProductGrid render this turn merges into it value-level — one
        # combined product display per turn, never stacked surfaces.
        self._turn_grid_op: Optional[Dict[str, Any]] = None
        # First rendered op of a turn anchors the widget's per-turn UI tree
        # as id="root"; later ops parent under it (ui_state.svelte.ts).
        self._turn_rendered_root = False
        self._rui_seq = 0
        # Forced think-step state: a successful force_after tool arms it;
        # a VALID render_ui payload (rendered or no_ui) disarms it.
        self._need_render_ui_think = False
        self._force_retry_used = False
        # LLM QuickReplies placement policy (template `quick_replies_mode`;
        # distinct from `quick_replies`, the static widget-open chicklets):
        # 'forced_final' bans mid-turn QuickReplies and appends ONE forced
        # render_ui cycle after the turn's final prose — the chips slot
        # accepts QuickReplies or no_ui only, so chips always paint (and
        # persist) BELOW the reply. 'off' removes the component entirely.
        # Absent/'model_choice' = today's behavior.
        self._quick_replies_mode: str = (
            getattr(configurations, "quick_replies_mode", None) or "model_choice"
        )
        # Forced final chips-cycle state (all per-turn):
        # _chips_pending    — the NEXT cycle is (or the current cycle IS)
        #                     the forced chips cycle; cleared by a resolved
        #                     outcome (QuickReplies rendered / no_ui) or by
        #                     the give-up paths.
        # _chips_attempted  — chips entry happened this turn (one-shot).
        # _chips_cycles     — forced chips cycles consumed (hard bound: 2 —
        #                     one shot + one structured-error correction).
        # _in_chips_cycle   — cycle-scoped marker the render_ui handler
        #                     reads to apply the chips-slot restriction.
        self._chips_pending = False
        self._chips_attempted = False
        self._chips_cycles = 0
        self._in_chips_cycle = False
        self._quick_replies_rendered = False
        # _turn_prose_streamed — visible prose reached the client this turn.
        # _suppress_extra_prose — set when a chips-only call was harvested
        #   AFTER prose already streamed: the reply is delivered, so any
        #   FURTHER prose the model generates before turn end is duplicate
        #   sign-off and gets dropped (the old ban's error text bred a
        #   rephrased duplicate greeting — live 2026-07-31).
        # _held_chips — rider-harvested quick replies (chips attached to a
        #   mid-turn render_ui call, or a chips-only call): flushed below
        #   the final prose at turn end, skipping the forced chips cycle.
        self._turn_prose_streamed = False
        self._suppress_extra_prose = False
        self._held_chips: Optional[List[str]] = None

    async def run_turn(
        self,
        *,
        user_content: str,
        history: List[Dict[str, Any]],
        current_node: Optional[str],
        internal: bool = False,
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
        self._internal_turn = internal
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
        # single source of truth on the next turn. Internal turns persist
        # the instruction as an internal-only block (resume replay skips
        # the row; the LLM still sees it) and commit nothing to the wire.
        if self._internal_turn:
            await insert_chat_message(
                session_id=self.session_id,
                role=ChatMessageRole.USER,
                content=None,
                content_blocks=[internal_text_block(user_content)],
            )
        else:
            user_msg = await insert_chat_message(
                session_id=self.session_id,
                role=ChatMessageRole.USER,
                content=user_content,
                content_blocks=plain_text_blocks(user_content),
            )
            yield SSEEvent(
                event="user_committed",
                data={
                    "idx": user_msg.idx if user_msg else None,
                    "content": user_content,
                },
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
        flow_builder = FlowConfigBuilder(
            disabled_names=CHAT_DISABLED_NAMES,
            quiet=True,
            render_ui_mode=self._render_ui_enabled,
            quick_replies_mode=self._quick_replies_mode,
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

    async def _cycle_loop(
        self,
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
            turn_text: List[str] = []
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

            async for kind, payload in llm_driver.stream(
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
            ):
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

            # Release the plan extractor's held tail (a partial "<plan"
            # prefix is ordinary prose; an unterminated block is dropped) —
            # it flows through the SAME ui-extractor path as live chunks.
            plan_tail = self._plan_extractor.flush()
            if plan_tail and not suppress_cycle_prose and not self._in_chips_cycle:
                turn_text.append(plan_tail)
                for out in self._ui_extractor.feed(plan_tail):
                    if isinstance(out, TextOut):
                        yield SSEEvent(
                            event="assistant_token", data={"delta": out.value}
                        )

            assistant_text_chunks.extend(turn_text)
            if turn_text:
                self._turn_prose_streamed = True
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
                chips_prose = strip_ui_stream_markers(
                    "".join(assistant_text_chunks)
                ).strip()
                if (
                    self._render_ui_enabled
                    and self._quick_replies_mode == "forced_final"
                    and not self._internal_turn
                    and not self._chips_attempted
                    and not self._quick_replies_rendered
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
                    # filter it; the LLM sees it live and on replay —
                    # Vertex requires user/model alternation).
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
            visible_text = strip_ui_stream_markers("".join(turn_text))
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
                assistant_blocks.insert(0, internal_text_block(visible_text))
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
                    # content stays None: the visible copy of any prose
                    # belongs to the turn's user-facing row (see the
                    # internal-demotion comment above).
                    content=None,
                    content_blocks=assistant_blocks,
                    # Tool-rendered ops are HELD off mid-turn gate rows (they
                    # used to scatter across whichever gate row came next):
                    # they persist together on the turn's user-facing row —
                    # which also lets a later same-turn ProductGrid merge
                    # mutate the pending op before anything hits the DB.
                    ui_blocks=self._row_ui_blocks(turn_ui_ops, include_tool_ops=False),
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
                        and not is_read_only(call.function_name, self.template)
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
                            call.function_name
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
                    running_label, done_label = resolve_step_label(call.function_name)
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
                    step_summary, step_count = summarize_step_result(result_payload)
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
                # not the model's say-so).
                call_success = not (
                    isinstance(result_payload, dict)
                    and result_payload.get("status") == "error"
                )
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
        approved_done_label: Optional[str] = None
        if approved:
            # Step-progress line for the approved execution — the resume
            # turn brackets its tool run exactly like _cycle_loop does, so
            # the widget's step list covers HITL resumes too.
            running_label, approved_done_label = resolve_step_label(
                approval.function_name
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
            step_summary, step_count = summarize_step_result(result_payload)
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

    # ------------------------------------------------------------------
    # Direct (no-LLM) dispatch surface — used by chat/intent_router.py.
    #
    # The intent router executes whitelisted cart tools through the SAME
    # machinery an LLM turn uses (builder-wrapped handlers running
    # apply_result_pipeline, inject_tool_args, state reducers, binding
    # store) without ever constructing an LLM client. These are thin
    # public seams over the private per-turn internals so the router
    # doesn't reach into them.
    # ------------------------------------------------------------------

    @property
    def binding_store(self) -> BindingStore:
        """This turn's tool-result binding store (show-op hydration source)."""
        return self._binding_store

    @property
    def ui_allowlist(self) -> Set[str]:
        """The resolved (and catalog-version-pruned) primitive allowlist."""
        return self._ui_allowlist

    async def prepare_direct_dispatch(
        self, current_node: Optional[str]
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
        self,
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

    def _plan_sse(self, plan: List[str]) -> SSEEvent:
        """Build the plan_started / plan_updated event for one parsed
        ``<plan>`` declaration. Labels resolve through the same step-label
        registry the live step lines use, so a pending skeleton line and
        the step_started that later claims it render identically."""
        seq = self._plans_emitted
        self._plans_emitted += 1
        steps = []
        for i, tool in enumerate(plan):
            running_label, _done = resolve_step_label(tool)
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
        self, node: Dict[str, Any], global_funcs: List[FlowsFunctionSchema]
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

    def _drain_tool_side_effects(self) -> List[SSEEvent]:
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

    async def _flush_held_chips(self) -> AsyncIterator[SSEEvent]:
        """Render rider-harvested quick replies below the final prose (the
        chips slot), persisted as their own chips-only row — the forced
        chips cycle never runs for this turn. Same validation path as a
        chips-cycle call; any failure degrades to no chips, never a
        failed turn."""
        labels = self._held_chips or []
        self._held_chips = None
        self._chips_attempted = True
        try:
            outcome = execute_render_ui(
                {"component": "QuickReplies", "quick_replies": labels},
                store=self._binding_store,
                allowlist=self._ui_allowlist,
                components=["QuickReplies"],
                op_id="root",
                parent=None,
                trusted_urls=self._trusted_link_urls,
                restrict_to={"QuickReplies"},
                state_values=self.agent_state,
            )
        except Exception:  # noqa: BLE001 — chips are decoration
            logger.exception(f"ChatAgent {self.session_id}: rider chips flush failed")
            return
        if outcome.decision != "rendered" or not outcome.ops:
            logger.warning(
                f"ChatAgent {self.session_id}: rider chips did not render "
                f"({outcome.decision}) — turn ends without chips"
            )
            return
        await insert_chat_message(
            session_id=self.session_id,
            role=ChatMessageRole.ASSISTANT,
            content=None,
            content_blocks=None,
            ui_blocks=outcome.ops,
        )
        self._quick_replies_rendered = True
        for op in outcome.ops:
            yield SSEEvent(event="ui_op", data={"op": op})

    def _ui_summary(self, turn_ui_ops: List[Dict[str, Any]]) -> str:
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
        self, turn_ui_ops: List[Dict[str, Any]], include_tool_ops: bool = True
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

    async def _render_ui_handler(
        self, args: Dict[str, Any], _flow_manager: Any = None
    ) -> Dict[str, Any]:
        """The ``render_ui`` tool handler — a thin wrapper over the existing
        hydration machinery. Returns the compact function response (the
        model's UI memory); hydrated ops ride ``_pending_ui_ops`` to the
        cycle loop's drain."""
        # Turn-level guard (live-observed 2026-07-29): mode=ANY can make
        # Flash spam DUPLICATE render_ui calls in one response, and replayed
        # duplicates then breed more (mimicry). Every call still gets a
        # response (function_call/response pairing is sacred), but past the
        # cap the response is a hard redirect to prose — deterministic loop
        # breaker. The forced final chips cycle is exempt (it has its own
        # 2-cycle bound) — a UI-heavy turn must not lose its chips to calls
        # it already spent mid-turn.
        chips_cycle = self._in_chips_cycle
        self._rui_calls_this_turn = getattr(self, "_rui_calls_this_turn", 0) + 1
        if self._rui_calls_this_turn > 3 and not chips_cycle:
            return {
                "status": "error",
                "error": (
                    "render_ui already resolved this turn — do NOT call it "
                    "again; reply to the shopper in one short line of prose "
                    "now."
                ),
            }
        if chips_cycle and not self._chips_pending:
            # Duplicate call inside the SAME forced chips cycle (mode=ANY
            # spam): the chips slot already resolved — hard stop, and no
            # second component can ride the tail. ``soft``: the chips DID
            # render; the step rail must not paint a failure.
            return {
                "status": "error",
                "soft": True,
                "error": (
                    "quick replies already resolved — the turn is done; do "
                    "not call render_ui again."
                ),
            }
        # Rider harvest (2026-08-03 — replaces the mid-turn chips BAN):
        # chips are an annotation the model may attach to any render_ui
        # call; the server owns placement. A mid-turn `quick_replies` arg
        # (with a real component, with component=QuickReplies, or alone)
        # is HELD and flushed below the turn's final prose — skipping the
        # forced end-of-turn cycle entirely. The old ban wasted the call,
        # cost an extra cycle, and its error text derailed the model
        # (double-greeting family, live 2026-07-31).
        raw_args = dict(args or {})
        if self._quick_replies_mode == "forced_final" and not chips_cycle:
            rider_raw = raw_args.pop("quick_replies", None)
            rider_labels = _chip_labels(rider_raw)
            if rider_labels:
                self._held_chips = rider_labels  # last-wins across the turn
            if raw_args.get("component") == "QuickReplies" or (
                raw_args.get("component") is None and rider_raw is not None
            ):
                # Chips-only call: nothing else to render. Positive result
                # (not an error — errors bred rephrased-reply rambles); if
                # the reply already streamed, trailing prose is duplicate
                # sign-off and gets suppressed.
                if self._turn_prose_streamed:
                    self._suppress_extra_prose = True
                return {
                    "status": "ok",
                    "deferred": True,
                    "note": (
                        "follow-ups saved — they will appear under your "
                        "final reply automatically; do not re-author them"
                    ),
                }
        elif (
            chips_cycle
            and raw_args.get("component") is None
            and raw_args.get("quick_replies")
        ):
            # Componentless chips call is the canonical chips-cycle shape
            # now that QuickReplies left the schema enum.
            raw_args["component"] = "QuickReplies"
        components = render_ui_components(self._ui_allowlist, self._catalog_v2)
        if self._quick_replies_mode == "off":
            components = [c for c in components if c != "QuickReplies"]
        if chips_cycle:
            # Chips are the turn's LAST frame in their OWN thread block —
            # the SDK splits ui blocks at the final bubble, so the chips op
            # must anchor a fresh tree (root, no parent), never join the
            # mid-turn tree that painted above the prose. Persistence
            # agrees: the chips op rides its own chips-only assistant row.
            op_id, parent = "root", None
        elif self._turn_rendered_root:
            self._rui_seq += 1
            op_id, parent = f"rui{self._rui_seq}", "root"
        else:
            op_id, parent = "root", None
        # CartView's checkout button is server policy (mirrors the DIRECT
        # path): label + state-fallback url from the template's intent_tools
        # block; execute prefers the bound payload's continue_url.
        intent_tools = getattr(self.template.configurations, "intent_tools", None)
        state_keys = (
            (getattr(intent_tools, "state_keys", None) or {}) if intent_tools else {}
        )
        labels = (getattr(intent_tools, "labels", None) or {}) if intent_tools else {}
        fallback_url = self.agent_state.get(
            state_keys.get("checkout_url", "checkout_url")
        )
        outcome = execute_render_ui(
            raw_args,
            store=self._binding_store,
            allowlist=self._ui_allowlist,
            components=components,
            op_id=op_id,
            parent=parent,
            trusted_urls=self._trusted_link_urls,
            restrict_to={"QuickReplies"} if chips_cycle else None,
            cart_checkout={
                "label": labels.get("checkout"),
                "url": fallback_url if isinstance(fallback_url, str) else None,
            },
            state_values=self.agent_state,
        )
        if (
            outcome.decision == "rendered"
            and outcome.component == "ProductGrid"
            and outcome.ops
        ):
            new_op = outcome.ops[0]
            if self._turn_grid_op is None:
                self._turn_grid_op = new_op
            else:
                # SECOND ProductGrid this turn: merge value-level into the
                # existing grid (works across different searches — hydrated
                # values need no bind re-resolution), restamp layout from
                # the combined count, and swap the wire op for a `replace`
                # on the existing node — the shopper sees ONE combined
                # display, never stacked product surfaces. The previous op
                # is still the pending in-memory dict (gate rows hold tool
                # ops back), so persistence gets the merged grid too.
                prev = self._turn_grid_op
                prev_props = dict(prev.get("props") or {})
                prev_products = [
                    p for p in (prev_props.get("products") or []) if isinstance(p, dict)
                ]
                seen_ids = {p.get("id") for p in prev_products}
                extra = [
                    p
                    for p in ((new_op.get("props") or {}).get("products") or [])
                    if isinstance(p, dict) and p.get("id") not in seen_ids
                ]
                merged_products = (prev_products + extra)[:12]
                prev_props["products"] = merged_products
                prev_props["layout"] = (
                    "grid" if len(merged_products) <= 2 else "carousel"
                )
                prev["props"] = prev_props
                outcome.ops = [
                    {"op": "replace", "id": prev["id"], "props": prev_props, "v": 2}
                ]
                outcome.fn_result = summarize_render(
                    "ProductGrid", {"props": prev_props}
                )
                outcome.fn_result["merged"] = (
                    "combined with this turn's earlier product display — "
                    "one display per turn"
                )
        if outcome.ops:
            self._pending_ui_ops.extend(outcome.ops)
            if op_id == "root":
                self._turn_rendered_root = True
        if outcome.decision == "rendered" and outcome.component == "QuickReplies":
            self._quick_replies_rendered = True
        if chips_cycle and outcome.decision in ("rendered", "no_ui"):
            # The chips slot resolved (chips painted or an explicit,
            # reasoned no-chips) — the cycle loop ends the turn on this.
            self._chips_pending = False
        decision_data: Dict[str, Any] = {"decision": outcome.decision}
        if outcome.component:
            decision_data["component"] = outcome.component
        if outcome.reason:
            decision_data["reason"] = outcome.reason[:200]
        self._pending_tool_sse.append(SSEEvent(event="ui_decision", data=decision_data))
        return outcome.fn_result

    async def _revise_plan_handler(
        self, args: Dict[str, Any], _flow_manager: Any = None
    ) -> Dict[str, Any]:
        """``revise_plan`` — the only path off an enforced plan. Replaces
        the REMAINING steps, queues the plan_updated SSE (honest step
        rail), and reports the effective remainder back to the model."""
        if not (self._plan_enforcement and self._plan_enforcer.active):
            return {"status": "error", "error": "no active plan to revise"}
        steps = args.get("steps") if isinstance(args, dict) else None
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            return {
                "status": "error",
                "error": "steps must be a list of tool names (may be empty)",
            }
        self._plan_enforcer.revise(steps, self._plan_known_tools)
        remaining = self._plan_enforcer.steps[self._plan_enforcer.cursor :]
        self._pending_tool_sse.append(self._plan_sse(remaining))
        return {"status": "ok", "remaining_steps": remaining}

    def _verify_result(
        self, tool_name: str, injected_args: Dict[str, Any], result_payload: Any
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

    def _should_fan_out(self, calls: List[FunctionCallFromLLM]) -> bool:
        """True when this cycle's ungated batch dispatches CONCURRENTLY:
        2+ calls, every one annotated ``read_only`` (template overrides >
        flavor registry > destructive default), and the template hasn't
        disabled the fan-out (``configurations.parallel_read_only=false``).
        Mutations never fan out — order is meaningful."""
        if len(calls) < 2:
            return False
        configurations = getattr(self.template, "configurations", None)
        if getattr(configurations, "parallel_read_only", None) is False:
            return False
        return all(is_read_only(c.function_name, self.template) for c in calls)

    async def _fan_out_read_only(
        self,
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

    def _show_resolver(self) -> Optional[ShowResolverFn]:
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
