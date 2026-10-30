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


def _host_or_none(domain: Any) -> Optional[str]:
    try:
        return normalize_merchant_domain(str(domain or ""))
    except ValueError:
        return None


def _without_www(host: str) -> str:
    return host[len("www.") :] if host.startswith("www.") else host


def own_shop_url(
    template_vars: Optional[Mapping[str, Any]],
    own_domains: Iterable[Any],
    saved_shop_url: Any = None,
) -> Dict[str, Any]:
    """``template_vars`` with ``shop_url`` kept only when it names this store.

    ``shop_url`` picks the host the commerce tools call, and a session
    payload comes from an anonymous client. A value is kept, normalized to a
    bare host, when it matches one of ``own_domains`` (``www.``-insensitive);
    otherwise it is replaced by ``saved_shop_url``. Dropping the key is not
    enough: session create fills a missing schema field with ``""``, which
    would then override the template's saved ``shop_url`` on every turn.
    """
    out = dict(template_vars or {})
    if "shop_url" not in out:
        return out
    sent = _host_or_none(out.pop("shop_url"))
    own = {_without_www(host) for host in map(_host_or_none, own_domains) if host}
    if sent and _without_www(sent) in own:
        out["shop_url"] = sent
    else:
        saved = _host_or_none(saved_shop_url)
        if saved:
            out["shop_url"] = saved
    return out


__all__ = ["normalize_merchant_domain", "own_shop_url"]
