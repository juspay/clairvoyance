"""Commerce UI-intent policy + direct cart executor (RFC-001 §3.3).

The flavor half of ``chat/intent_router.py``: per-intent payload schemas,
the policy table (add_to_cart / remove_line / set_qty / view_product →
DIRECT, enrich_product → AGENT_TURN internal, checkout → CLIENT), the
Stage-A UCP cart tool-name / state-key constants, and the CartView
``show``-op emission.

Lazy-loaded flavor module: importing it registers the policies into the
intent engine (see ``register_intents`` at the bottom). The import happens
only via ``intent_router.ensure_flavor_intents`` — i.e. only when a
session on a commerce-enabled template sends a ``ui_intent``.

Tool names / state keys / labels resolve per-template through the
``configurations.ui_intents`` block (Stage B); the UCP defaults below
apply when the block (or an individual role) is absent — the Beyond Bound
pilot template's reducers/injections speak exactly these default names.

Payload compatibility policy
----------------------------
Payloads are produced by our own versioned widget; additive fields must
never break older/newer servers. Per-intent payload models therefore use
``extra="ignore"``: required-field validation stays strict (that is the
real safety), while unknown extra keys are accepted and dropped, with one
structured WARNING per request listing the dropped key NAMES (never
values) so payload drift stays visible in telemetry without breaking
shoppers. The captured widget emissions live in
``tests/assist/fixtures/intent_payloads.json`` (mirrored byte-identically
in the loom repo at
``packages/client-sdk/src/lib/chat/__fixtures__/intent_payloads.json``) —
update both fixtures and both repos' contract tests when an emission
changes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.media import (
    resolve_product_media,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.roles import (
    DEFAULT_TOOLS,
    ROLE_CREATE_CART,
    ROLE_GET_CART,
    ROLE_GET_PRODUCT,
    ROLE_SEARCH,
    ROLE_UPDATE_CART,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.upsell import run_cart_upsell
from app.ai.voice.agents.breeze_buddy.chat.intents.router import (
    IntentPolicy,
    IntentRoute,
    ParsedIntent,
    error_events,
    register_intents,
    run_persisted_tool,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent

# Same envelope helpers the engine + binding store use — one definition of
# "success" / "payload" across every result consumer. Private-name import
# is deliberate (same posture as chat/ui_binding.py): re-implementing them
# here would drift.
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _is_tool_success,
    _unwrap_tool_payload,
)
from app.core.logger import logger

# ---------------------------------------------------------------------------
# Cart tool roles — UCP defaults, overridable per template via
# ``configurations.ui_intents`` (roles: tools.create_cart /
# tools.update_cart / tools.get_cart, state_keys.cart_id /
# state_keys.checkout_url, labels.checkout).
# ---------------------------------------------------------------------------

# The role names and their default tool bindings are declared once in
# ``roles.py`` — the same table the engine resolves flavor registries
# through, so the driver and the metadata can never drift apart.
_TOOL_CREATE_CART = DEFAULT_TOOLS[ROLE_CREATE_CART]
_TOOL_UPDATE_CART = DEFAULT_TOOLS[ROLE_UPDATE_CART]
_TOOL_GET_CART = DEFAULT_TOOLS[ROLE_GET_CART]
_TOOL_GET_PRODUCT = DEFAULT_TOOLS[ROLE_GET_PRODUCT]
_TOOL_SEARCH = DEFAULT_TOOLS[ROLE_SEARCH]
_STATE_CART_ID = "cart_id"
_STATE_CHECKOUT_URL = "checkout_url"
_CHECKOUT_LABEL = "Review and checkout"


@dataclass(frozen=True)
class CartToolConfig:
    """The commerce driver's resolved tool/state/label roles for one
    template. Defaults = the UCP Stage-A surface."""

    create_cart: str = _TOOL_CREATE_CART
    update_cart: str = _TOOL_UPDATE_CART
    get_cart: str = _TOOL_GET_CART
    get_product: str = _TOOL_GET_PRODUCT
    search: str = _TOOL_SEARCH
    cart_id_key: str = _STATE_CART_ID
    checkout_url_key: str = _STATE_CHECKOUT_URL
    checkout_label: str = _CHECKOUT_LABEL
    # Optional fixed destination for the CartView checkout button
    # (template `ui_intents.urls.checkout_page`). Set it to the
    # storefront's /cart page to land shoppers on the native cart (the
    # CartView cookie sync makes it show the buddy-built items) instead
    # of the tool's checkout-bound continue_url. None = continue_url.
    checkout_page_url: Optional[str] = None
    # Post-add upsell (template `flavor.ucp.features.upsell`). OPT-IN:
    # off unless the merchant asks for it, because it spends an extra LLM
    # call + catalog search on every add_to_cart and puts merchandising
    # the merchant did not choose into their thread.
    upsell_enabled: bool = False
    # Platform connectors this template's gateway needs
    # (`flavor.ucp.connectors`). Empty = every registered connector
    # self-selects on the payload, which is right whenever the platform is
    # unambiguous; naming them stops a look-alike gateway paying for a
    # connector that will never match.
    connectors: Tuple[str, ...] = ()


# The protocol dialect this flavor speaks — the key it reads out of the
# template's ``configurations.flavor`` map, and the name of its own package
# directory. A second dialect would be a sibling package + a second key.
UCP_PROTOCOL = "ucp"


@dataclass(frozen=True)
class _FlavorBlock:
    """The resolved ``flavor.ucp`` block, defaulted when absent."""

    connectors: Tuple[str, ...] = ()
    features: Dict[str, bool] = field(default_factory=dict)


def _resolve_flavor_block(configurations: Any) -> _FlavorBlock:
    """Read ``configurations.flavor["ucp"]``; every field optional."""
    flavors = getattr(configurations, "flavor", None) or {}
    if not isinstance(flavors, dict):
        return _FlavorBlock()
    block = flavors.get(UCP_PROTOCOL)
    if block is None:
        # The engine can't validate these keys (it doesn't know any
        # flavor's protocols), so the flavor that owns them says so. The
        # likely typo is `flavor.commerce` — every OTHER flavor-keyed
        # surface (ui_catalog.enabled_groups, LAZY_GROUPS,
        # FLAVOR_INTENT_MODULES) keys on the flavor name, this one keys on
        # the protocol — and it would otherwise read as "everything off".
        if flavors:
            logger.warning(
                f"commerce: configurations.flavor has no {UCP_PROTOCOL!r} "
                f"block (found {sorted(flavors)!r}); connectors will "
                "self-select and every optional feature stays off"
            )
        return _FlavorBlock()
    return _FlavorBlock(
        connectors=tuple(getattr(block, "connectors", None) or ()),
        features=dict(getattr(block, "features", None) or {}),
    )


def resolve_cart_config(template: Any) -> CartToolConfig:
    """Overlay the template's ``configurations.ui_intents`` block (if
    any) onto the UCP defaults. Unknown roles in the block are ignored —
    they may belong to another flavor sharing the same template."""
    configurations = getattr(template, "configurations", None)
    flavor = _resolve_flavor_block(configurations)
    cfg = getattr(configurations, "ui_intents", None)
    if cfg is None:
        return CartToolConfig(
            upsell_enabled=bool(flavor.features.get("upsell", False)),
            connectors=tuple(flavor.connectors or ()),
        )
    tools = cfg.tools or {}
    keys = cfg.state_keys or {}
    labels = cfg.labels or {}
    urls = getattr(cfg, "urls", None) or {}
    return CartToolConfig(
        create_cart=tools.get("create_cart", _TOOL_CREATE_CART),
        update_cart=tools.get("update_cart", _TOOL_UPDATE_CART),
        get_cart=tools.get("get_cart", _TOOL_GET_CART),
        get_product=tools.get("get_product", _TOOL_GET_PRODUCT),
        search=tools.get("search", _TOOL_SEARCH),
        cart_id_key=keys.get("cart_id", _STATE_CART_ID),
        checkout_url_key=keys.get("checkout_url", _STATE_CHECKOUT_URL),
        checkout_label=labels.get("checkout", _CHECKOUT_LABEL),
        checkout_page_url=urls.get("checkout_page"),
        upsell_enabled=bool(flavor.features.get("upsell", False)),
        connectors=tuple(flavor.connectors or ()),
    )


# ---------------------------------------------------------------------------
# Per-intent payload schemas
#
# Each schema explicitly covers every field the widget emits TODAY (see
# the payload-compatibility policy in the module docstring and the
# contract fixtures in tests/assist/fixtures/intent_payloads.json).
# Unknown extra keys — a newer widget talking to this server — are
# accepted, dropped, and logged by the shared base class below.
# ---------------------------------------------------------------------------


class _IntentPayload(BaseModel):
    """Shared base for per-intent payload schemas.

    ``extra="ignore"``: payloads come from our own versioned widget, so an
    unknown key is expected version skew (additive field), never hostile
    input — required-field validation stays strict, unknown keys are
    dropped. The before-validator logs ONE structured WARNING per request
    listing the dropped key names (structural only — key names, never
    values, same privacy contract as ``_structural_errors``) so drift is
    visible in telemetry without 422-ing shoppers.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _warn_dropped_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            dropped = sorted(key for key in data if key not in cls.model_fields)
            if dropped:
                logger.warning(
                    f"ui_intent payload drift: {cls.__name__} dropped unknown "
                    f"keys {dropped} (key names only; values never logged)"
                )
        return data


