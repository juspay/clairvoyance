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

import re
from typing import Literal, Tuple

AssistHostApp = Literal["buddy-assist", "breeze-buddy"]

_ASSIST_MERCHANT_PREFIX = "assist-"

# Hostname labels: letters/digits/hyphens separated by dots. Deliberately
# mechanical — this is input hygiene for a public query param, not domain
# ontology.
_MERCHANT_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)


def normalize_merchant_domain(merchant_domain: str) -> str:
    """Lowercased bare hostname, or ``ValueError`` for anything else."""
    candidate = (merchant_domain or "").strip().lower().rstrip(".")
    if candidate.startswith("https://"):
        candidate = candidate[len("https://") :]
    candidate = candidate.split("/", 1)[0]
    if not _MERCHANT_DOMAIN_RE.match(candidate):
        raise ValueError(
            "merchant_domain must be a bare domain, e.g. acme.myshopify.com"
        )
    return candidate


def assist_tenant(host_app: AssistHostApp, merchant_domain: str) -> Tuple[str, str]:
    """(reseller_id, merchant_id) for a host app's Assist tenant."""
    if host_app == "breeze-buddy":
        return "BB_SHOPIFY", merchant_domain
    return "BB_ASSIST", f"{_ASSIST_MERCHANT_PREFIX}{merchant_domain}"


def assist_tenant_candidates(merchant_domain: str) -> Tuple[Tuple[str, str], ...]:
    """Lookup order for domain-only resolution: standalone app first."""
    return (
        assist_tenant("buddy-assist", merchant_domain),
        assist_tenant("breeze-buddy", merchant_domain),
    )


__all__ = [
    "AssistHostApp",
    "assist_tenant",
    "assist_tenant_candidates",
    "normalize_merchant_domain",
]
