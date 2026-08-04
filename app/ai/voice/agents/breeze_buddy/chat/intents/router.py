"""Typed UI intents — the widget → server action channel (RFC-001 §3.3).

Catalog-v2 data-bound components emit ``{intent, component_id, payload,
display}`` actions instead of free-text ``to_assistant`` messages. This
module is the **flavor-agnostic engine**:

- The wire model + :func:`parse_ui_intent` (→ 422-shaped
  :class:`IntentValidationError` on anything malformed, unknown, or not
  enabled for the session's template).
- The **intent policy registry** — which intents execute directly (no LLM
  call), which convert into a structured agent turn, and which are
  client-side only. The registry starts EMPTY; flavor packages under
  ``breeze_buddy/assist/`` populate it via :func:`register_intents` when
  :func:`ensure_flavor_intents` lazily imports them (mirror of
  ``ui_catalog.ensure_group_loaded`` for schemas).
- :func:`run_direct_intent` — the direct executor shell: the policy's
  flavor-provided ``drive`` callable runs whitelisted tools through the
  EXISTING pipeline (``inject_tool_args`` → MCP dispatch →
  ``apply_result_pipeline`` → ``apply_state_reducers``, via
  ``ChatAgent.run_direct_tool`` / :func:`run_persisted_tool`) with **no
  LLM client constructed**, then a hydrated component (the policy's
  ``show_op``) + ``turn_end`` streams over the same SSE shape a chat turn
  uses.

Like the session-state / ui-stream engines, this module is deliberately
flavor-blind: tool names, payload schemas, and which component gets
emitted live in the flavor's own ``intents`` module and register here on
flavor load. Sessions whose template doesn't enable a flavor get the typed 422
(``unknown_intent`` when the flavor was never loaded in this process,
``flavor_not_enabled`` when it was loaded by another template) — either
way the intent never executes.
"""

from __future__ import annotations

import copy
import importlib
import json
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Type,
)

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import is_approval_gated
from app.ai.voice.agents.breeze_buddy.chat.client_context import diff_state_patch
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    assistant_turn_to_blocks,
    internal_text_block,
    plain_text_blocks,
    tool_results_to_user_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
    build_render_template_vars,
    resolve_session_catalog_version,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import resolve_show_op
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    summarize_ui_ops,
    ui_op_dropped_event,
    ui_op_event,
)
from app.ai.voice.agents.breeze_buddy.mcp import close_mcp_pool
from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached

