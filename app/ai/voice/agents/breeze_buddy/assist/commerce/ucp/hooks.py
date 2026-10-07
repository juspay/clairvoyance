"""Connector seams on the UCP layer.

UCP is the protocol this flavor speaks; a *connector* is a platform that
serves it. The protocol layer must stay platform-blind —
so wherever a real gateway's data needs platform knowledge to interpret,
the UCP module calls a hook here and connectors register into it.

Four seams. Three share one contract: the chain is EMPTY by default
(pure-UCP behavior — the projections work with no connector loaded), each
hook is asked in registration order, the first one to express an opinion
wins, and a hook that raises is skipped with a log rather than failing the
request. A connector is decoration over a protocol that already works.

Hooks self-select on the data they are handed rather than on declared
configuration — the media resolvers, for instance, return None the moment a
product URL isn't a path they recognise. That keeps the flavor zero-config:
enabling ``commerce`` is the only switch a template throws.

Sniffing is right where a wrong guess is free, and wrong where it costs.
The media seam reaches the network, so a look-alike gateway (any storefront
whose product URLs are also ``/products/{handle}``) would pay a dead fetch
per product view — that seam therefore accepts an ``allowed`` connector
allowlist from the template's ``flavor.<protocol>.connectors``.

All four are scoped by connector name. The media and order-lookup seams
take the allowlist as an argument; the variant and description seams run inside
Pydantic validators with no template in scope, so they read the session's
connectors from :func:`chat.flavors.active_connectors` (set per chat turn
and per voice call from ``flavor.<protocol>.connectors``). An empty list keeps the zero-config
default: every registered connector self-selects.

The order-lookup seam differs: it returns the lookup of the first connector
the template names, its errors reach the caller, and a template that names
none gets none, since one platform owns a store's orders.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.chat.flavors import active_connectors
from app.core.logger import logger

# (aiohttp_session, product) → gallery media list, or None for "no opinion"
# (the UCP payload's own media stands).
MediaResolverFn = Callable[
    [Any, Dict[str, Any]], Awaitable[Optional[List[Dict[str, str]]]]
]

# well-formed variant dicts → the list to project, or None for "no opinion".
# Lets a connector suppress variants a platform manufactures but that are
# not real choices for the shopper.
VariantNormalizerFn = Callable[[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]

# display text → repaired display text, or None for "no opinion". For
# gateways that damage description text upstream (e.g. shipping it already
# tag-stripped, with the block boundaries lost).
DescriptionRepairFn = Callable[[str], Optional[str]]

# Each chain entry is (connector_name, fn), so a template's
# ``flavor.<protocol>.connectors`` can name the ones it wants.
_MEDIA_RESOLVERS: List[Tuple[str, MediaResolverFn]] = []
_VARIANT_NORMALIZERS: List[Tuple[str, VariantNormalizerFn]] = []
_DESCRIPTION_REPAIRS: List[Tuple[str, DescriptionRepairFn]] = []


def _in_scope(connector: str) -> bool:
    allowed = active_connectors()
    return not allowed or connector in allowed


# Order lookup: ``(context, order_number, phone, email) -> (status_code,
# body)``. ``body`` is the platform's ``{found, orders: [...]}`` answer or
# its error object; a connector raises OrderLookupUnavailable when it cannot
# reach its backend or is not configured for this template.
OrderLookupFn = Callable[..., Awaitable[Tuple[int, Any]]]

_ORDER_LOOKUPS: List[Tuple[str, OrderLookupFn]] = []


class OrderLookupUnavailable(RuntimeError):
    """The connector cannot answer right now (unconfigured, unreachable)."""


def register_order_lookup(connector: str, fn: OrderLookupFn) -> None:
    """Add an order lookup under ``connector``'s name (idempotent for the
    same function object)."""
    if all(existing is not fn for _, existing in _ORDER_LOOKUPS):
        _ORDER_LOOKUPS.append((connector, fn))


def resolve_order_lookup(
    allowed: Optional[Iterable[str]] = None,
) -> Optional[Tuple[str, OrderLookupFn]]:
    """The lookup of the first connector the template names that has one.
    ``None`` when it names none: registration order follows import order,
    so it must never pick the platform that owns a store's orders."""
    lookups = dict(_ORDER_LOOKUPS)
    for connector in allowed or ():
        if connector in lookups:
            return connector, lookups[connector]
    return None


def register_media_resolver(connector: str, fn: MediaResolverFn) -> None:
    """Add a gallery resolver under ``connector``'s name (idempotent for
    the same function object)."""
    if all(existing is not fn for _, existing in _MEDIA_RESOLVERS):
        _MEDIA_RESOLVERS.append((connector, fn))


def register_variant_normalizer(connector: str, fn: VariantNormalizerFn) -> None:
    """Add a variant-list normalizer under ``connector``'s name (idempotent
    for the same function object)."""
    if all(existing is not fn for _, existing in _VARIANT_NORMALIZERS):
        _VARIANT_NORMALIZERS.append((connector, fn))


def register_description_repair(connector: str, fn: DescriptionRepairFn) -> None:
    """Add a description repair under ``connector``'s name (idempotent for
    the same function object)."""
    if all(existing is not fn for _, existing in _DESCRIPTION_REPAIRS):
        _DESCRIPTION_REPAIRS.append((connector, fn))


async def resolve_media(
    aiohttp_session: Any,
    product: Dict[str, Any],
    *,
    allowed: Optional[Iterable[str]] = None,
) -> Optional[List[Dict[str, str]]]:
    """First connector to produce a gallery wins; None when none does.

    ``allowed`` is the template's declared connector list. ``None`` or
    empty keeps the zero-config default (every resolver self-selects);
    naming connectors restricts the chain to those, which is how a gateway
    that merely LOOKS like another platform avoids a dead fetch.
    """
    allowlist = set(allowed) if allowed else None
    for connector, fn in _MEDIA_RESOLVERS:
        if allowlist is not None and connector not in allowlist:
            continue
        try:
            gallery = await fn(aiohttp_session, product)
        except Exception:  # noqa: BLE001 — a connector is never load-bearing
            logger.warning(f"commerce media resolver {connector!r} raised; skipping")
            continue
        # ``is not None`` (not truthiness): an empty list is a deliberate
        # "this platform has no gallery, stop asking", same contract the
        # other two chains use.
        if gallery is not None:
            return gallery
    return None


def normalize_variants(well_formed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """First in-scope connector with an opinion wins; otherwise the list is
    projected as UCP delivered it."""
    for connector, fn in _VARIANT_NORMALIZERS:
        if not _in_scope(connector):
            continue
        try:
            normalized = fn(well_formed)
        except Exception:  # noqa: BLE001
            logger.warning("commerce variant normalizer raised; skipping")
            continue
        if normalized is not None:
            return normalized
    return well_formed


def repair_description(text: str) -> str:
    """Apply every in-scope repair in order; each may decline (None)."""
    for connector, fn in _DESCRIPTION_REPAIRS:
        if not _in_scope(connector):
            continue
        try:
            repaired = fn(text)
        except Exception:  # noqa: BLE001
            logger.warning("commerce description repair raised; skipping")
            continue
        if repaired is not None:
            text = repaired
    return text


__all__ = [
    "MediaResolverFn",
    "OrderLookupFn",
    "OrderLookupUnavailable",
    "register_order_lookup",
    "resolve_order_lookup",
    "VariantNormalizerFn",
    "DescriptionRepairFn",
    "register_media_resolver",
    "register_variant_normalizer",
    "register_description_repair",
    "resolve_media",
    "normalize_variants",
    "repair_description",
]