class AddToCartPayload(_IntentPayload):
    """Widget emission (ProductCard): variant_id + qty + product_id."""

    variant_id: str = Field(..., min_length=1, max_length=256)
    qty: int = Field(1, ge=1, le=99)
    # Sent by ProductCard for audit/re-resolution context; unused by the
    # cart driver (the variant GID alone addresses the line).
    product_id: Optional[str] = Field(None, max_length=256)


class RemoveLinePayload(_IntentPayload):
    """Widget emission (CartView): line_id [+ variant_id] [+ cart_id]."""

    line_id: str = Field(..., min_length=1, max_length=256)
    # CartView includes these when known; audit-only context for the driver
    # (line_id alone addresses the line; cart_id comes from session state).
    variant_id: Optional[str] = Field(None, max_length=256)
    cart_id: Optional[str] = Field(None, max_length=512)


class SetQtyPayload(_IntentPayload):
    """Widget emission (CartView): line_id + variant_id + qty [+ cart_id]."""

    line_id: str = Field(..., min_length=1, max_length=256)
    qty: int = Field(..., ge=0, le=99, description="0 removes the line.")
    # CartView includes these when known; audit-only context for the driver.
    variant_id: Optional[str] = Field(None, max_length=256)
    cart_id: Optional[str] = Field(None, max_length=512)