# Same envelope helper the reducer engine + binding store use — one
# definition of "success" across every result consumer.
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _is_tool_success,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.chat_session import (
    get_agent_session_state,
    get_chat_session_by_id,
    insert_chat_message,
    update_chat_session_after_turn,
    upsert_agent_session_state_merge,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole, ChatSessionStatus

# ---------------------------------------------------------------------------
# Wire model
# ---------------------------------------------------------------------------


class UiIntent(BaseModel):
    """The ``ui_intent`` body variant on ``POST /widget/session/{id}/message``
    (RFC-001 §3.3). ``payload`` is validated per-intent (see the policy
    table); ``display`` becomes the visible user bubble."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(..., min_length=1, max_length=64)
    component_id: str = Field(..., min_length=1, max_length=128)
    payload: Dict[str, Any] = Field(default_factory=dict)
    display: Optional[str] = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# Policy registry — populated by flavor packages on lazy load
# ---------------------------------------------------------------------------


class IntentRoute(str, Enum):
    """How an intent is served (RFC-001 §3.3 policy table)."""

    DIRECT = "direct"  # whitelisted tool execution, no LLM call
    AGENT_TURN = "agent_turn"  # rewritten into a structured user turn
    CLIENT = "client"  # widget-side only; server never executes


# A DIRECT policy's tool driver: yields ``(sse_event, final_tool_name,
# final_result)`` triples — events stream as they happen; the final
# tool/result pair rides the last dispatch (see ``run_direct_intent``).
DirectDriveFn = Callable[
    ["ChatAgent", Any, Dict[str, Any], "ParsedIntent", str],
    AsyncIterator[Tuple[Optional[SSEEvent], Optional[str], Any]],
]

# Builds the server-authored ``show`` op over the final (tool_name,
# result, agent) — hydrated via the same resolver a LLM `show` op uses.
# The agent carries both ``agent_state`` (reducer output) and ``template``
# (per-template ui_intents overrides).
ShowOpBuilderFn = Callable[[str, Any, "ChatAgent"], Dict[str, Any]]

# Post-success follow-up surface (a flavor's "and here's a related thing"
# panel): runs AFTER the ack + primary component are on the wire, may
# dispatch its own tools through the same pipeline, and returns ONE
# fully-resolved ui op (or None for silence). The engine persists it as its
# own assistant row and streams it before turn_end. Must be fail-open — a
# follow-up is decoration, never allowed to fail the mutation turn.
FollowupFn = Callable[
    ["ChatAgent", Any, Dict[str, Any], "ParsedIntent", str, str, Any],
    Awaitable[Optional[Dict[str, Any]]],
]


@dataclass(frozen=True)
class IntentPolicy:
    """One intent's routing + validation + (flavor-provided) execution.

    ``flavor`` is stamped by :func:`register_intents` — sessions only see
    intents whose flavor their template enables. DIRECT policies carry
    ``drive`` (+ optionally ``show_op``); AGENT_TURN policies carry
    ``agent_turn`` (the structured-user-message rewriter).
    """

    route: IntentRoute
    payload_model: Type[BaseModel]
    flavor: str = ""
    default_display: Optional[str] = None
    agent_turn: Optional[Callable[["ParsedIntent"], str]] = None
    drive: Optional[DirectDriveFn] = None
    show_op: Optional[ShowOpBuilderFn] = None
    # Optional flavor-provided extractor: a user-displayable reason off a
    # FAILED final result (upstream backends often carry a precise one in a
    # warnings/messages array). None / raising / no match → the generic
    # copy. Only display-grade upstream text should come back from this —
    # never raw error internals.
    failure_message: Optional[Callable[[Any], Optional[str]]] = None
    # Optional flavor-provided acknowledgement off the SUCCESSFUL final
    # (parsed, tool_name, result): one short user-facing line ("Done.")
    # persisted as the turn's assistant prose ALONGSIDE any rendered
    # component — it keeps the turn visibly anchored after a later turn's
    # component sweeps this one. None / raising → no bubble.
    ack_message: Optional[Callable[["ParsedIntent", str, Any], Optional[str]]] = None
    # Optional post-success follow-up (see FollowupFn): streamed and
    # persisted AFTER the primary component so the user never waits on
    # it. Skipped for silent policies (nothing visible to follow up on).
    followup: Optional[FollowupFn] = None
    # Silent intents (a detail fetch behind a full-panel overlay, say)
    # leave NO visible trace in the thread: the user record persists
    # internal-only (no bubble, no user_committed event) and the hydrated
    # component is streamed live but never persisted as a replayable ui
    # block. The tool exchange still persists — the agent keeps the context.
    silent: bool = False
    # Internal AGENT_TURN intents run a REAL LLM turn whose entire exchange
    # persists with visibility=internal: the rewritten user instruction and
    # the assistant prose stay in the LLM's context on later turns, but
    # resume replay never shows them and no user_committed fires. The live
    # SSE stream is unchanged — the widget routes the tokens wherever it
    # wants them. DIRECT policies use `silent` instead; `internal` is the
    # agent-turn counterpart.
    internal: bool = False


# Process-global registry. Starts empty; ``register_intents`` populates it
# when a flavor's intents module is (lazily) imported. Additive-only —
# per-session gating is the ``enabled_flavors`` check in parse_ui_intent.
INTENT_POLICY: Dict[str, IntentPolicy] = {}

# Flavor → intents module, the intent-side mirror of
# ``ui_catalog.LAZY_GROUPS`` (which maps the same flavor names to schema
# modules). Kept separate so a chat session that never sends a ui_intent
# loads only the schemas, and vice versa.
FLAVOR_INTENT_MODULES: Dict[str, str] = {
    "commerce": "app.ai.voice.agents.breeze_buddy.assist.commerce.intents",
}

# Attempted flavors — successes AND failures. A failed import must not be
# retried per request: Python drops the half-initialised module from
# sys.modules, so an unguarded retry re-raises on every call for the life of
# the process. A broken flavor needs a deploy, not another import.
_LOADED_FLAVOR_INTENTS: Set[str] = set()


def ensure_flavor_intents(flavors: Iterable[str]) -> None:
    """Idempotently import (→ register) the intent modules for ``flavors``.

    Unknown flavor names are ignored — callers pass a template's enabled
    UI-catalog groups verbatim (``core`` etc. simply have no intents).

    Fail-open by contract: a flavor module that is absent (its layer has not
    shipped yet) or raises on import leaves its intents unregistered, which
    is precisely the ``unknown_intent`` 422 this module already documents —
    the same answer a template gets for an intent nobody ever defined.
    Propagating the ImportError instead would turn a deployment gap into an
    unhandled 500 on a public widget route.
    """
    for flavor in flavors:
        module_path = FLAVOR_INTENT_MODULES.get(flavor)
        if module_path is None or flavor in _LOADED_FLAVOR_INTENTS:
            continue
        try:
            importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 — fail-open; intents 422 instead
            logger.exception(
                f"intents: flavor {flavor!r} failed to load from "
                f"{module_path!r}; its intents stay unregistered and will "
                "answer 'unknown_intent'"
            )
        # Marked either way — see _LOADED_FLAVOR_INTENTS.
        _LOADED_FLAVOR_INTENTS.add(flavor)


def register_intents(flavor: str, policies: Dict[str, IntentPolicy]) -> None:
    """Register a flavor's intent policies (called at flavor import time).

    Stamps ``flavor`` onto each policy. Idempotent for re-registration of
    the same flavor; a cross-flavor name collision raises — intent names
    are a global namespace on the wire.
    """
    for name, policy in policies.items():
        existing = INTENT_POLICY.get(name)
        if existing is not None and existing.flavor != flavor:
            raise ValueError(
                f"intent {name!r} already registered by flavor "
                f"{existing.flavor!r}; refusing {flavor!r}"
            )
        INTENT_POLICY[name] = replace(policy, flavor=flavor)


def template_enabled_flavors(template: Any) -> Set[str]:
    """The template's enabled UI-catalog groups — the flavor scope used to
    lazy-load and gate intents. Config absent → ``{"core"}`` (same default
    as ``ui_catalog.resolve_allowlist``)."""
    configurations = getattr(template, "configurations", None)
    ui_cat = configurations.ui_catalog if configurations else None
    if ui_cat is None:
        return {"core"}
    return set(ui_cat.enabled_groups or [])


class IntentValidationError(ValueError):
    """Typed 422 payload for the widget (rendered as a Message primitive).
    ``detail`` is the HTTPException-ready body."""

    def __init__(
        self, code: str, message: str, errors: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        super().__init__(message)
        self.detail: Dict[str, Any] = {"code": code, "message": message}
        if errors:
            self.detail["errors"] = errors


class IntentApprovalRequired(RuntimeError):
    """A DIRECT policy tried to dispatch an approval-gated tool.

    Raised by :func:`run_persisted_tool` BEFORE dispatch and converted into
    the terminal ``intent_failed`` pair by :func:`run_direct_intent`. The
    no-LLM path has nowhere to put an approval card (there is no assistant
    turn to suspend), so a gated tool fails closed here and the user is
    pointed at the chat surface, which does have the HITL flow.
    """

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"tool {tool_name!r} requires human approval")
        self.tool_name = tool_name


@dataclass(frozen=True)
class ParsedIntent:
    """A wire intent validated against its policy-table payload schema."""

    intent: UiIntent
    policy: IntentPolicy
    payload: BaseModel


def _structural_errors(exc: ValidationError) -> List[Dict[str, Any]]:
    """Field paths + error kinds only — never input values (same privacy
    contract as ui_op_dropped telemetry)."""
    return [
        {
            "loc": ".".join(str(p) for p in err.get("loc", ())),
            "type": err.get("type"),
        }
        for err in exc.errors()[:5]
    ]


def parse_ui_intent(
    raw: Dict[str, Any], *, enabled_flavors: Optional[Set[str]] = None
) -> ParsedIntent:
    """Validate one raw ``ui_intent`` body against the wire model + the
    per-intent payload schema. Raises :class:`IntentValidationError` (422
    at the HTTP layer) for unknown intents, intents whose flavor the
    session's template doesn't enable, or invalid payloads.

    ``enabled_flavors`` is the template's flavor scope (see
    :func:`template_enabled_flavors`); callers must run
    :func:`ensure_flavor_intents` on it first so an enabled flavor's
    intents are actually registered. ``None`` skips the flavor gate
    (trusted/internal callers only).
    """
    try:
        wire = UiIntent.model_validate(raw)
    except ValidationError as exc:
        raise IntentValidationError(
            "invalid_intent", "Malformed ui_intent body", _structural_errors(exc)
        ) from exc

    policy = INTENT_POLICY.get(wire.intent)
    if policy is None:
        # Also the path taken when the intent's flavor was never loaded in
        # this process — an unregistered intent is indistinguishable from
        # an unknown one, and both are a typed 422.
        raise IntentValidationError("unknown_intent", f"Unknown intent {wire.intent!r}")
    if enabled_flavors is not None and policy.flavor not in enabled_flavors:
        raise IntentValidationError(
            "flavor_not_enabled",
            f"Intent {wire.intent!r} requires the {policy.flavor!r} flavor, "
            "which this template does not enable.",
        )

    try:
        payload = policy.payload_model.model_validate(wire.payload)
    except ValidationError as exc:
        raise IntentValidationError(
            "invalid_intent_payload",
            f"Invalid payload for intent {wire.intent!r}",
            _structural_errors(exc),
        ) from exc

    return ParsedIntent(intent=wire, policy=policy, payload=payload)


def agent_turn_content(parsed: ParsedIntent) -> str:
    """Rewrite an ``agent_turn``-routed intent into the structured user
    message the LLM answers (RFC-001 §3.3), via the policy's flavor-provided
    rewriter. Falling back to the visible display keeps a policy without a
    rewriter from crashing the rewrite."""
    if parsed.policy.agent_turn is not None:
        return parsed.policy.agent_turn(parsed)
    return parsed.intent.display or parsed.intent.intent


# ---------------------------------------------------------------------------
# Direct executor — engine shell + the seams flavor drivers build on
# ---------------------------------------------------------------------------


def error_events(code: str, message: str) -> List[SSEEvent]:
    """Terminal ``intent_failed`` + ``turn_end`` pair for a DIRECT intent
    that could not complete. Session stays ACTIVE — a failed intent must
    not brick the conversation. Part of the flavor-driver surface
    (drivers yield these on pre-dispatch failures).

    Deliberately NOT the ``error`` SSE event: the widget renders
    ``intent_failed`` as an in-thread notice with ``message`` (the intent
    itself was rejected, and retrying it unchanged fails identically),
    while ``error`` drives the delivery-failure banner with a Retry
    affordance. ``message`` must therefore be user-displayable.
    """
    return [
        SSEEvent(event="intent_failed", data={"code": code, "message": message}),
        SSEEvent(event="turn_end", data={"session_status": "ACTIVE"}),
    ]


async def _persist_tool_step(session_id: str, call: Any, result_payload: Any) -> None:
    """Persist one direct dispatch as the standard assistant(tool_use) +
    user(tool_result) row pair, so replayed history stays fully answered
    and the LLM sees the mutation on its next turn."""
    await insert_chat_message(
        session_id=session_id,
        role=ChatMessageRole.ASSISTANT,
        content=None,
        content_blocks=assistant_turn_to_blocks("", [call]),
    )
    await insert_chat_message(
        session_id=session_id,
        role=ChatMessageRole.USER,
        content=None,
        content_blocks=tool_results_to_user_blocks(
            [(call.tool_call_id, result_payload)]
        ),
    )


async def _merge_state_delta(
    session_id: str, baseline: Dict[str, Any], current: Dict[str, Any]
) -> None:
    """Persist the reducer-driven state delta (key-scoped merge, same
    contract as ``_cycle_loop``).

    Idempotent for unchanged keys and always diffed from the SAME baseline,
    so calling it again after a follow-up advances state further just writes
    a superset.
    """
    patch = diff_state_patch(baseline, current)
    if patch:
        await upsert_agent_session_state_merge(chat_session_id=session_id, patch=patch)


async def run_persisted_tool(
    agent: ChatAgent,
    *,
    tool_name: str,
    args: Dict[str, Any],
    node: Dict[str, Any],
    prep: Any,
    turn_id: str,
) -> Tuple[List[SSEEvent], Any]:
    """Dispatch ONE tool through the existing pipeline and persist it.

    The engine-owned building block flavor drivers compose their tool
    sequences from: ``ChatAgent.run_direct_tool`` (inject_tool_args →
    dispatch → binding store → reducers) + the standard row-pair
    persistence + the ``function_call_started`` / ``function_call_completed``
    SSE pair. Returns ``(events, result_payload)``.

    HITL is enforced HERE, not in each driver: this is the single choke
    point every DIRECT policy dispatches through, so a template that gates
    a tool behind human approval gates it on both surfaces. Without this a
    tool would be gated when the LLM calls it and free when a component
    button fires it — and chat has no second line of defence, since
    ``handles_approval_externally`` disables the handler-level gate that
    voice relies on.
    """
    if is_approval_gated(tool_name, agent._approval_map, node):
        raise IntentApprovalRequired(tool_name)
    call, result = await agent.run_direct_tool(
        tool_name=tool_name, args=args, node=node, prep=prep, turn_id=turn_id
    )
    await _persist_tool_step(agent.session_id, call, result)
    events = [
        SSEEvent(
            event="function_call_started",
            data={
                "name": tool_name,
                # The driver's args, NOT ``call.arguments`` — the latter is
                # post-injection (tool_arg_injection pulls identifiers and
                # generated values out of agent_session_state, which stays
                # server-internal). The LLM path emits pre-injection args
                # here too; both surfaces tell the client the same thing.
                "args": dict(args),
                "tool_call_id": call.tool_call_id,
            },
        ),
        SSEEvent(
            event="function_call_completed",
            data={
                "name": tool_name,
                "tool_call_id": call.tool_call_id,
                "result_summary": None,
            },
        ),
    ]
    return events, result


async def run_direct_intent(
    *, session_id: str, parsed: ParsedIntent
) -> AsyncIterator[SSEEvent]:
    """Serve one DIRECT-routed intent: the policy's flavor tools through
    the existing pipeline, hydrated component + ``turn_end`` over SSE —
    no LLM call.

    Mirrors ``run_chat_turn``'s load-everything-itself shape (terminal
    ``error`` + ``turn_end`` events instead of raising — the HTTP shell
    pre-validates, the SSE stream has no status code to change). The
    ``ChatAgent`` here is a dispatch chassis only: ``llm=None`` and no
    ``_cycle_loop`` entry, so no LLM client is ever constructed. The
    flavor content — which tools run and which component shows — comes
    entirely off ``parsed.policy`` (``drive`` / ``show_op``).
    """
    session = await get_chat_session_by_id(session_id)
    if session is None or session.status == ChatSessionStatus.ENDED:
        yield SSEEvent(
            event="error",
            data={"code": "session_gone", "message": "Session no longer active"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return
    template = await get_template_by_id_cached(session.template_id)
    if template is None:
        yield SSEEvent(
            event="error",
            data={"code": "template_missing", "message": "Template missing"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return

    # Persist the visible user bubble + the full intent as an internal
    # audit block: widget transcripts show only `display`; the LLM (and
    # the audit trail) keep the typed intent via the internal block.
    display = (
        parsed.intent.display or parsed.policy.default_display or parsed.intent.intent
    )
    audit = json.dumps(
        {"ui_intent": parsed.intent.model_dump(exclude_none=True)},
        ensure_ascii=False,
        default=str,
    )
    if parsed.policy.silent:
        # No visible trace: the audit block (which carries the full typed
        # intent) is the only record — internal visibility, so widget
        # transcripts and resume replay skip the row entirely while the
        # LLM still sees what the user did.
        await insert_chat_message(
            session_id=session_id,
            role=ChatMessageRole.USER,
            content=None,
            content_blocks=[internal_text_block(audit)],
        )
    else:
        user_msg = await insert_chat_message(
            session_id=session_id,
            role=ChatMessageRole.USER,
            content=display,
            content_blocks=[*plain_text_blocks(display), internal_text_block(audit)],
        )
        yield SSEEvent(
            event="user_committed",
            data={"idx": user_msg.idx if user_msg else None, "content": display},
        )

    state_row = await get_agent_session_state(session_id)
    agent_state: Dict[str, Any] = state_row.data if state_row else {}
    state_baseline = copy.deepcopy(agent_state)
    persisted_vars = (
        session.metadata.get("template_vars", {})
        if isinstance(session.metadata, dict)
        else {}
    )
    template_vars = await build_render_template_vars(template, persisted_vars)

    agent = ChatAgent(
        session_id=session_id,
        template=template,
        llm=None,  # direct path — no LLM client, ever
        template_vars=template_vars,
        agent_state=agent_state,
        catalog_version=resolve_session_catalog_version(session.metadata),
    )
    agent.aiohttp_session = create_aiohttp_session()
    agent.mcp_pool = {}
    turn_id = uuid.uuid4().hex
    try:
        drive = parsed.policy.drive
        if drive is None:
            # Defensive — the HTTP layer only routes DIRECT policies here,
            # and flavor registration provides ``drive`` for those.
            for ev in error_events(
                "invalid_intent", f"Intent {parsed.intent.intent!r} is not direct."
            ):
                yield ev
            return
        prep, node = await agent.prepare_direct_dispatch(session.current_node)

        final_tool: Optional[str] = None
        final_result: Any = None
        try:
            async for event, tool_name, result in drive(
                agent, prep, node, parsed, turn_id
            ):
                if event is not None:
                    yield event
                if tool_name is not None:
                    final_tool, final_result = tool_name, result
        except IntentApprovalRequired as exc:
            logger.warning(
                f"intent_router {session_id}: {parsed.intent.intent} needs "
                f"approval for {exc.tool_name} — refusing the direct path"
            )
            await _merge_state_delta(session_id, state_baseline, agent.agent_state)
            for ev in error_events(
                "intent_requires_approval",
                "This action needs to be confirmed first. Ask the assistant "
                "in the chat and approve it there.",
            ):
                yield ev
            return

        # Persist the reducer delta on EVERY outcome, before branching on
        # the result. Each dispatch's tool rows are already committed by
        # run_persisted_tool, so returning early without this would leave
        # agent_session_state behind what history says happened — a later
        # turn would then re-derive identifiers the reducers had already
        # captured. The LLM path persists unconditionally too (_cycle_loop).
        await _merge_state_delta(session_id, state_baseline, agent.agent_state)

        if final_tool is None:
            # The driver already emitted the terminal error events.
            return

        if not _is_tool_success(final_result):
            logger.warning(
                f"intent_router {session_id}: {parsed.intent.intent} via "
                f"{final_tool} returned an error envelope"
            )
            # Prefer the flavor's display-grade reason off the upstream
            # payload — a precise message beats "please try again" copy for
            # a failure that would repeat identically on retry.
            message: Optional[str] = None
            if parsed.policy.failure_message is not None:
                try:
                    message = parsed.policy.failure_message(final_result)
                except Exception:  # noqa: BLE001 — extractor is best-effort
                    message = None
            for ev in error_events(
                "intent_tool_failed",
                message or "The update could not be completed. Please try again.",
            ):
                yield ev
            return

        # Hydrate the policy's component exactly the way a `show` op would
        # — same resolver, same schema validation, same v:2 wire shape.
        # Events are collected, not yielded: the acknowledgement bubble
        # goes on the wire FIRST so the thread reads "Done." above the
        # component (matching LLM turns, whose prose streams before UI).
        hydrated_ops: List[Dict[str, Any]] = []
        ui_events: List[SSEEvent] = []
        if parsed.policy.show_op is not None:
            show_op = parsed.policy.show_op(final_tool, final_result, agent)
            resolved = resolve_show_op(show_op, agent.binding_store, agent.ui_allowlist)
            if resolved.op is not None:
                hydrated_ops.append(resolved.op)
                ui_events.append(ui_op_event(resolved.op))
            else:
                ui_events.append(
                    ui_op_dropped_event(
                        json.dumps(show_op, ensure_ascii=False),
                        resolved.error or "bind_unresolved",
                    )
                )

        # Flavor acknowledgement: one short assistant line ("Done.")
        # alongside the rendered component — persisted like any prose turn
        # so resume replays it.
        ack_text: Optional[str] = None
        if parsed.policy.ack_message is not None and not parsed.policy.silent:
            try:
                ack_text = parsed.policy.ack_message(parsed, final_tool, final_result)
            except Exception:  # noqa: BLE001 — ack is best-effort, never fatal
                ack_text = None

        assistant_idx: Optional[int] = None
        # RFC-002 Phase B: render_ui sessions never write the legacy
        # marker (replayed markers bred the F1 mimicry bug); the persisted
        # tool exchange already carries the underlying data. Fleet
        # text-channel sessions keep it.
        ui_summary = (
            ""
            if getattr(agent, "_render_ui_enabled", False)
            else summarize_ui_ops(hydrated_ops)
        )
        if hydrated_ops or ack_text:
            blocks: List[Dict[str, Any]] = (
                [*plain_text_blocks(ack_text)] if ack_text else []
            )
            if ui_summary:
                blocks.append(internal_text_block(ui_summary))
            stored = await insert_chat_message(
                session_id=session_id,
                role=ChatMessageRole.ASSISTANT,
                content=ack_text,
                content_blocks=blocks or None,
                # Silent intents: the component streamed to the live
                # overlay only — never a replayable thread block.
                ui_blocks=None if parsed.policy.silent else (hydrated_ops or None),
            )
            assistant_idx = stored.idx if stored else None
            if ack_text:
                yield SSEEvent(
                    event="assistant_message",
                    data={"idx": assistant_idx, "content": ack_text},
                )
        for ev in ui_events:
            yield ev

        # Post-success follow-up: runs with the ack + primary component
        # ALREADY delivered, so its latency (which may include its own LLM
        # call and tool dispatches) is invisible — the turn simply stays
        # open a moment longer. Fail-open: any error → no block, turn ends
        # normally.
        if parsed.policy.followup is not None and not parsed.policy.silent:
            followup_op: Optional[Dict[str, Any]] = None
            try:
                followup_op = await parsed.policy.followup(
                    agent, prep, node, parsed, turn_id, final_tool, final_result
                )
            except Exception:  # noqa: BLE001 — decoration, never fatal
                logger.exception(
                    f"intent_router {session_id}: followup for "
                    f"{parsed.intent.intent} failed"
                )
            if followup_op is not None:
                # The follow-up's own tool dispatches may have advanced
                # reducer state past the delta persisted above — merge the
                # full diff again (idempotent for unchanged keys).
                await _merge_state_delta(session_id, state_baseline, agent.agent_state)
                await insert_chat_message(
                    session_id=session_id,
                    role=ChatMessageRole.ASSISTANT,
                    content=None,
                    content_blocks=None,
                    ui_blocks=[followup_op],
                )
                yield ui_op_event(followup_op)

        await update_chat_session_after_turn(
            session_id=session_id, current_node=session.current_node
        )
        yield SSEEvent(
            event="turn_end",
            data={"session_status": "ACTIVE", "assistant_idx": assistant_idx},
        )
    finally:
        if agent.aiohttp_session is not None:
            await agent.aiohttp_session.close()
            agent.aiohttp_session = None
        await close_mcp_pool(agent.mcp_pool)
        agent.mcp_pool = None


__all__ = [
    "UiIntent",
    "IntentRoute",
    "IntentPolicy",
    "INTENT_POLICY",
    "FLAVOR_INTENT_MODULES",
    "register_intents",
    "ensure_flavor_intents",
    "template_enabled_flavors",
    "IntentValidationError",
    "IntentApprovalRequired",
    "ParsedIntent",
    "parse_ui_intent",
    "agent_turn_content",
    "error_events",
    "run_persisted_tool",
    "run_direct_intent",
    "DirectDriveFn",
    "ShowOpBuilderFn",
    "FollowupFn",
]
