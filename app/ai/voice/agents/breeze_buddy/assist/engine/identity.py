"""Generic tenant-identity hygiene shared by every platform adapter."""

from __future__ import annotations

import re

# Hostname labels: letters/digits/hyphens separated by dots. Deliberately
# mechanical — input hygiene for a public query param, not domain ontology.
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
        raise ValueError("merchant_domain must be a bare domain, e.g. shop.example.com")
    return candidate


__all__ = ["normalize_merchant_domain"]
