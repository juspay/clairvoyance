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
from typing import Any, Dict, List, Optional, Set

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    BindingStore,
    parse_bind_ref,
    resolve_show_op,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    UI_CATALOG,
    is_data_bound,
    validate_props,
)

RENDER_UI_TOOL_NAME = "render_ui"
REVISE_PLAN_TOOL_NAME = "revise_plan"

# Summary caps — the function response is the model's UI memory (the
# structured replacement for the old ``[ui rendered: …]`` marker); it must
# stay ~50-80 tokens, never a payload echo.
_SUMMARY_ITEMS_CAP = 8
_SUMMARY_TITLE_CAP = 60


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


def render_ui_components(ui_allowlist: Set[str], catalog_v2: bool) -> List[str]:
    """The component names ``render_ui`` offers on this session: data-bound,
    allowlisted, not server-only — plus the literal-content components the
    commerce flavor keeps model-authored (QuickReplies; LinkButton, whose
    url is server-verified against trusted/tool-sourced links)."""
    names: List[str] = []
    if catalog_v2:
        for name in sorted(ui_allowlist):
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


def build_render_ui_schema(
    components: List[str],
    handler: Any,
    trusted_urls: Optional[List[str]] = None,
    quick_replies_mode: Optional[str] = None,
) -> FlowsFunctionSchema:
    """The ``render_ui`` function schema (Vertex-safe subset: no
    additionalProperties — ``bind`` is an array of {prop, ref} pairs).

    ``trusted_urls`` (template ``trusted_link_urls``) self-documents in the
    ``link`` arg description — without it the model doesn't KNOW the
    configured checkout URL and guesses (live-observed: two rejected
    guesses, then a search just to harvest a usable URL)."""
    link_desc = (
        "LinkButton content (only with component='LinkButton'): a single "
        "link CTA for link-only answers (e.g. 'just give me the checkout "
        "link'). url must be one of the trusted URLs below or a URL from "
        "THIS turn's tool results — anything else is rejected."
    )
    if trusted_urls:
        link_desc += " Trusted URLs: " + ", ".join(sorted(trusted_urls))
    quick_desc = (
        "QuickReplies content (only with component='QuickReplies'): 2-5 "
        "short strings — each is exactly what the shopper sees on the "
        "pill AND what comes back as their next message when tapped."
    )
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
            "Optional, attachable to ANY call: 2-5 short follow-up "
            "strings shown as tappable pills UNDER your final reply — "
            "each is exactly what the shopper sees AND what comes back "
            "as their next message when tapped. Attach them to the "
            "render_ui call that accompanies your reply; placement is "
            "automatic. Labels <=4 words; never duplicate an action "
            "already available on UI rendered this turn (cards already "
            "carry Add to cart / View)."
        )
    return FlowsFunctionSchema(
        name=RENDER_UI_TOOL_NAME,
        # This description is LLM-facing (rides the tools schema on EVERY
        # cycle) — contract only, never server internals. Litmus test for a
        # sentence here: does it change what the model should DO? Behavioral
        # style lives in the system prompt; recovery behavior (e.g. the
        # same-turn ProductGrid merge) is explained just-in-time by the
        # function RESPONSE when it actually happens.
        description=(
            "Render UI for the shopper THIS turn. Call it once. Values are "
            "filled from this turn's tool results — you author selectors "
            "only. When a search returned products, default to showing "
            "them. decision='no_ui' renders nothing (empty results or a "
            "purely conversational reply)."
        ),
        properties={
            "component": {
                "type": "string",
                "enum": components,
                "description": "Which component to render.",
            },
            "bind": {
                "type": "array",
                "description": (
                    "Data bindings into THIS turn's tool results, e.g. "
                    "[{'prop':'products','ref':'$tool:search_catalog#/products'}]. "
                    "CartView binds cart_id/line_items/totals/cart_token off "
                    "the cart tool result the same way; its checkout button "
                    "is automatic — never author it."
                ),
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
                "description": (
                    "ProductGrid selection: which bound products to show, in "
                    "this order (ids from THIS turn's results). Use when the "
                    "shopper asked for specific product(s); omit for "
                    "open-ended browsing. feature_variant: a variant id from "
                    "that product's variants to feature as the card hero "
                    "(e.g. the pink one the shopper asked for) — prefer the "
                    "search result's matched_variant when present."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "feature_variant": {"type": "string"},
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


_GID_TAIL_RE = re.compile(r"/(\d+)$")


def _short_id(gid: Any) -> Any:
    """Full gid stays authoritative in ops; the SUMMARY may carry it whole —
    it's the referent the model reuses in items[]/view_product."""
    return gid


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


def url_is_trusted(
    url: str,
    store: BindingStore,
    trusted_urls: Optional[Set[str]],
    state_values: Optional[Dict[str, Any]] = None,
) -> bool:
    """LinkButton trust rule: exact match against the template's
    ``trusted_link_urls`` allowlist, verbatim presence in THIS turn's
    tool results, OR verbatim presence in reducer state — the model
    selects links, it never authors them. State qualifies because
    ``agent_session_state`` is written only by the template's reducers
    over tool payloads (e.g. ``policy_links`` captured from an earlier
    cart call), so a state-sourced URL has the same tool provenance as a
    this-turn one."""
    if trusted_urls and url in trusted_urls:
        return True
    if state_values and _payload_contains_url(state_values, url):
        return True
    return any(_payload_contains_url(payload, url) for payload in store.payloads())


def summarize_render(component: str, hydrated_op: Dict[str, Any]) -> Dict[str, Any]:
    """The function response for a successful render — the model's UI
    memory: what the shopper is looking at, compact and structured.
    ``items`` carries id + title (+ featured_variant) so referents like
    "the green one" and follow-up ``items[]`` selections resolve without
    re-searching."""
    props = hydrated_op.get("props") or {}
    result: Dict[str, Any] = {"status": "ok", "rendered": component}
    products = props.get("products")
    if isinstance(products, list):
        result["count"] = len(products)
        items: List[Dict[str, Any]] = []
        for entry in products[:_SUMMARY_ITEMS_CAP]:
            if not isinstance(entry, dict):
                continue
            item: Dict[str, Any] = {
                "id": _short_id(entry.get("id")),
                "title": str(entry.get("title", ""))[:_SUMMARY_TITLE_CAP],
            }
            fv = entry.get("featured_variant_id")
            if fv:
                item["featured_variant"] = fv
            items.append(item)
        result["items"] = items
    elif isinstance(props.get("product"), dict):
        p = props["product"]
        result["items"] = [
            {
                "id": _short_id(p.get("id")),
                "title": str(p.get("title", ""))[:_SUMMARY_TITLE_CAP],
            }
        ]
    elif isinstance(props.get("items"), list):  # QuickReplies
        result["count"] = len(props["items"])
    elif isinstance(props.get("line_items"), list):  # CartView
        result["count"] = len(props["line_items"])
    elif isinstance(props.get("url"), str):  # LinkButton
        result["label"] = props.get("label")
        result["url"] = props["url"]
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
    cart_checkout: Optional[Dict[str, Optional[str]]] = None,
    state_values: Optional[Dict[str, Any]] = None,
) -> RenderUiOutcome:
    """Run one ``render_ui`` call: validate → hydrate → summarize.

    Every failure returns a structured error in ``fn_result`` (never
    raises) — the model reads it in the function response and corrects its
    next call. That closes RFC-002 F1's silent-drop hole: an invalid render
    is no longer invisible to the model.

    ``restrict_to`` narrows THIS call's renderable components (the forced
    final quick-replies cycle passes ``{"QuickReplies"}`` so the turn's
    tail can never sprout a second grid); ``decision='no_ui'`` stays legal.

    ``cart_checkout`` is ``{"label": …, "url": fallback-or-None}``: when the
    hydrated component's schema has a ``checkout`` field, the server stamps
    the button — bound payload's ``continue_url`` first, fallback url next.
    The model never authors it.
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
    # server policy, never a model choice — stamped after hydration from
    # the product count (see below). A model-passed value is ignored.
    items = args.get("items")
    if isinstance(items, list) and items:
        selectors: List[Dict[str, Any]] = []
        for entry in items:
            if isinstance(entry, str) and entry:
                selectors.append({"id": entry})
            elif (
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"]
            ):
                sel: Dict[str, Any] = {"id": entry["id"]}
                fv = entry.get("feature_variant")
                if isinstance(fv, str) and fv:
                    sel["feature_variant"] = fv
                selectors.append(sel)
        if selectors:
            props["items"] = selectors
    # ``checkout`` is deliberately NOT read from args (2026-07-30): the
    # CartView checkout button is server policy — stamped after hydration
    # from the bound cart payload's continue_url (fallback: the reducer
    # state url the caller passes via ``cart_checkout``). Mirrors the
    # DIRECT-intent path (_cart_view_show_op).
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
        props["items"] = chip_items
    if component == "LinkButton":
        link = args.get("link")
        if not (isinstance(link, dict) and isinstance(link.get("url"), str)):
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        "LinkButton needs link={'label': …, 'url': …} — the "
                        "url must be the store's configured checkout URL or "
                        "a URL from THIS turn's tool results"
                    ),
                },
                component=component,
            )
        url = link["url"]
        if not url_is_trusted(url, store, trusted_urls, state_values):
            hint = (
                "use one of: " + ", ".join(sorted(trusted_urls))
                if trusted_urls
                else "pass the store's configured checkout URL"
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

    if is_data_bound(component):
        if not bind:
            return RenderUiOutcome(
                fn_result={
                    "status": "error",
                    "error": (
                        f"{component} is data-bound: pass bind, e.g. "
                        "[{'prop':'products',"
                        "'ref':'$tool:search_catalog#/products'}]"
                    ),
                },
                component=component,
            )
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
        result = resolve_show_op(op, store, allowlist)
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
        if isinstance(hydrated_props, dict) and isinstance(
            hydrated_props.get("products"), list
        ):
            # Layout is server policy derived from the FINAL hydrated count
            # (post items[]-selection, post max_items cap): 1-2 products sit
            # side by side; 3+ scroll as a carousel.
            hydrated_props["layout"] = (
                "grid" if len(hydrated_props["products"]) <= 2 else "carousel"
            )
        schema_cls = UI_CATALOG.get(component)
        if (
            cart_checkout is not None
            and isinstance(hydrated_props, dict)
            and schema_cls is not None
            and "checkout" in schema_cls.model_fields
            and not hydrated_props.get("checkout")
        ):
            # Checkout button is server policy: the bound cart payload's
            # continue_url wins, the reducer-state fallback next; no url
            # anywhere → no button (CartView renders without it).
            checkout_url: Optional[str] = None
            for ref in bind.values():
                parsed = parse_bind_ref(ref)
                if parsed is None:
                    continue
                payload = store.resolve(parsed.tool_name, parsed.tool_use_id)
                if isinstance(payload, dict):
                    cu = payload.get("continue_url")
                    if isinstance(cu, str) and cu:
                        checkout_url = cu
                        break
            checkout_url = checkout_url or cart_checkout.get("url")
            if checkout_url:
                hydrated_props["checkout"] = {
                    "label": cart_checkout.get("label") or "Review and checkout",
                    "url": checkout_url,
                }
        return RenderUiOutcome(
            fn_result=summarize_render(component, result.op),
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
    "RenderUiOutcome",
    "build_render_ui_schema",
    "build_revise_plan_schema",
    "execute_render_ui",
    "render_ui_components",
    "summarize_render",
    "url_is_trusted",
]
