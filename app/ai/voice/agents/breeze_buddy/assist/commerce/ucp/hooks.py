"""Connector seams on the UCP layer.

UCP is the protocol this flavor speaks; a *connector* is a platform that
serves it. The protocol layer must stay platform-blind —
so wherever a real gateway's data needs platform knowledge to interpret,
the UCP module calls a hook here and connectors register into it.

Three seams, each with the same contract: the chain is EMPTY by default
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

⚠️ **BEFORE YOU ADD A SECOND CONNECTOR — read this.**

Only the media chain honours that allowlist. The variant and description
chains are name-free (see ``_VARIANT_NORMALIZERS`` /
``_DESCRIPTION_REPAIRS`` below), so **every registered normalizer and
repair runs for every commerce tenant**, whatever their template declared.

With one connector that is harmless: today's two quirks have narrow
triggers (a placeholder variant title, re-flattened description text) and a
gateway from any other platform simply never matches them. With two it is a
bug — the second platform's tenants get the first platform's quirks applied
to their data, silently, with no template opting in.

So connector #2 is the trigger to scope these chains. The reason it wasn't
done up front is that both run from inside Pydantic validators
(``schemas.ProductP`` / ``_html_to_display_text``) where no template is in
scope, so the allowlist cannot simply be passed down as an argument the way
``resolve_media`` takes it. The intended shape is a ``contextvar`` set once
per turn from the resolved ``flavor.<protocol>.connectors`` and read here —
deliberately deferred until there is a real second connector to test it
against, because a contextvar that leaks or goes unset across async task
boundaries fails silently in exactly the direction that hurts (quirks
applied to a tenant that never asked for them).

Until then: keep new quirks narrow-triggered and platform-specific enough
that a foreign payload cannot match them.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

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

# Media resolvers are (connector_name, fn) so a template can name the ones
# it wants. The other two chains are name-free — which is exactly what
# connector #2 has to change; see the warning in the module docstring
# before adding one.
_MEDIA_RESOLVERS: List[Tuple[str, MediaResolverFn]] = []
_VARIANT_NORMALIZERS: List[VariantNormalizerFn] = []
_DESCRIPTION_REPAIRS: List[DescriptionRepairFn] = []


def register_media_resolver(connector: str, fn: MediaResolverFn) -> None:
    """Add a gallery resolver under ``connector``'s name (idempotent for
    the same function object)."""
    if all(existing is not fn for _, existing in _MEDIA_RESOLVERS):
        _MEDIA_RESOLVERS.append((connector, fn))


def register_variant_normalizer(fn: VariantNormalizerFn) -> None:
    """Add a variant-list normalizer (idempotent for the same object).

    ⚠️ Takes no connector name, so this normalizer runs for EVERY commerce
    tenant regardless of their ``flavor.<protocol>.connectors``. Fine while
    one connector exists; scope this chain when adding a second (module
    docstring).
    """
    if all(existing is not fn for existing in _VARIANT_NORMALIZERS):
        _VARIANT_NORMALIZERS.append(fn)


def register_description_repair(fn: DescriptionRepairFn) -> None:
    """Add a description repair (idempotent for the same object).

    ⚠️ Same caveat as :func:`register_variant_normalizer`: unscoped, so it
    applies to every commerce tenant until connector #2 forces the chains
    to carry connector names (module docstring).
    """
    if all(existing is not fn for existing in _DESCRIPTION_REPAIRS):
        _DESCRIPTION_REPAIRS.append(fn)


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
    """First connector with an opinion wins; otherwise the list is
    projected as UCP delivered it.

    No allowlist parameter: called from a Pydantic validator with no
    template in scope. That is the constraint connector #2 has to solve —
    see the module docstring.
    """
    for fn in _VARIANT_NORMALIZERS:
        try:
            normalized = fn(well_formed)
        except Exception:  # noqa: BLE001
            logger.warning("commerce variant normalizer raised; skipping")
            continue
        if normalized is not None:
            return normalized
    return well_formed


def repair_description(text: str) -> str:
    """Apply every registered repair in order; each may decline (None).

    Unscoped for the same reason as :func:`normalize_variants`.
    """
    for fn in _DESCRIPTION_REPAIRS:
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
    "VariantNormalizerFn",
    "DescriptionRepairFn",
    "register_media_resolver",
    "register_variant_normalizer",
    "register_description_repair",
    "resolve_media",
    "normalize_variants",
    "repair_description",
]