class ViewProductPayload(_IntentPayload):
    """Widget emission (ProductCard): product_id + title [+ url]."""

    product_id: str = Field(..., min_length=1, max_length=256)
    title: Optional[str] = Field(None, max_length=200)
    # Storefront product URL, sent when the hydrated ProductP carried one.
    # Audit/handoff context only — the agent turn is built from
    # product_id + title.
    url: Optional[str] = Field(None, max_length=2048)
    # Variant continuity: the variant the card's hero was re-derived from
    # (render_ui ``items[].feature_variant`` stamps it on ProductP). The
    # widget preselects it in the detail overlay CLIENT-side; the server
    # models it so the emission parses drift-free (audit context only).
    featured_variant_id: Optional[str] = Field(None, max_length=256)
    # The card's search-sourced variant list (id/title/available/price).
    # Live gateways commonly wrap a get-product-details call that
    # returns ONLY the selected variant — this relay lets the detail
    # overlay still offer the full size axis. Display-only provenance:
    # variant ids are client-authored in add_to_cart anyway, and the
    # deterministic cart verifier is the backstop for a bogus id.
    variants: Optional[List[Dict[str, Any]]] = Field(None, max_length=24)


class EnrichProductPayload(_IntentPayload):
    """Widget emission (detail overlay, background): product_id [+ title].

    Fired by the store AFTER the view_product hydration lands — the
    rewritten agent turn asks for a short "Buddy says" note that streams
    into the open overlay. The product data itself is already in the
    LLM's context (the view_product tool exchange persisted just before),
    so the payload only needs to identify the product.
    """

    product_id: str = Field(..., min_length=1, max_length=256)
    title: Optional[str] = Field(None, max_length=200)


