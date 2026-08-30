"""``render_ui`` — UI authoring as a real function call (RFC-002 Phase A).

The model's most important structured decision — "show this UI" — moves off
the in-band ``<ui_stream>`` text protocol onto the provider-validated
function-calling channel. The tool's args are the ``show``-op shape
(component + bind refs + selection); the handler is a thin wrapper over the
existing hydration machinery (:func:`resolve_show_op` / ``BindingStore``) so
every RFC-001 trust invariant survives verbatim: values come ONLY from THIS
turn's validated tool results, the model authors selectors, never values.

Why this is structurally more reliable than the text channel (RFC-002 §3.3):
the history replays native ``function_call``/``function_response`` pairs, so
the model sees its past self calling ``render_ui`` — mimicry now works FOR
us; hydration errors return in the function response, so the model can
correct within the same turn; and "didn't render" becomes an observable
(and forceable) event instead of a silent omission.

``{"decision": "no_ui", "reason": …}`` is a legal payload — the forced
think-step ("force to THINK, not to always SHOW") requires the model to
deliberate about UI after a successful search, while display stays its
judgment. An explicit no-render is distinguishable from a silent one.

This module is pure logic — no agent state. ``ChatAgent`` owns op-id
assignment (first rendered op of a turn anchors the widget tree as
``root``; later ops parent under it) and SSE emission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    strip_client_context_keys,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    BindingStore,
    parse_bind_ref,
    resolve_show_op,
    selector_extension_keys,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    UI_CATALOG,
    is_data_bound,
    validate_props,
)

RENDER_UI_TOOL_NAME = "render_ui"

# Chips carrying raw identifiers are always a model mistake (ids belong
# in actions' msg, never on a user-facing pill). Shared shape with the
# rider-harvest guard in agent/runtime.py.
_IDENTIFIER_CHIP_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\b[0-9a-f]{16,}\b",
    re.IGNORECASE,
)
REVISE_PLAN_TOOL_NAME = "revise_plan"


@dataclass(frozen=True)
class RenderUiFlavorPack:
    """Flavor-owned pieces of the render_ui surface.

    The engine owns the MECHANISM (schema assembly, arg parsing, bind
    validation, hydration, error envelopes); everything a flavor knows —
    product/cart vocabulary in the LLM-facing arg descriptions, which prop
    shapes a summary reads, and post-hydration projection policy (layout,
    checkout stamping) — registers here under the flavor's catalog group,
    same lifecycle as ``ui_prompt.register_render_ui_flavor_section``
    (the flavor's schemas module registers at lazy import, which
    ``resolve_allowlist`` triggers in agent ``__init__``).

    Every text field is optional — unset falls back to the engine's
    flavor-neutral copy. ``summarize(component, props)`` returns the
    function-response summary or ``None`` to defer to the generic one.
    ``finalize_hydrated(component, schema_cls, hydrated_props, *, bind,
    store, template, state_values)`` mutates ``hydrated_props`` in place
    (server-policy stamps like layout / checkout button)."""

    tool_description: Optional[str] = None
    bind_description: Optional[str] = None
    # Per-component sentences APPENDED to bind_description, each only when
    # its component is actually offered this session (dict order = prose
    # order). A disabled component must leave no trace in the schema — a
    # template that opts out of OrderStatus must not have the model coached
    # to bind it (dangling coaching live-reads as an instruction to try).
    bind_component_coaching: Optional[Dict[str, str]] = None
    items_description: Optional[str] = None
    quick_replies_description: Optional[str] = None
    quick_replies_rider_description: Optional[str] = None
    link_description: Optional[str] = None
    literal_fields_description: Optional[str] = None
    bind_example: Optional[str] = None
    link_untrusted_fallback_hint: Optional[str] = None
    # Tools whose success forces the render_ui think-step when the
    # template leaves ``render_ui.force_after`` unset (commerce:
    # ["search_catalog"]). The engine itself names no tools.
    default_force_after: Optional[List[str]] = None
    summarize: Optional[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = (
        None
    )
    finalize_hydrated: Optional[Callable[..., None]] = None
    # Literal-fields trust gate (components with ``literal_fields`` on
    # their schema, e.g. OrderStatus's transcribed ETA): called as
    # ``(component, schema_cls, literal_args, store=…, template=…,
    # state_values=…) -> (accepted_props, dropped_reasons)``. Accepted
    # values merge into the hydrated props; dropped names+reasons ride the
    # function response. No hook registered → every literal field drops
    # (fail closed: unverified model values must never render).
    verify_literal_fields: Optional[
        Callable[..., "tuple[Dict[str, Any], Dict[str, str]]"]
    ] = None
    # Repeat-render policy: (component, prev_props, new_props) → None (no
    # merge — surfaces stack as authored) or (merged_props, llm_note); the
    # agent swaps the wire op for a `replace` on the first node and the
    # note rides the function response.
    merge_repeat_render: Optional[
        Callable[
            [str, Dict[str, Any], Dict[str, Any]],
            Optional[tuple],
        ]
    ] = None


_RENDER_UI_FLAVOR_PACKS: Dict[str, RenderUiFlavorPack] = {}


def register_render_ui_flavor_pack(group: str, pack: RenderUiFlavorPack) -> None:
    """Register a flavor's render_ui pack under its catalog group name.

    Process-global, idempotent on re-import (same-key overwrite)."""
    _RENDER_UI_FLAVOR_PACKS[group] = pack


def resolve_render_ui_flavor_pack(
    flavor_groups: Optional[List[str]],
) -> Optional[RenderUiFlavorPack]:
    """First enabled group with a registered pack wins (mirrors the
    ui_prompt flavor-section resolution)."""
    for group in flavor_groups or []:
        pack = _RENDER_UI_FLAVOR_PACKS.get(group)
        if pack is not None:
            return pack
    return None


# Internal alias — module-local call sites predate the public name.
_resolve_pack = resolve_render_ui_flavor_pack


@dataclass
class RenderUiOutcome:
    """One ``render_ui`` execution: the function response the model sees +
    the hydrated op(s) the widget renders. ``decision`` is ``"rendered"`` |
    ``"no_ui"`` | ``"error"`` (drives the ``ui_decision`` telemetry event)."""

    fn_result: Dict[str, Any]
    ops: List[Dict[str, Any]] = field(default_factory=list)
    decision: str = "error"
    component: Optional[str] = None
    reason: Optional[str] = None


def render_ui_components(
    ui_allowlist: Set[str],
    catalog_v2: bool,
    custom_components: Optional[Set[str]] = None,
) -> List[str]:
    """The component names ``render_ui`` offers on this session: data-bound,
    allowlisted, not server-only — plus the literal-content components the
    engine keeps model-authored (QuickReplies; LinkButton, whose url is
    server-verified against trusted/tool-sourced links).

    ``custom_components`` are this session's registry defs (CHAMELEON
    overlay) — session data, never catalog entries, so they join the enum
    here explicitly. v2-only, same as every data-bound component."""
    names: List[str] = []
    if catalog_v2:
        for name in sorted(ui_allowlist):
            if custom_components and name in custom_components:
                names.append(name)
                continue
            schema = UI_CATALOG.get(name)
            if schema is None or not is_data_bound(name):
                continue
            if getattr(schema, "server_only", False):
                continue
            names.append(name)
    for literal in ("QuickReplies", "LinkButton"):
        if literal in ui_allowlist and literal not in names:
            names.append(literal)
    return names


_TOOL_DESC_GENERIC = (
    "Render UI for the user THIS turn. Call it once. Values are filled "
    "from this turn's tool results — you author selectors only. "
    "decision='no_ui' renders nothing (empty results or a purely "
    "conversational reply)."
)
_BIND_DESC_GENERIC = (
    "Data bindings into THIS turn's tool results, e.g. "
    "[{'prop':'<prop>','ref':'$tool:<tool_name>#/<json_pointer>'}]."
)
_ITEMS_DESC_GENERIC = (
    "Optional selection for data-bound list components: which bound "
    "entries to show, in this order (ids from THIS turn's results); "
    "omit to show everything."
)
_QUICK_DESC_GENERIC = (
    "QuickReplies content (only with component='QuickReplies'): 2-5 "
    "short strings — each is exactly what the user sees on the "
    "pill AND what comes back as their next message when tapped."
)
_QUICK_RIDER_DESC_GENERIC = (
    "Optional, attachable to ANY call: 2-5 short follow-up "
    "strings shown as tappable pills UNDER your final reply — "
    "each is exactly what the user sees AND what comes back "
    "as their next message when tapped. Attach them to the "
    "render_ui call that accompanies your reply; placement is "
    "automatic. Labels <=4 words; never duplicate an action "
    "already available on UI rendered this turn."
)
_LINK_DESC_GENERIC = (
    "LinkButton content (only with component='LinkButton'): a single "
    "link CTA for link-only answers. url must be one of the trusted "
    "URLs below or a URL from THIS turn's tool results — anything "
    "else is rejected."
)
_BIND_EXAMPLE_GENERIC = "[{'prop':'<prop>','ref':'$tool:<tool_name>#/<json_pointer>'}]"
_LINK_FALLBACK_HINT_GENERIC = "pass one of the template's trusted URLs"
_FIELDS_DESC_GENERIC = (
    "Literal display fields for components that declare them: "
    "[{'name':'<field>','value':'<short string>'} or "
    "{'name':'<field>','values':['<string>', …]}]. Values must be "
    "TRANSCRIBED near-verbatim from THIS turn's tool results — the "
    "server verifies each one and silently drops anything it cannot "
    "ground (the function response names the drops)."
)


def build_render_ui_schema(
    components: List[str],
    handler: Any,
    trusted_urls: Optional[List[str]] = None,
    quick_replies_mode: Optional[str] = None,
    flavor_groups: Optional[List[str]] = None,
    custom_coaching: Optional[Dict[str, str]] = None,
) -> FlowsFunctionSchema:
    """The ``render_ui`` function schema (Vertex-safe subset: no
    additionalProperties — ``bind`` is an array of {prop, ref} pairs).

    Arg descriptions are flavor-composed: the enabled flavor's registered
    pack (see :class:`RenderUiFlavorPack`) supplies its vocabulary
    (products/carts/checkout coaching for commerce); unset fields fall
    back to the flavor-neutral copy above.

    ``trusted_urls`` (template ``render_ui.trusted_link_urls``)
    self-documents in the ``link`` arg description — without it the model
    doesn't KNOW the configured trusted URL and guesses (live-observed:
    two rejected guesses, then a search just to harvest a usable URL)."""
    pack = _resolve_pack(flavor_groups)
    link_desc = (pack.link_description if pack else None) or _LINK_DESC_GENERIC
    if trusted_urls:
        link_desc += " Trusted URLs: " + ", ".join(sorted(trusted_urls))
    quick_desc = (
        pack.quick_replies_description if pack else None
    ) or _QUICK_DESC_GENERIC
    if quick_replies_mode == "forced_final":
        # Rider semantics (2026-08-03): chips are an ANNOTATION the model
        # may attach to any render_ui call — the server owns placement
        # (always below the final reply). QuickReplies leaves the
        # component enum entirely in this mode; the old mid-turn ban is
        # replaced by harvest-and-defer in the agent handler. The model's
        # natural grid+chips-in-one-call instinct (live-observed as a
        # parallel-call crash 2026-07-31) becomes the FAST path: no
        # forced end-of-turn cycle when a rider was attached.
        components = [c for c in components if c != "QuickReplies"]
        quick_desc = (
            pack.quick_replies_rider_description if pack else None
        ) or _QUICK_RIDER_DESC_GENERIC
    # Bind coaching is composed per OFFERED component (post-filter enum):
    # the base description plus each offered component's sentence, in the
    # pack's stated order. A component absent from the enum
    # (disabled_primitives, lazy group not enabled) contributes nothing —
    # the model never reads coaching for UI it cannot render.
    bind_desc = (pack.bind_description if pack else None) or _BIND_DESC_GENERIC
    for comp_name, sentence in (
        (pack.bind_component_coaching if pack else None) or {}
    ).items():
        if comp_name in components:
            bind_desc += sentence
    # Custom registry defs coach the same way (prompt_hint per offered
    # component) — a def a template didn't opt into leaves no trace.
    for comp_name, sentence in (custom_coaching or {}).items():
        if comp_name in components and sentence:
            hint = sentence.strip()
            bind_desc += " " + hint if hint.endswith(".") else " " + hint + "."
    # `fields` is advertised only when an offered component actually
    # declares literal fields — every other session keeps today's schema
    # byte-identical (no arg for the model to misuse).
    literal_field_names: List[str] = []
    for comp_name in components:
        comp_schema = UI_CATALOG.get(comp_name)
        for fname in getattr(comp_schema, "literal_fields", ()) or ():
            if fname not in literal_field_names:
                literal_field_names.append(fname)
    fields_property: Dict[str, Any] = {}
    if literal_field_names:
        fields_desc = (
            pack.literal_fields_description if pack else None
        ) or _FIELDS_DESC_GENERIC
        fields_property = {
            "fields": {
                "type": "array",
                "description": fields_desc,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": literal_field_names},
                        "value": {"type": "string"},
                        "values": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name"],
                },
            }
        }
    return FlowsFunctionSchema(
        name=RENDER_UI_TOOL_NAME,
        # This description is LLM-facing (rides the tools schema on EVERY
        # cycle) — contract only, never server internals. Litmus test for a
        # sentence here: does it change what the model should DO? Behavioral
        # style lives in the system prompt; recovery behavior (e.g. the
        # same-turn grid merge) is explained just-in-time by the function
        # RESPONSE when it actually happens.
        description=(pack.tool_description if pack else None) or _TOOL_DESC_GENERIC,
        properties={
            **fields_property,
            "component": {
                "type": "string",
                "enum": components,
                "description": "Which component to render.",
            },
            "bind": {
                "type": "array",
                "description": bind_desc,
                "items": {
                    "type": "object",
                    "properties": {
                        "prop": {"type": "string"},
                        "ref": {"type": "string"},
                    },
                    "required": ["prop", "ref"],
                },
            },
            "items": {
                "type": "array",
                "description": (pack.items_description if pack else None)
                or _ITEMS_DESC_GENERIC,
                "items": {
                    "type": "object",
                    # Extra selector keys come from the transforms
                    # registered by THIS session's flavor groups (commerce:
                    # feature_variant) — the schema advertises exactly what
                    # the selection engine will honor for this template,
                    # and nothing another template's flavor registered.
                    "properties": {
                        "id": {"type": "string"},
                        **{
                            key: {"type": "string"}
                            for key in selector_extension_keys(flavor_groups)
                        },
                    },
                    "required": ["id"],
                },
            },
            "max_items": {"type": "integer"},
            "quick_replies": {
                "type": "array",
                "description": quick_desc,
                "items": {"type": "string"},
            },
            "link": {
                "type": "object",
                "description": link_desc,
                "properties": {
                    "label": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
            "decision": {
                "type": "string",
                "enum": ["no_ui"],
                "description": (
                    "Pass 'no_ui' (with reason) to explicitly render "
                    "nothing this turn."
                ),
            },
            "reason": {"type": "string"},
        },
        required=[],
        handler=handler,
    )


def build_revise_plan_schema(handler: Any) -> FlowsFunctionSchema:
    """``revise_plan`` — the ONLY path off a declared plan (plan
    enforcement, RFC-002 Decision 4). Calling it is an explicit, observable
    event: the step rail updates honestly instead of silently drifting."""
    return FlowsFunctionSchema(
        name=REVISE_PLAN_TOOL_NAME,
        description=(
            "Replace the REMAINING steps of your declared plan. Call this "
            "when the plan no longer fits (a step failed, results changed "
            "the approach, or fewer/more steps are needed). steps = tool "
            "names in execution order; may be empty to finish early."
        ),
        properties={
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Remaining tool steps, in order.",
            },
            "reason": {"type": "string"},
        },
        required=["steps", "reason"],
        handler=handler,
    )


# Depth cap for the LinkButton URL scan — tool payloads are shallow
# (UCP products nest ~4 levels); the cap just bounds pathological inputs.
_URL_SCAN_DEPTH = 8


def _payload_contains_url(payload: Any, url: str, depth: int = _URL_SCAN_DEPTH) -> bool:
    if depth < 0:
        return False
    if isinstance(payload, str):
        return payload == url
    if isinstance(payload, dict):
        return any(_payload_contains_url(v, url, depth - 1) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_payload_contains_url(v, url, depth - 1) for v in payload)
    return False


def _tool_sourced_state(
    state_values: Optional[Dict[str, Any]], template: Any
) -> Dict[str, Any]:
    """``state_values`` minus everything the STOREFRONT can write.

    The state scan below treats a URL as trusted because
    ``agent_session_state`` is reducer-built over tool payloads. That is
    true of most of it, but two parts are shopper-controlled: the
    ``_client_context`` facts namespace, and any top-level key the
    template listed in ``client_context.state_allowlist`` (the push
    validates key NAMES, never values). Left in, a compromised storefront
    page could write an arbitrary URL into state and have the assistant
    render it as a trusted CTA — defeating ``trusted_link_urls``.

    ⚠️ Removal is by KEY, and a state key is a single slot: if a template
    ever allowlists a key that its reducers ALSO write real tool URLs
    into, the genuine value is dropped along with the untrusted one and
    the link is refused. No template does that today (none configures
    ``client_context`` at all). The provenance-tracking fix — reducers
    writing into a trusted sub-namespace — is the correct answer if that
    day comes; this is the cheap one that closes the hole.
    """
    if not state_values:
        return {}
    scannable = strip_client_context_keys(state_values)
    configurations = getattr(template, "configurations", None)
    client_context = getattr(configurations, "client_context", None)
    allowlist = getattr(client_context, "state_allowlist", None) or ()
    if not allowlist:
        return scannable
    return {k: v for k, v in scannable.items() if k not in set(allowlist)}


def url_is_trusted(
    url: str,
    store: BindingStore,
    trusted_urls: Optional[Set[str]],
    state_values: Optional[Dict[str, Any]] = None,
    template: Any = None,
) -> bool:
    """LinkButton trust rule: exact match against the template's
    ``trusted_link_urls`` allowlist, verbatim presence in THIS turn's
    tool results, OR verbatim presence in the TOOL-SOURCED part of
    reducer state — the model selects links, it never authors them.

    State qualifies only after :func:`_tool_sourced_state` removes the
    storefront-writable keys: a reducer-captured URL (e.g. ``policy_links``
    from an earlier cart call) has real tool provenance, a browser-pushed
    one has none."""
    if trusted_urls and url in trusted_urls:
        return True
    scannable = _tool_sourced_state(state_values, template)
    if scannable and _payload_contains_url(scannable, url):
        return True
    return any(_payload_contains_url(payload, url) for payload in store.payloads())


def summarize_render(
    component: str,
    hydrated_op: Dict[str, Any],
    flavor_groups: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The function response for a successful render — the model's UI
    memory: what the user is looking at, compact and structured (~50-80
    tokens, never a payload echo).

    The enabled flavor's ``pack.summarize`` runs first — it knows its own
    prop shapes (commerce: products/line_items + id/title/variant
    referents); ``None`` defers to the generic fallback here, which
    handles the engine's literal components (QuickReplies, LinkButton)
    and counts any list-valued prop."""
    props = hydrated_op.get("props") or {}
    pack = _resolve_pack(flavor_groups)
    if pack is not None and pack.summarize is not None:
        flavored = pack.summarize(component, props)
        if flavored is not None:
            return flavored
    result: Dict[str, Any] = {"status": "ok", "rendered": component}
    if isinstance(props.get("items"), list):  # QuickReplies
        result["count"] = len(props["items"])
    elif isinstance(props.get("url"), str):  # LinkButton
        result["label"] = props.get("label")
        result["url"] = props["url"]
    else:
        for value in props.values():
            if isinstance(value, list):
                result["count"] = len(value)
                break
    return result


