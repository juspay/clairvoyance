"""platform_identity contracts logic (B2) — the handles book.

BUSINESS LOGIC ONLY — DB mechanics live in accessor.py. Three contracts,
re-exported through contracts.py, the only sanctioned access to the table:

- ``ensure_identities`` — registry upsert, called by identity's resolve();
  best-effort: registry upkeep must never break customer creation.
- ``is_suppressed`` — the gate's check 2. FAIL CLOSED: any error means
  blocked (module rules law 6).
- ``record_suppression`` — the single suppression writer; the blacklist
  backfill (B3) and platform-wide STOP call this, nobody writes directly.

Every value is normalized before it touches the table (and the 048 CHECKs
refuse what slips through): a suppression stored unnormalized is a gate
probe MISS, which contacts someone who said stop.

The platform layer only says yes or no — no profile data, ever.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.crm.platform.db import DbTxn, accessor, atomically
from app.crm.shared.normalize import normalize_email, normalize_phone

# Identity-handle name -> platform kind. Handles with no platform kind
# (igsid, shopify ids) are per-merchant facts and never enter the book.
HANDLE_KINDS = {"phone": "phone", "email": "email"}


def _normalize_pair(kind: str, value: str) -> Optional[str]:
    if kind == "phone":
        return normalize_phone(value)
    if kind == "email":
        return normalize_email(value)
    return (value or "").strip() or None


def _platform_pairs(handles: Dict[str, str]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for name, value in handles.items():
        if name not in HANDLE_KINDS or not value:
            continue
        normalized = _normalize_pair(HANDLE_KINDS[name], value)
        if normalized:
            pairs.append((HANDLE_KINDS[name], normalized))
    return pairs


def entry_is_live(entry: Dict[str, Any]) -> bool:
    """A suppression entry is live when it has no `until` or its `until`
    is in the future — expiry is a predicate, never a stored status.
    Mirrors the 048 trigger exactly; the trigger is the authority."""
    until = entry.get("until")
    if not until:
        return True
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True  # unparseable expiry = treat as live (fail closed)


async def ensure_identities(handles: Dict[str, str]) -> None:
    """Upsert one registry row per platform-kind handle. Never raises."""
    pairs = _platform_pairs(handles)
    if not pairs:
        return
    try:
        await accessor.ensure_identities(pairs)  # independent idempotent
    except Exception as e:  # upserts — no shared fate, no txn
        logger.error(f"platform_identity registry upsert failed: {e}")


async def is_suppressed(handles: Dict[str, str]) -> bool:
    """True if ANY handle carries a live suppression. FAIL CLOSED:
    a DB error returns True (blocked) — a missing answer means NO send."""
    pairs = _platform_pairs(handles)
    if not pairs:
        return False
    try:
        row = await accessor.probe_suppression_pairs(pairs)
        return bool(row["suppressed"]) if row and row["suppressed"] else False
    except Exception as e:
        logger.error(f"suppression probe failed, failing CLOSED: {e}")
        return True


async def record_suppression(
    kind: str,
    value: str,
    channel: str,
    reason: str,
    source: str,
    evidence_ref: Optional[str] = None,
    until: Optional[datetime] = None,
) -> None:
    """Append to the suppression log and rewrite the resolved map in one
    transaction; the 048 trigger derives is_suppressed (liveness-aware).
    channel '*' = every channel. ``until`` None = permanent; set = the
    suppression lapses when the predicate says so (expiry-as-predicate)."""
    normalized = _normalize_pair(kind, value)
    if normalized is None:
        raise ValueError(f"unnormalizable {kind} value for suppression: {value!r}")
    entry = _build_entry(channel, reason, source, evidence_ref, until)
    await atomically(_record_suppression_in_txn, kind, normalized, channel, entry)


async def _record_suppression_in_txn(
    txn: DbTxn, kind: str, normalized: str, channel: str, entry: Dict[str, Any]
) -> None:
    """ATOMIC: log append + resolved-map rewrite — the two stores must
    never disagree (canon T07/T08: the gate reads one, auditors the other)."""
    await accessor.ensure_identity(txn, kind, normalized)
    row = await accessor.fetch_identity_for_update(txn, kind, normalized)
    if row is None:
        raise RuntimeError(f"platform_identity row missing for {kind}")
    suppressions, log = _merge_entry(row, channel, entry)  # pure DECIDE
    await accessor.update_suppression(
        txn, str(row["id"]), json.dumps(suppressions), json.dumps(log)
    )


def _build_entry(  # noqa: PLR0913 — entry fields are the schema
    channel: str,
    reason: str,
    source: str,
    evidence_ref: Optional[str],
    until: Optional[datetime],
) -> Dict[str, Any]:
    return {
        "channel": channel,
        "reason": reason,
        "source": source,
        "evidence_ref": evidence_ref,
        "from": datetime.now(timezone.utc).isoformat(),
        "until": until.isoformat() if until else None,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _merge_entry(
    row: Any, channel: str, entry: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Pure: fold one entry into the resolved map + the append-only log."""
    suppressions = _load_dict(row["suppressions"])
    log = _load_list(row["suppression_log"])
    suppressions[channel] = {k: v for k, v in entry.items() if k != "channel"}
    log.append(entry)
    return suppressions, log


def _load_dict(value: Any) -> Dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def _load_list(value: Any) -> List[Dict[str, Any]]:
    return json.loads(value) if isinstance(value, str) else list(value)