def _enrich_product_agent_turn(parsed: ParsedIntent) -> str:
    """Rewrite enrich_product into the internal instruction the LLM answers.

    The brief IS the overlay's body copy (2026-07-31 minimalist redesign
    — the raw merchant description is no longer shown): a compact
    markdown product brief, grounded in the product data already in
    context (the get_product exchange from the view fetch). Markdown-only
    contract: no tools, no UI calls; the widget drops any op/step frames
    from this turn defensively.
    """
    payload = parsed.payload
    assert isinstance(payload, EnrichProductPayload)  # policy table guarantees
    label = (
        f'"{payload.title}" ({payload.product_id})'
        if payload.title
        else (payload.product_id)
    )
    return (
        "[Background enrichment — a system request, not the shopper speaking.] "
        f"The shopper just opened the product detail view for {label}. "
        "Write the product brief that view shows below the price and "
        "add-to-cart button, in markdown, under 90 words total: one "
        "sentence on what it is and who it's for, then 2-4 tight bullet "
        "points (fabric/feel, standout details, fit or sizing guidance, "
        "what it pairs with). Ground every claim in the product data "
        "already in this conversation; do not invent specifics; skip "
        "anything the data doesn't support. Do not repeat the product "
        "title or price — they're already on screen. Do not call any "
        "tools. Markdown text only — no headings, no links, no images, "
        "and no greeting."
    )


class CheckoutPayload(_IntentPayload):
    """Client-routed — the widget opens the storefront cart URL itself;
    the schema exists so an (unexpected) server-side arrival still 422s
    with a typed error instead of a KeyError."""

    url: Optional[str] = Field(None, max_length=2048)


async def _drive_view_product(
    agent: Any,
    prep: Any,
    node: Dict[str, Any],
    parsed: ParsedIntent,
    turn_id: str,
) -> AsyncIterator[Tuple[Optional[SSEEvent], Optional[str], Any]]:
    """view_product (DIRECT) → one deterministic ``get_product`` read.

    Feeds the widget's full-panel detail overlay: the overlay opens with a
    skeleton on click; this fetch hydrates the server-only ProductDetail
    component through the standard show-op resolver. No LLM in the loop —
    the "which call" decision is the template's ``ui_intents`` mapping.
    """
    payload = parsed.payload
    assert isinstance(payload, ViewProductPayload)  # policy table guarantees
    cfg = resolve_cart_config(agent.template)
    events, result = await run_persisted_tool(
        agent,
        tool_name=cfg.get_product,
        args={"catalog": {"id": payload.product_id}},
        node=node,
        prep=prep,
        turn_id=turn_id,
    )
    for ev in events:
        yield ev, None, None
    # Binding enrichment: re-record an ENRICHED payload so the hydrated
    # ProductDetail is richer than the raw wire read. The persisted
    # tool_result stays the original — the LLM's context is untouched;
    # only this turn's binding store (which the show-op resolves against)
    # sees the merge. Two grafts compose here:
    #   (a) variants — single-variant read + a multi-variant card: the
    #       payload-relayed axis replaces the lone variant (A6).
    #   (b) media — the gateway ships ONE image per product; the media
    #       resolver (assist/commerce/media.py, no LLM) fills the full
    #       gallery, and is a no-op when UCP media is already rich.
    if _is_tool_success(result):
        unwrapped = _unwrap_tool_payload(result)
        product = unwrapped.get("product") if isinstance(unwrapped, dict) else None
        if isinstance(product, dict):
            enriched: Optional[Dict[str, Any]] = None

            def _enriched_product() -> Dict[str, Any]:
                nonlocal enriched
                if enriched is None:
                    enriched = copy.deepcopy(unwrapped)
                    assert isinstance(enriched, dict)  # mirror of unwrapped
                ep = enriched["product"]
                assert isinstance(ep, dict)  # mirror of `product`
                return ep

            if (
                payload.variants
                and len([v for v in (product.get("variants") or []) if v]) <= 1
                and len(payload.variants) > 1
            ):
                ep = _enriched_product()
                ep["variants"] = payload.variants
                ep.pop("default_variant_id", None)

            gallery = await resolve_product_media(
                agent.aiohttp_session, product, connectors=cfg.connectors
            )
            if gallery:
                _enriched_product()["media"] = gallery

            if enriched is not None:
                agent.binding_store.record(cfg.get_product, None, enriched)
    yield None, cfg.get_product, result