def execute_render_ui(
    args: Dict[str, Any],
    *,
    store: BindingStore,
    allowlist: Set[str],
    components: List[str],
    op_id: str,
    parent: Optional[str] = None,
    trusted_urls: Optional[Set[str]] = None,
    restrict_to: Optional[Set[str]] = None,
    template: Any = None,
    state_values: Optional[Dict[str, Any]] = None,
    flavor_groups: Optional[List[str]] = None,
    custom_defs: Optional[Dict[str, Any]] = None,
) -> RenderUiOutcome:
    """Run one ``render_ui`` call: validate → hydrate → finalize → summarize.

    Every failure returns a structured error in ``fn_result`` (never
    raises) — the model reads it in the function response and corrects its
    next call. That closes RFC-002 F1's silent-drop hole: an invalid render
    is no longer invisible to the model.

    ``restrict_to`` narrows THIS call's renderable components (the forced
    final quick-replies cycle passes ``{"QuickReplies"}`` so the turn's
    tail can never sprout a second grid); ``decision='no_ui'`` stays legal.

    Post-hydration projection policy is the flavor's: the enabled group's
    ``pack.finalize_hydrated`` (commerce: layout-by-count, CartView
    checkout stamping off the template's ``ui_intents`` roles +
    ``template``/``state_values`` here) mutates the hydrated props before
    the summary. No pack → no stamps — a generic session renders exactly
    what it bound. The model never authors any of it.
    """
    if args.get("decision") == "no_ui":
        reason = str(args.get("reason") or "").strip() or "model chose no UI"
        return RenderUiOutcome(
            fn_result={"status": "ok", "decision": "no_ui", "reason": reason},
            decision="no_ui",
            reason=reason,
        )

    component = args.get("component")
    if not isinstance(component, str) or not component:
        return RenderUiOutcome(
            fn_result={
                "status": "error",
                "error": (
                    "render_ui needs either component=<one of "
                    f"{components}> or decision='no_ui' with a reason"
                ),
            }
        )
    if component not in components:
        return RenderUiOutcome(
            fn_result={
                "status": "error",
                "error": f"unknown component {component!r}; pick one of {components}",
            },
            component=component,
        )
    if restrict_to is not None and component not in restrict_to:
        return RenderUiOutcome(
            fn_result={
                "status": "error",
                "error": (
                    "this end-of-turn call may only render "
                    f"{sorted(restrict_to)} (short follow-up suggestions) "
                    "or pass decision='no_ui' — nothing else"
                ),
            },
            component=component,
        )

    props: Dict[str, Any] = {}
    if isinstance(args.get("max_items"), int):
        props["max_items"] = args["max_items"]
    # ``layout`` is deliberately NOT read from args (2026-07-30): layout is
    # server policy, never a model choice — the flavor pack's
    # ``finalize_hydrated`` stamps it after hydration. A model-passed
    # value is ignored.
    items = args.get("items")
    if isinstance(items, list) and items:
        selectors: List[Dict[str, Any]] = []
        extension_keys = selector_extension_keys(flavor_groups)
        for entry in items:
            if isinstance(entry, str) and entry:
                selectors.append({"id": entry})
            elif (
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"]
            ):
                sel: Dict[str, Any] = {"id": entry["id"]}
                for key in extension_keys:
                    value = entry.get(key)
                    if isinstance(value, str) and value:
                        sel[key] = value
                selectors.append(sel)
        if selectors:
            props["items"] = selectors
    # Server-policy props (e.g. commerce's CartView ``checkout`` button)
    # are deliberately NOT read from args (2026-07-30): the flavor pack's
    # ``finalize_hydrated`` stamps them after hydration from tool/state
    # provenance — never the model.
    quick = args.get("quick_replies")
    if isinstance(quick, list) and quick:
        # One STRING per chip (2026-07-31 simplification): the same text
        # is shown on the pill and sent back on tap — the model authors a
        # single value, nothing to keep consistent. The widget wire keeps
        # {label} (its click already falls back value→label). The old
        # {label, value} object form is tolerated, not advertised.
        chip_items: List[Dict[str, str]] = []
        for entry in quick:
            if isinstance(entry, str) and entry.strip():
                chip_items.append({"label": entry.strip()})
            elif isinstance(entry, dict) and entry.get("label"):
                chip_items.append(
                    {k: v for k, v in entry.items() if k in ("label", "value") and v}
                )
        # Chips are user-facing copy: drop any carrying a raw identifier
        # (UUID / long hex) — same guard as the rider-harvest path.
        chip_items = [
            item
            for item in chip_items
            if not _IDENTIFIER_CHIP_RE.search(item.get("label", ""))
        ]
        props["items"] = chip_items
    if component == "LinkButton":
        link = args.get("link")
        if not (isinstance(link, dict) and isinstance(link.get("url"), str)):
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        "LinkButton needs link={'label': …, 'url': …} — the "
                        "url must be a trusted URL or a URL from THIS "
                        "turn's tool results"
                    ),
                },
                component=component,
            )
        url = link["url"]
        if not url_is_trusted(url, store, trusted_urls, state_values, template):
            pack = _resolve_pack(flavor_groups)
            hint = (
                "use one of: " + ", ".join(sorted(trusted_urls))
                if trusted_urls
                else (pack.link_untrusted_fallback_hint if pack else None)
                or _LINK_FALLBACK_HINT_GENERIC
            )
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        f"untrusted url for LinkButton — {hint}, or a URL "
                        "that appears in THIS turn's tool results, verbatim"
                    ),
                },
                component=component,
            )
        props["url"] = url
        props["label"] = str(link.get("label") or "Open link")[:40]

    bind_raw = args.get("bind")
    bind: Dict[str, str] = {}
    if isinstance(bind_raw, list):
        for pair in bind_raw:
            if (
                isinstance(pair, dict)
                and isinstance(pair.get("prop"), str)
                and isinstance(pair.get("ref"), str)
            ):
                bind[pair["prop"]] = pair["ref"]
    elif isinstance(bind_raw, dict):  # tolerate object form
        bind = {
            k: v
            for k, v in bind_raw.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    if custom_defs and component in custom_defs:
        # CHAMELEON: session-scoped registry component. Same trust path
        # (this turn's BindingStore only), hydrated + validated against
        # the def's JSON Schema instead of a catalog Pydantic class. No
        # flavor finalize, no literal fields (v1 rejects them at write).
        from app.ai.voice.agents.breeze_buddy.chat.ui.custom_defs import (
            resolve_custom_show_op,
            summarize_custom_render,
        )

        def_ = custom_defs[component]
        if not bind:
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        f"{component} is data-bound: pass bind, e.g. "
                        f"{_BIND_EXAMPLE_GENERIC}"
                    ),
                },
                component=component,
            )
        show_op: Dict[str, Any] = {
            "op": "show",
            "id": op_id,
            "component": component,
            "bind": bind,
            "props": props,
        }
        if parent:
            show_op["parent"] = parent
        custom_result = resolve_custom_show_op(show_op, store, def_)
        if custom_result.op is None:
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        f"render failed: {custom_result.error}. Bind refs "
                        "must point into a tool result from THIS turn."
                    ),
                },
                component=component,
                reason=custom_result.error,
            )
        return RenderUiOutcome(
            fn_result=summarize_custom_render(
                def_, custom_result.op.get("props") or {}
            ),
            ops=[custom_result.op],
            decision="rendered",
            component=component,
        )

    if is_data_bound(component):
        if not bind:
            pack = _resolve_pack(flavor_groups)
            example = (pack.bind_example if pack else None) or _BIND_EXAMPLE_GENERIC
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (f"{component} is data-bound: pass bind, e.g. {example}"),
                },
                component=component,
            )
        # Literal fields (schema-declared, e.g. OrderStatus's transcribed
        # ETA): collect from the `fields` array (the advertised form) or
        # tolerated top-level args, run the flavor's trust gate, and merge
        # ONLY the verified survivors into props. No registered verifier →
        # everything drops (fail closed — an unverified model value must
        # never render). Dropped names+reasons ride the function response
        # so the model corrects in-turn instead of silently losing UI.
        schema_cls = UI_CATALOG.get(component)
        declared = tuple(getattr(schema_cls, "literal_fields", ()) or ())
        dropped_fields: Dict[str, str] = {}
        if declared:
            literal_args: Dict[str, Any] = {}
            fields_arg = args.get("fields")
            if isinstance(fields_arg, list):
                for entry in fields_arg:
                    if not (isinstance(entry, dict) and entry.get("name") in declared):
                        continue
                    if isinstance(entry.get("values"), list):
                        literal_args[entry["name"]] = entry["values"]
                    elif entry.get("value") is not None:
                        literal_args[entry["name"]] = entry["value"]
            for name in declared:  # tolerate the natural top-level form
                if name in args and name not in literal_args:
                    literal_args[name] = args[name]
            if literal_args:
                pack = _resolve_pack(flavor_groups)
                verify = pack.verify_literal_fields if pack else None
                if verify is None:
                    dropped_fields = {k: "no_verifier" for k in literal_args}
                else:
                    accepted, dropped_fields = verify(
                        component,
                        schema_cls,
                        literal_args,
                        store=store,
                        template=template,
                        state_values=state_values,
                    )
                    props.update(accepted)
        for prop, ref in bind.items():
            if parse_bind_ref(ref) is None:
                return RenderUiOutcome(
                    fn_result={
                        "status": "error",
                        "error": (
                            f"bad bind ref for {prop!r}: expected "
                            "$tool:<tool_name>#/<json-pointer>"
                        ),
                    },
                    component=component,
                )
        op: Dict[str, Any] = {
            "op": "show",
            "id": op_id,
            "component": component,
            "bind": bind,
            "props": props,
        }
        if parent:
            op["parent"] = parent
        result = resolve_show_op(op, store, allowlist, flavor_groups)
        if result.op is None:
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        f"render failed: {result.error}. Bind refs must "
                        "point into a tool result from THIS turn."
                    ),
                },
                component=component,
                reason=result.error,
            )
        hydrated_props = result.op.get("props")
        pack = _resolve_pack(flavor_groups)
        if (
            pack is not None
            and pack.finalize_hydrated is not None
            and isinstance(hydrated_props, dict)
        ):
            # Flavor projection policy (server-authored, never the model):
            # commerce stamps layout-by-count and the CartView checkout
            # button here. Mutates the props in place before the summary.
            pack.finalize_hydrated(
                component,
                UI_CATALOG.get(component),
                hydrated_props,
                bind=bind,
                store=store,
                template=template,
                state_values=state_values,
            )
        fn_result = summarize_render(component, result.op, flavor_groups)
        if dropped_fields:
            # The model's in-turn correction signal: which transcriptions
            # failed the trust gate, and why. Names + short reasons only.
            fn_result["dropped_fields"] = dropped_fields
        return RenderUiOutcome(
            fn_result=fn_result,
            ops=[result.op],
            decision="rendered",
            component=component,
        )

    # Literal component (QuickReplies): validate props, no hydration.
    try:
        validated = validate_props(component, props)
    except Exception as exc:  # ValidationError or unknown type
        detail = str(exc).split("\n", 1)[0][:200]
        return RenderUiOutcome(
            fn_result={
                "status": "error",
                "error": f"invalid {component} props: {detail}",
            },
            component=component,
        )
    hydrated_op: Dict[str, Any] = {
        "op": "add",
        "id": op_id,
        "type": component,
        "props": validated.model_dump(exclude_none=True, mode="json"),
        "v": 2,
    }
    if parent:
        hydrated_op["parent"] = parent
    return RenderUiOutcome(
        fn_result=summarize_render(component, hydrated_op),
        ops=[hydrated_op],
        decision="rendered",
        component=component,
    )


__all__ = [
    "RENDER_UI_TOOL_NAME",
    "REVISE_PLAN_TOOL_NAME",
    "RenderUiFlavorPack",
    "RenderUiOutcome",
    "build_render_ui_schema",
    "build_revise_plan_schema",
    "execute_render_ui",
    "register_render_ui_flavor_pack",
    "render_ui_components",
    "resolve_render_ui_flavor_pack",
    "summarize_render",
    "url_is_trusted",
]
