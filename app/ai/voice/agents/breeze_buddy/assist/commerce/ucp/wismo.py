"""WISMO (order tracking) — the commerce flavor's ``OrderStatus`` surface.

One data-bound component replaces the v1 hand-composed ``<ui_stream>``
order card. Hard order facts BIND off the ``get_order_status`` result
(``$tool:get_order_status#/orders/0``); the soft enrichment strings —
ETA, latest update, timeline rows — are **model-authored literal fields**
transcribed from the courier page the ``read_page_content`` tool fetched.
The LLM does the heavy lifting; the server only SHAPES (caps, list
bounds, same-turn-page-read requirement — :func:`verify_wismo_literals`)
and never judges content. Courier-agnostic by construction: there are NO
per-courier parsers anywhere in this module.

Platform-agnostic the same way: ``{found, orders: [...]}`` with the order
object at ``orders[0]`` is the CANONICAL bound shape — the card's wire
contract, not any endpoint's. Each commerce platform's lookup keeps its
own endpoint URL/auth/params in template JSON and, when its response
differs, normalizes into this shape via the template's
``tool_response_transforms``. This module never learns per-platform
schemas; only ``derive_status`` vocabulary may need widening for a new
platform's status strings (unknown values degrade to the neutral
"Order update", never crash).

Pieces (all registered by :func:`register_commerce_wismo`, called from
``schemas.py`` on the flavor's lazy import):

- :class:`OrderP` — projection of one nautilus WISMO order entry.
- :class:`OrderStatus` — the data-bound component; the v1 prompt's
  status→headline/tone table lives here as CODE (:func:`derive_status`).
- :func:`wrap_page_read_result` — courier-blind result annotator on the
  ``page_read`` role: wraps the raw page text into ``{"page_text": …}``
  so it is bind-addressable to the literal-fields gate (and capped).
- :func:`verify_wismo_literals` — the render_ui literal-fields trust
  gate, wired through the flavor pack (``render_ui.py``).
- Step labels + ``read_only`` annotations for both WISMO roles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.roles import (
    ROLE_ORDER_STATUS,
    ROLE_PAGE_READ,
    resolve_role_map,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.step_labels import (
    COMMERCE_GROUP,
)
from app.ai.voice.agents.breeze_buddy.chat.flavors import role_key
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import register_step_labels
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import (
    register_tool_annotations,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.result_annotators import (
    register_result_annotator,
)

# Private-name import is deliberate (same posture as schemas.py):
# _CatalogBase is the catalog's schema contract and re-declaring it here
# would drift.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    _CatalogBase,
    register_primitives,
)

# Cap on the wrapped courier-page text — bounds LLM context cost. ~15k
# chars comfortably covers the live Shiprocket sample (~30k raw includes
# footer legalese the card never needs).
PAGE_TEXT_CAP = 15_000

# Literal-field display caps (Phase 0 contract). The gate truncates to
# these so an over-long transcription degrades to a render, never to a
# whole-op validation failure.
LITERAL_CAPS: Dict[str, int] = {"eta_display": 40, "latest_update": 120}
UPDATES_MAX_ITEMS = 5
UPDATE_ITEM_CAP = 120


# ---------------------------------------------------------------------------
# Projection — one nautilus WISMO order entry
# ---------------------------------------------------------------------------


def _friendly_day(iso_ts: str) -> Optional[str]:
    """``2026-08-24T18:03:17+05:30`` → ``24 Aug`` (offset-aware — the live
    endpoint emits IST offsets, never Z)."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    return f"{dt.day} {dt.strftime('%b')}"