def _product_detail_show_op(tool_name: str, result: Any, agent: Any) -> Dict[str, Any]:
    """Server-authored ``show ProductDetail`` over the get_product result —
    same resolver/validation path as every LLM-authored show op."""
    return {
        "op": "show",
        "id": "root",
        "component": "ProductDetail",
        "bind": {"product": f"$tool:{tool_name}#/product"},
    }


# ---------------------------------------------------------------------------
# Direct cart executor (driven by intent_router.run_direct_intent)
# ---------------------------------------------------------------------------


def _cart_lines(cart_payload: Any) -> List[Dict[str, Any]]:
    """The ``line_items`` array off a post-pipeline cart payload."""
    if isinstance(cart_payload, dict) and isinstance(
        cart_payload.get("line_items"), list
    ):
        return [ln for ln in cart_payload["line_items"] if isinstance(ln, dict)]
    return []


def _line_variant_id(line: Dict[str, Any]) -> Optional[str]:
    item = line.get("item")
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return None


class UnaddressableCartLine(Exception):
    """A cart line could not be re-encoded into ``update_cart``'s desired set.

    ``update_cart`` REPLACES the cart with the set we send, so a line we
    silently skip is a line we DELETE. Refusing the mutation is the only
    safe answer: the shopper retries or edits on the storefront, versus
    losing a line they never touched and getting no error.
    """

    def __init__(self, line: Dict[str, Any]) -> None:
        super().__init__(f"cart line {line.get('id')!r} cannot be re-encoded")
        self.line_id = line.get("id")


def _desired_line_items(
    lines: List[Dict[str, Any]],
) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    """Re-encode current cart lines as ``update_cart``'s full desired set,
    each entry paired with the cart-line id it came from.

    The pairing matters: ``line_items[].id`` is the LINE and ``item.id`` the
    variant, and a cart may legitimately hold two lines of the same variant
    (different attributes/properties). Mutations must therefore select on
    the line id — keying on the variant would hit every line sharing it.

    Raises :class:`UnaddressableCartLine` rather than skipping a line — see
    that exception's docstring.
    """
    desired: List[Tuple[Optional[str], Dict[str, Any]]] = []
    for line in lines:
        variant_id = _line_variant_id(line)
        qty = line.get("quantity")
        if variant_id is None or not isinstance(qty, int):
            raise UnaddressableCartLine(line)
        line_id = line.get("id")
        desired.append(
            (
                line_id if isinstance(line_id, str) else None,
                {"item": {"id": variant_id}, "quantity": qty},
            )
        )
    return desired


