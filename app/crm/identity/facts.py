"""assert_facts() — which of the conflicting claims we believe (A7).

BUSINESS LOGIC ONLY — DB mechanics live in accessor.py. Every profile
claim (name, locale, timezone) is appended into the customer's
``attributes`` assertion history with its evidence class and confidence;
the winner per attribute materializes into the plain column the UI reads.
The trust ladder is fixed:

    declared > observed > imported > inferred

An inferred claim NEVER materializes and its confidence is capped at 0.5
(canon T05) — a guess may steer, never decide. History is master data:
never evicted, never trimmed, and appended to only on drift (_is_drift).
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.core.logger.context import update_log_context
from app.crm.identity.db import DbTxn, accessor, atomically

EVIDENCE_RANK = {"declared": 3, "observed": 2, "imported": 1, "inferred": 0}

# Default confidence per evidence class; explicit confidence may lower
# but inferred can never exceed the canon's 0.5 cap.
DEFAULT_CONFIDENCE = {
    "declared": 1.0,
    "observed": 0.9,
    "imported": 0.7,
    "inferred": 0.4,
}
INFERRED_CONFIDENCE_CAP = 0.5

# attribute name in facts{} -> materialized column on crm_customer
MATERIALIZED_COLUMNS = {
    "name": "display_name",
    "locale": "primary_locale",
    "timezone": "timezone",
}


def claim_confidence(evidence: str, confidence: Optional[float]) -> float:
    """Resolve a claim's confidence: default by class, explicit allowed,
    inferred hard-capped at 0.5 (the canon CHECK, enforced here until the
    jsonb CHECK lands with the assertion-shape freeze)."""
    value = DEFAULT_CONFIDENCE[evidence] if confidence is None else confidence
    if evidence == "inferred":
        value = min(value, INFERRED_CONFIDENCE_CAP)
    return max(0.0, min(1.0, value))


def _winner(claims: list) -> Dict[str, Any]:
    """Highest evidence wins; ties break to the newest claim."""
    return max(claims, key=lambda c: (EVIDENCE_RANK.get(c["e"], -1), c["at"]))


def _is_drift(claims: list, value: Any, evidence: str, source: str) -> bool:
    """The history records evidence, not traffic: a claim identical to the
    latest one (same value, class and producer) carries nothing new. Any
    difference is real drift and is always appended."""
    if not claims:
        return True
    latest = claims[-1]
    return (
        latest.get("v") != value
        or latest.get("e") != evidence
        or latest.get("src") != source
    )


async def assert_facts(
    merchant_id: str,
    customer_id: str,
    facts: Dict[str, Any],
    evidence: str,
    source: str,
    confidence: Optional[float] = None,
) -> None:
    """Append claims into the assertion history and materialize winners."""
    if evidence not in EVIDENCE_RANK:
        raise ValueError(f"unknown evidence class: {evidence}")
    facts = {k: v for k, v in (facts or {}).items() if v is not None and v != ""}
    if not facts:
        return

    now = datetime.now(timezone.utc).isoformat()
    k = claim_confidence(evidence, confidence)
    await atomically(
        _assert_facts_in_txn, merchant_id, customer_id, facts, evidence, source, now, k
    )


async def _assert_facts_in_txn(
    txn: DbTxn,
    merchant_id: str,
    customer_id: str,
    facts: Dict[str, Any],
    evidence: str,
    source: str,
    now: str,
    k: float,
) -> None:
    """ATOMIC: history append + winner materialization — the materialized
    columns must never drift from the assertion history (canon T05)."""
    row = await accessor.fetch_attributes_for_update(txn, merchant_id, customer_id)
    if row is None:
        update_log_context(customer_id=customer_id, merchant_id=merchant_id)
        logger.error("assert_facts: customer not found for merchant")
        return
    attributes = row["attributes"]
    if isinstance(attributes, str):
        attributes = json.loads(attributes)

    materialized: Dict[str, Any] = {}
    for attribute, value in facts.items():
        claims = attributes.setdefault(attribute, [])
        if not _is_drift(claims, value, evidence, source):
            continue
        claims.append({"v": value, "e": evidence, "k": k, "src": source, "at": now})
        winner = _winner(claims)
        column = MATERIALIZED_COLUMNS.get(attribute)
        if column and winner["e"] != "inferred":
            materialized[column] = winner["v"]

    await accessor.update_attributes(
        txn, merchant_id, customer_id, json.dumps(attributes), materialized
    )
