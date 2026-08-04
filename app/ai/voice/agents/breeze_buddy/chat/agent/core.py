"""ChatAgent — the single-turn chat brain, assembled from subsystem
mixins (split from the monolithic agent.py, 2026-08-05). ONE class,
ONE instance per turn; the mixins are file organization, not
architecture — all state lives here in ``__init__``."""

import copy
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from app.ai.voice.agents.breeze_buddy.chat.agent.approval import ApprovalTurnMixin
from app.ai.voice.agents.breeze_buddy.chat.agent.context import ContextSeedMixin
from app.ai.voice.agents.breeze_buddy.chat.agent.cycle import CycleLoopMixin
from app.ai.voice.agents.breeze_buddy.chat.agent.direct import DirectDispatchMixin
from app.ai.voice.agents.breeze_buddy.chat.agent.render_ui import RenderUiHandlerMixin
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
from app.ai.voice.agents.breeze_buddy.chat.agent.tooling import ToolDispatchMixin
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    internal_text_block,
    plain_text_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import (
    SSEEvent,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.enforcer import PlanEnforcer
from app.ai.voice.agents.breeze_buddy.chat.steps.plan import PlanExtractor
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    BindingStore,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    resolve_render_ui_flavor_pack,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    UiStreamExtractor,
)
from app.ai.voice.agents.breeze_buddy.mcp import (
    MCPPool,
    close_mcp_pool,
)
from app.ai.voice.agents.breeze_buddy.template.approval import build_approval_map
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    CATALOG_VERSION_V2,
    data_bound_names,
    resolve_allowlist,
)
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.chat_session import (
    insert_chat_message,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole


class ChatAgent(
    ApprovalTurnMixin,
    ContextSeedMixin,
    CycleLoopMixin,
    DirectDispatchMixin,
    RenderUiHandlerMixin,
    ToolDispatchMixin,
):
    """Single-turn driver. Construct, ``run_turn``, discard."""

    # Shared lifecycle attrs also assigned from mixin methods —
    # annotated identically here and there (pyrefly override parity).
    aiohttp_session: Optional[Any]
    mcp_pool: Optional[Dict[str, Any]]
    _held_chips: Optional[List[str]]

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
        # Enabled flavor groups, forwarded to the prompt builder: the
        # render_ui section body is the flavor's registered vocabulary
        # (ui_prompt.register_render_ui_flavor_section), and the
        # resolve_allowlist call above has already lazy-imported those
        # flavor packages — registration is in place before any splice.
        self._ui_flavor_groups: List[str] = (
            list(ui_cat.enabled_groups or []) if ui_cat is not None else []
        )
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
        render_ui_cfg = getattr(configurations, "render_ui", None)
        self._render_ui_enabled = (
            bool(getattr(render_ui_cfg, "enabled", False)) and self._catalog_v2
        )
        # Forced think-step tools: template config wins; absent, the
        # enabled flavor's pack default applies (commerce: search_catalog).
        # No flavor, no config → no forced think — the engine names no
        # tools of its own.
        force_after = getattr(render_ui_cfg, "force_after", None)
        if force_after is None:
            _pack = resolve_render_ui_flavor_pack(self._ui_flavor_groups)
            force_after = _pack.default_force_after if _pack else None
        self._render_ui_force_after: Set[str] = set(force_after or [])
        # LinkButton URL allowlist (template config) — the other trusted
        # source is THIS turn's tool results, checked at execute time.
        self._trusted_link_urls: Set[str] = set(
            getattr(render_ui_cfg, "trusted_link_urls", None) or []
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
        # First rendered op per component this turn. When the flavor pack
        # declares a repeat-merge policy (commerce: a SECOND ProductGrid
        # merges value-level into the first — one combined display per
        # turn, never stacked surfaces), later renders of the same
        # component fold into the stored op.
        self._turn_merge_ops: Dict[str, Dict[str, Any]] = {}
        # First rendered op of a turn anchors the widget's per-turn UI tree
        # as id="root"; later ops parent under it (ui_state.svelte.ts).
        self._turn_rendered_root = False
        self._rui_seq = 0
        # Forced think-step state: a successful force_after tool arms it;
        # a VALID render_ui payload (rendered or no_ui) disarms it.
        self._need_render_ui_think = False
        self._force_retry_used = False
        # LLM QuickReplies placement policy (template `render_ui.
        # quick_replies`; distinct from top-level `quick_replies`, the
        # static widget-open chicklets): 'forced_final' bans mid-turn
        # QuickReplies and appends ONE forced render_ui cycle after the
        # turn's final prose — the chips slot accepts QuickReplies or
        # no_ui only, so chips always paint (and persist) BELOW the
        # reply. 'off' removes the component entirely. Absent/
        # 'model_choice' = today's behavior.
        self._quick_replies_mode: str = (
            getattr(render_ui_cfg, "quick_replies", None) or "model_choice"
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

    @property
    def binding_store(self) -> BindingStore:
        """This turn's tool-result binding store (show-op hydration source)."""
        return self._binding_store

    @property
    def ui_allowlist(self) -> Set[str]:
        """The resolved (and catalog-version-pruned) primitive allowlist."""
        return self._ui_allowlist