async def _drive_cart_tools(
    agent: Any,
    prep: Any,
    node: Dict[str, Any],
    parsed: ParsedIntent,
    turn_id: str,
) -> AsyncIterator[Tuple[Optional[SSEEvent], Optional[str], Any]]:
    """Run the per-intent cart tool sequence, yielding ``(sse_event,
    final_tool_name, final_result)`` triples — events stream as they
    happen; the final tool/result pair rides the last dispatch. On a
    pre-dispatch failure (missing cart, unknown line) the terminal error
    events are yielded and no final pair is produced.

    ``update_cart``'s ``line_items`` is the FULL desired set (UCP), so the
    mutating intents against an existing cart read it back first
    (``get_cart``) and re-encode — the read rides the same pipeline, so
    transforms/reducers apply uniformly.
    """
    payload = parsed.payload
    cfg = resolve_cart_config(agent.template)
    cart_id = agent.agent_state.get(cfg.cart_id_key)

    async def _run(tool_name: str, args: Dict[str, Any]) -> Tuple[List[SSEEvent], Any]:
        return await run_persisted_tool(
            agent, tool_name=tool_name, args=args, node=node, prep=prep, turn_id=turn_id
        )

    if isinstance(payload, AddToCartPayload) and cart_id is None:
        # First add — no cart yet: create it with the one line.
        events, result = await _run(
            cfg.create_cart,
            {
                "cart": {
                    "line_items": [
                        {
                            "item": {"id": payload.variant_id},
                            "quantity": payload.qty,
                        }
                    ]
                }
            },
        )
        for ev in events:
            yield ev, None, None
        yield None, cfg.create_cart, result
        return

    # Every other direct mutation needs the current lines to build the
    # full desired set. Args stay minimal — the template's
    # tool_arg_injection fills `id` from state (only_if_missing).
    events, get_result = await _run(cfg.get_cart, {})
    for ev in events:
        yield ev, None, None
    if not _is_tool_success(get_result):
        for ev in error_events(
            "intent_tool_failed", "Could not load the current cart."
        ):
            yield ev, None, None
        return
    lines = _cart_lines(_unwrap_tool_payload(get_result))
    try:
        pairs = _desired_line_items(lines)
    except UnaddressableCartLine as exc:
        # Fail closed: shipping a lossy replacement set would delete the
        # line we can't encode, without the shopper ever asking.
        logger.warning(
            f"commerce cart: line {exc.line_id!r} is not re-encodable; "
            "refusing the update rather than dropping it"
        )
        for ev in error_events(
            "intent_cart_unsupported",
            "This cart has an item we can't update from here. "
            "Please make the change on the cart page.",
        ):
            yield ev, None, None
        return

    if isinstance(payload, AddToCartPayload):
        for _line_id, entry in pairs:
            if entry["item"]["id"] == payload.variant_id:
                entry["quantity"] += payload.qty
                break
        else:
            pairs.append(
                (None, {"item": {"id": payload.variant_id}, "quantity": payload.qty})
            )
        desired = [entry for _line_id, entry in pairs]
    elif isinstance(payload, (RemoveLinePayload, SetQtyPayload)):
        # Select by LINE id, never by variant — two lines can share a
        # variant, and keying on it would remove/retitle both.
        new_qty = 0 if isinstance(payload, RemoveLinePayload) else payload.qty
        for line_id, entry in pairs:
            if line_id is not None and line_id == payload.line_id:
                entry["quantity"] = new_qty
                break
        else:
            for ev in error_events(
                "intent_line_not_found",
                "That item is no longer in the cart.",
            ):
                yield ev, None, None
            return
        desired = [entry for _line_id, entry in pairs if entry["quantity"] > 0]
    else:  # pragma: no cover — policy table guarantees a cart payload here
        for ev in error_events(
            "invalid_intent", f"Intent {parsed.intent.intent!r} is not direct."
        ):
            yield ev, None, None
        return

    events, update_result = await _run(
        cfg.update_cart, {"cart": {"line_items": desired}}
    )
    for ev in events:
        yield ev, None, None
    yield None, cfg.update_cart, update_result


def _cart_failure_message(result: Any) -> Optional[str]:
    """Shopper-displayable reason off a failed cart result.

    The UCP cart payload carries a ``messages[]`` array of display-grade
    entries — e.g. ``{"type":"warning","content_type":"plain","code":
    "merchandise_out_of_stock","content":"The product '…' is already sold
    out."}`` on a create/update that silently skipped a line (which the
    deterministic verifier converts into an error envelope carrying the
    raw payload under ``unverified_result``). Only those plain-content
    entries are returned — never envelope internals; ``None`` falls back
    to the engine's generic copy.
    """
    candidates: List[Any] = []
    if isinstance(result, dict):
        # unverified_result may be the raw post-pipeline dict OR still the
        # {status, data} envelope (test fixtures / non-projected servers) —
        # check both the unwrapped and verbatim forms.
        unverified = result.get("unverified_result")
        if isinstance(unverified, dict):
            candidates.append(_unwrap_tool_payload(unverified))
            candidates.append(unverified)
        candidates.append(_unwrap_tool_payload(result))
        candidates.append(result)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        messages = candidate.get("messages")
        if not isinstance(messages, list):
            continue
        for entry in messages:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") not in ("warning", "error"):
                continue
            if entry.get("content_type", "plain") != "plain":
                continue
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()[:200]
    return None


def _cart_ack(parsed: ParsedIntent, tool_name: str, result: Any) -> str:
    """Cart mutations acknowledge with one plain line ABOVE the rendered
    cart (2026-07-30 user spec). The fresh CartView sweeps earlier cart
    blocks client-side, so this line is what keeps old mutation turns
    visibly anchored after their cart card vanishes."""
    return "Done."


