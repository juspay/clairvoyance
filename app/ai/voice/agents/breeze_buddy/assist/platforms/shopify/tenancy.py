"""Assist tenancy convention: how a merchant's storefront domain maps to a tenant.

The RESELLER is decided by which host Shopify app installed Assist:

- ``breeze-buddy`` (the voice app's Assist tab) → reseller ``BB_SHOPIFY``
  with the PLAIN domain as merchant id — the same merchant row as the
  voice registration. Every pre-existing live Assist tenant has this
  shape.
- ``buddy-assist`` (the standalone Assist app) → reseller ``BB_ASSIST``
  with an ``assist-`` prefixed merchant id. The prefix is required:
  ``merchants.merchant_identifier`` is a GLOBAL primary key, so a plain
  domain under BB_ASSIST would collide with the shop's voice row.

The storefront loader knows only the domain, not the host app, so
domain-only lookups (the public storefront-config resolve) probe both
namespaces via ``assist_tenant_candidates`` — the standalone app's
tenant wins when both exist.
"""

from __future__ import annotations

from typing import Literal, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.identity import (
    normalize_merchant_domain,
)

AssistHostApp = Literal["buddy-assist", "breeze-buddy"]

_ASSIST_MERCHANT_PREFIX = "assist-"


def assist_tenant(host_app: AssistHostApp, merchant_domain: str) -> Tuple[str, str]:
    """(reseller_id, merchant_id) for a host app's Assist tenant."""
    if host_app == "breeze-buddy":
        return "BB_SHOPIFY", merchant_domain
    return "BB_ASSIST", f"{_ASSIST_MERCHANT_PREFIX}{merchant_domain}"


def assist_merchant_domain(reseller_id: str, merchant_id: str) -> str:
    """The storefront domain behind a tenant: the inverse of assist_tenant.

    Only a BB_ASSIST id carries the prefix; a plain domain may itself start
    with ``assist-``, so it is never stripped from other resellers.
    """
    if reseller_id == "BB_ASSIST":
        return merchant_id.removeprefix(_ASSIST_MERCHANT_PREFIX)
    return merchant_id


def assist_tenant_candidates(merchant_domain: str) -> Tuple[Tuple[str, str], ...]:
    """Lookup order for domain-only resolution: standalone app first."""
    return (
        assist_tenant("buddy-assist", merchant_domain),
        assist_tenant("breeze-buddy", merchant_domain),
    )


__all__ = [
    "AssistHostApp",
    "assist_merchant_domain",
    "assist_tenant",
    "assist_tenant_candidates",
    "normalize_merchant_domain",
]
