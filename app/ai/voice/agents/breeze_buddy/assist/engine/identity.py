"""Generic tenant-identity hygiene shared by every platform adapter."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional

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


def _valid_domain(domain: Any) -> Optional[str]:
    try:
        return normalize_merchant_domain(str(domain or ""))
    except ValueError:
        return None


def verify_shop_url(
    template_vars: Optional[Mapping[str, Any]],
    own_domains: Iterable[Any],
    saved_shop_url: Any = None,
) -> Dict[str, Any]:
    """``template_vars`` with ``shop_url`` kept only when it names this store.

    ``shop_url`` picks the host the commerce tools call, and a session
    payload comes from an anonymous client. A value is kept, normalized to a
    bare host, when it matches one of ``own_domains`` (``www.``-insensitive);
    otherwise, or when none is sent, it is set to ``saved_shop_url``. A
    missing key is not enough: session create fills a missing schema field
    with ``""``, which would then override the template's saved ``shop_url``
    on every turn.
    """
    out = dict(template_vars or {})
    sent = _valid_domain(out.pop("shop_url", None))
    own = {
        host.removeprefix("www.") for host in map(_valid_domain, own_domains) if host
    }
    if sent and sent.removeprefix("www.") in own:
        out["shop_url"] = sent
    else:
        saved = _valid_domain(saved_shop_url)
        if saved:
            out["shop_url"] = saved
    return out


__all__ = ["normalize_merchant_domain", "verify_shop_url"]