def _cart_view_show_op(tool_name: str, result: Any, agent: Any) -> Dict[str, Any]:
    """Server-authored ``show CartView`` op over the final cart result.

    Binds only pointers that exist in the payload (an absent optional field
    must not fail the whole op — RFC drops on ANY unresolved bind); the
    checkout handoff is a literal prop built from the cart's
    ``continue_url`` (falling back to reducer state) since the UCP payload
    has no ``{label, url}`` object to point at.
    """
    cfg = resolve_cart_config(agent.template)
    payload = _unwrap_tool_payload(result)
    payload = payload if isinstance(payload, dict) else {}
    bind: Dict[str, str] = {"cart_id": f"$tool:{tool_name}#/id"}
    for prop, key in (
        ("line_items", "line_items"),
        ("totals", "totals"),
        ("cart_token", "cart_token"),
    ):
        if payload.get(key) is not None:
            bind[prop] = f"$tool:{tool_name}#/{key}"
    props: Dict[str, Any] = {}
    # Configured fixed destination (template ui_intents.urls.checkout_page,
    # e.g. the storefront /cart page — cookie sync shows the built cart
    # there) WINS over the tool's checkout-bound continue_url.
    checkout_url = (
        cfg.checkout_page_url
        or payload.get("continue_url")
        or agent.agent_state.get(cfg.checkout_url_key)
    )
    if isinstance(checkout_url, str) and checkout_url:
        props["checkout"] = {"label": cfg.checkout_label, "url": checkout_url}
    return {
        "op": "show",
        "id": "root",
        "component": "CartView",
        "bind": bind,
        "props": props,
    }


# ---------------------------------------------------------------------------
# Registration — runs once, at (lazy) import time
# ---------------------------------------------------------------------------

register_intents(
    "commerce",
    {
        "add_to_cart": IntentPolicy(
            IntentRoute.DIRECT,
            AddToCartPayload,
            default_display="Add to cart",
            drive=_drive_cart_tools,
            show_op=_cart_view_show_op,
            failure_message=_cart_failure_message,
            ack_message=_cart_ack,
            # Adds only — qty tweaks and removals must not re-pitch. The
            # upsell streams AFTER the CartView (see upsell.py), so the
            # mutation's perceived latency is unchanged.
            followup=run_cart_upsell,
        ),
        "remove_line": IntentPolicy(
            IntentRoute.DIRECT,
            RemoveLinePayload,
            default_display="Remove from cart",
            drive=_drive_cart_tools,
            show_op=_cart_view_show_op,
            failure_message=_cart_failure_message,
            ack_message=_cart_ack,
        ),
        "set_qty": IntentPolicy(
            IntentRoute.DIRECT,
            SetQtyPayload,
            default_display="Update quantity",
            drive=_drive_cart_tools,
            show_op=_cart_view_show_op,
            failure_message=_cart_failure_message,
            ack_message=_cart_ack,
        ),
        "view_product": IntentPolicy(
            IntentRoute.DIRECT,
            ViewProductPayload,
            default_display="View product",
            drive=_drive_view_product,
            show_op=_product_detail_show_op,
            # Full-panel overlay UX: nothing appears in the chat thread —
            # no user bubble, no persisted ui block (the tool exchange
            # still persists so the agent knows what the shopper viewed).
            silent=True,
        ),
        "enrich_product": IntentPolicy(
            IntentRoute.AGENT_TURN,
            EnrichProductPayload,
            agent_turn=_enrich_product_agent_turn,
            # Background overlay blurb: the whole turn (instruction +
            # prose) persists visibility=internal — the LLM keeps the
            # context, resume replay never shows it, no user_committed.
            internal=True,
        ),
        "checkout": IntentPolicy(IntentRoute.CLIENT, CheckoutPayload),
    },
)


__all__ = [
    "AddToCartPayload",
    "RemoveLinePayload",
    "SetQtyPayload",
    "ViewProductPayload",
    "EnrichProductPayload",
    "CheckoutPayload",
    "CartToolConfig",
    "resolve_cart_config",
]