class OrderP(BaseModel):
    """Projection of ``orders[0]`` from the WISMO lookup result.

    Every field except ``order_name`` is nullable on the live wire; the
    card renders only what exists. ``financial_status`` is deliberately
    NOT projected (never rendered — Phase 0 contract)."""

    model_config = ConfigDict(extra="ignore")

    order_name: str
    order_number: Optional[int] = None
    created_at: Optional[str] = None
    placed_display: Optional[str] = None  # derived: "placed 24 Aug"
    fulfillment_status: Optional[str] = None
    line_items: List[str] = Field(default_factory=list)
    items_display: Optional[str] = None  # derived: "A · B ·  …and N more"
    tracking_company: Optional[str] = None
    tracking_number: Optional[str] = None
    tracking_url: Optional[str] = None
    shipment_status: Optional[str] = None
    order_status_url: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _lift(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        out = dict(v)
        # order_name falls back to the integer twin — the endpoint sends
        # both, but the card must never fail for want of a display name.
        if not out.get("order_name") and out.get("order_number") is not None:
            out["order_name"] = f"#{out['order_number']}"
        created = out.get("created_at")
        if not out.get("placed_display") and isinstance(created, str):
            day = _friendly_day(created)
            if day:
                out["placed_display"] = f"placed {day}"
        items = out.get("line_items")
        if isinstance(items, list):
            titles = [i for i in items if isinstance(i, str) and i]
            out["line_items"] = titles
            if titles and not out.get("items_display"):
                shown = " · ".join(titles[:2])
                extra = len(titles) - 2
                out["items_display"] = (
                    f"{shown} …and {extra} more" if extra > 0 else shown
                )
        return out


# ---------------------------------------------------------------------------
# Status derivation — the v1 prompt table, as code (single source of truth)
# ---------------------------------------------------------------------------

StatusKey = Literal[
    "delivered",
    "out_for_delivery",
    "in_transit",
    "attempted",
    "issue",
    "partial",
    "preparing",
    "unknown",
]

# Moving shipments — the only states where a transcribed ETA may lead the
# headline ("Arriving Friday, 10 July"). Delivered/issue states keep their
# status headline even if the page carried a stale ETA.
_ETA_HEADLINE_KEYS = {"in_transit", "out_for_delivery"}

_TONE: Dict[str, str] = {
    "delivered": "positive",
    "out_for_delivery": "info",
    "in_transit": "info",
    "attempted": "warning",
    "issue": "warning",
    "partial": "info",
    "preparing": "neutral",
    "unknown": "neutral",
}

_HEADLINE: Dict[str, str] = {
    "delivered": "Delivered",
    "out_for_delivery": "Out for delivery",
    "in_transit": "On its way",
    "attempted": "Delivery attempted",
    "issue": "Delivery issue",
    "partial": "Partially shipped",
    "preparing": "Being prepared",
    "unknown": "Order update",
}


def derive_status(
    shipment_status: Optional[str],
    fulfillment_status: Optional[str],
    has_tracking: bool,
) -> Tuple[str, str, str]:
    """``(status_key, headline, tone)`` for one order — priority order per
    the Phase 0 table: shipment signal first, then fulfillment fallbacks.

    ``confirmed`` and every ``label_*`` state read as "on its way" (live
    vocab confirmed 2026-08-25: a freshly-fulfilled Shadowfax shipment
    reports ``confirmed``)."""
    s = (shipment_status or "").strip().lower()
    if s == "delivered":
        key = "delivered"
    elif s == "out_for_delivery":
        key = "out_for_delivery"
    elif s in ("in_transit", "confirmed") or s.startswith("label_"):
        key = "in_transit"
    elif s == "attempted_delivery":
        key = "attempted"
    elif s == "failure":
        key = "issue"
    else:
        f = (fulfillment_status or "").strip().lower()
        if f == "partial":
            key = "partial"
        elif f == "fulfilled" and has_tracking:
            # Fulfilled with tracking but no shipment signal yet — moving.
            key = "in_transit"
        elif not has_tracking and f in ("", "unfulfilled", "null", "none"):
            key = "preparing"
        else:
            key = "unknown"
    return key, _HEADLINE[key], _TONE[key]


# ---------------------------------------------------------------------------
# The component
# ---------------------------------------------------------------------------


class OrderStatus(_CatalogBase):
    """Data-bound WISMO card — bind ``order`` to the lookup result's
    ``orders[0]``; transcribe the enrichment strings from the courier
    page as literal fields (server-shaped; see module docstring).

    ``status_key`` / ``headline`` / ``tone`` are SERVER-DERIVED at
    validation — never authored by the model, never re-derived by the
    widget — so a persisted op replays pixel-identical on resume."""

    data_bound: ClassVar[bool] = True
    # Model-authored fields accepted ONLY via the render_ui function path
    # and ONLY after the flavor's literal-fields gate shaped them. A
    # text-channel `show` op naming any of these drops at parse
    # (``literal_field_requires_render_ui``).
    literal_fields: ClassVar[Tuple[str, ...]] = (
        "eta_display",
        "latest_update",
        "updates",
    )

    order: OrderP
    eta_display: Optional[str] = Field(
        None,
        max_length=LITERAL_CAPS["eta_display"],
        description="Transcribed estimated-delivery date, e.g. 'Friday, 10 July'.",
    )
    latest_update: Optional[str] = Field(
        None,
        max_length=LITERAL_CAPS["latest_update"],
        description="Transcribed newest checkpoint, e.g. '25 Aug, 12:32 PM — Out for pickup'.",
    )
    updates: Optional[List[str]] = Field(
        None,
        max_length=UPDATES_MAX_ITEMS,
        description="Transcribed timeline rows, newest first (in-card expander).",
    )
    # Server-derived (filled below; the model never sets these — they are
    # not literal fields and hand-typed values are overwritten anyway).
    status_key: Optional[str] = None
    headline: Optional[str] = None
    tone: Optional[Literal["neutral", "info", "positive", "warning"]] = None

    @model_validator(mode="after")
    def _derive(self) -> "OrderStatus":
        key, headline, tone = derive_status(
            self.order.shipment_status,
            self.order.fulfillment_status,
            bool(self.order.tracking_url or self.order.tracking_number),
        )
        if self.eta_display and key in _ETA_HEADLINE_KEYS:
            headline = f"Arriving {self.eta_display}"
        self.status_key = key
        self.headline = headline
        self.tone = tone  # type: ignore[assignment]
        return self


# ---------------------------------------------------------------------------
# Result annotator — courier-blind page-text wrapper (role: page_read)
# ---------------------------------------------------------------------------


def wrap_page_read_result(args: Dict[str, Any], result: Any) -> Any:
    """Wrap the page-read tool's raw text payload as ``{"page_text": …}``.

    The HTTP handler returns ``{"status", "status_code", "data": "<text>"}``
    — a bare string payload that neither binds nor the literal-fields
    gate can address. Wrapping (capped) makes it a dict without touching the
    text itself. NO parsing, NO per-courier patterns — extraction is the
    LLM's job, trust is :func:`verify_wismo_literals`'s.

    Runs only for commerce-scoped sessions (registry is group+role keyed),
    so v1 templates keep seeing the raw string exactly as today. Error
    envelopes and already-dict payloads pass through untouched."""
    if not isinstance(result, dict) or result.get("status") == "error":
        return result
    data = result.get("data")
    if not isinstance(data, str) or not data.strip():
        return result
    out = dict(result)
    out["data"] = {"page_text": data[:PAGE_TEXT_CAP]}
    return out


def _resolve_page_text(store: Any, template: Any) -> Optional[str]:
    """This turn's fetched page text, via the template's ``page_read``
    role binding — proof this turn actually fetched something to
    transcribe from."""
    tool_name = resolve_role_map(template).get(ROLE_PAGE_READ, "read_page_content")
    payload = store.resolve(tool_name) if store is not None else None
    if isinstance(payload, dict):
        text = payload.get("page_text")
        return text if isinstance(text, str) and text.strip() else None
    # Defensive: annotator didn't run (e.g. mis-scoped session) — the raw
    # string payload still proves a page was read this turn.
    if isinstance(payload, str) and payload.strip():
        return payload[:PAGE_TEXT_CAP]
    return None


def verify_wismo_literals(
    component: str,
    schema_cls: Any,
    literal_args: Dict[str, Any],
    *,
    store: Any = None,
    template: Any = None,
    state_values: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """The literal-fields gate (flavor pack hook): pure MECHANICS, zero
    content judgment (product call 2026-08-26 — the LLM generates the
    results; the server only shapes them). Values are coerced to their
    expected shapes and truncated to display caps.

    The one rule kept: no page read this turn → everything drops — the
    fields exist to transcribe THIS turn's fetched results, and with no
    fetch there is nothing to transcribe from. Failures drop the FIELD,
    never the card; reasons ride the function response."""
    dropped: Dict[str, str] = {}
    accepted: Dict[str, Any] = {}
    page_text = _resolve_page_text(store, template)
    if page_text is None:
        return {}, {name: "no_page_read_this_turn" for name in literal_args}

    for name, raw in literal_args.items():
        if name == "updates":
            if not isinstance(raw, list):
                dropped[name] = "not_a_list"
                continue
            kept: List[str] = [
                entry.strip()[:UPDATE_ITEM_CAP]
                for entry in raw[:UPDATES_MAX_ITEMS]
                if isinstance(entry, str) and entry.strip()
            ]
            if kept:
                accepted[name] = kept
            else:
                dropped[name] = "not_a_list"
            continue
        if not isinstance(raw, str) or not raw.strip():
            dropped[name] = "not_a_string"
            continue
        accepted[name] = raw.strip()[: LITERAL_CAPS.get(name, UPDATE_ITEM_CAP)]
    return accepted, dropped


# ---------------------------------------------------------------------------
# Registration — rides the flavor's lazy import (schemas.py)
# ---------------------------------------------------------------------------


def register_commerce_wismo() -> None:
    """Register the WISMO surface into the flavor-agnostic registries:
    the component, the page-text wrapper annotator, step labels, and
    ``read_only`` annotations for both roles (without which the engine's
    default-``destructive`` posture would serialize two pure reads).

    Idempotent — same classes / same function objects on re-import."""
    register_primitives(COMMERCE_GROUP, {"OrderStatus": OrderStatus})
    register_result_annotator(
        COMMERCE_GROUP, role_key(ROLE_PAGE_READ), wrap_page_read_result
    )
    register_step_labels(
        COMMERCE_GROUP,
        {
            role_key(ROLE_ORDER_STATUS): (
                "Checking your order",
                "Found your order",
            ),
            role_key(ROLE_PAGE_READ): (
                "Reading tracking updates",
                "Read tracking updates",
            ),
        },
    )
    register_tool_annotations(
        COMMERCE_GROUP,
        {
            role_key(ROLE_ORDER_STATUS): "read_only",
            role_key(ROLE_PAGE_READ): "read_only",
        },
    )


__all__ = [
    "LITERAL_CAPS",
    "OrderP",
    "OrderStatus",
    "PAGE_TEXT_CAP",
    "UPDATES_MAX_ITEMS",
    "derive_status",
    "register_commerce_wismo",
    "verify_wismo_literals",
    "wrap_page_read_result",
]
