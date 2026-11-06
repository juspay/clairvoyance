"""Per-customer state kept by OTHER modules under one key of
``crm_customer.attributes`` (agentic keeps its onboarding attempts under
``"agents"``). Identity owns the row, so identity owns the atom: a caller
hands in a pure function over the current value and gets the result back;
the read + write happen under the row lock, so two concurrent patches (a
poll tick and the SDK callback) never lose each other's fields.

Values under a key are opaque JSON to identity — it never inspects them.
"""

import json
from typing import Any, Callable, Optional, Tuple

from app.crm.identity.db import accessor
from app.crm.shared.db import DbTxn, atomically

# (customer_id, merchant_id, value-under-key)
AttributeHit = Tuple[str, str, Any]

# (new value to store, result handed back to the caller)
Mutation = Callable[[Any], Tuple[Any, Any]]


def _attributes(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


async def read_customer_attribute(customer_id: str, key: str) -> Optional[AttributeHit]:
    """The value under ``key`` for one customer; None when the customer does
    not exist. A missing key reads as None inside the hit."""
    row = await accessor.fetch_attributes_by_id(customer_id)
    if row is None:
        return None
    return str(row["id"]), row["merchant_id"], _attributes(row["attributes"]).get(key)


async def find_customer_by_attribute(key: str, fragment: Any) -> Optional[AttributeHit]:
    """The customer whose value under ``key`` contains ``fragment`` (jsonb
    containment). For a list value pass ``[{...}]`` to mean "an element
    with these fields"."""
    row = await accessor.find_customer_by_attribute(key, json.dumps(fragment))
    if row is None:
        return None
    return str(row["id"]), row["merchant_id"], _attributes(row["attributes"]).get(key)


async def mutate_customer_attribute(
    customer_id: str,
    key: str,
    mutation: Mutation,
    *,
    merchant_id: Optional[str] = None,
) -> Optional[Tuple[str, Any]]:
    """Replace the value under ``key`` with ``mutation(current)[0]`` and
    return ``(merchant_id, mutation(current)[1])``; None when the customer
    does not exist (nothing written). ``mutation`` must be pure.

    ``merchant_id``, when given, is the tenant the caller believes owns the
    customer: a customer of another merchant is refused under the lock,
    BEFORE anything is written (also None). Callers that only hold a
    customer id (webhook and poll paths, whose refs embed it) omit it."""
    return await atomically(
        _mutate_customer_attribute_in_txn, customer_id, key, mutation, merchant_id
    )


async def _mutate_customer_attribute_in_txn(
    txn: DbTxn,
    customer_id: str,
    key: str,
    mutation: Mutation,
    expected_merchant: Optional[str],
) -> Optional[Tuple[str, Any]]:
    """ATOMIC: read-under-lock + write of one attributes key — a concurrent
    writer of the same customer (another key, or the same one) must never
    overwrite this change with a stale copy of the document."""
    row = await accessor.fetch_attributes_for_update_by_id(txn, customer_id)
    if row is None:
        return None
    merchant_id = row["merchant_id"]
    if expected_merchant is not None and merchant_id != expected_merchant:
        # tenant-pinned: the door never lets one merchant write onto
        # another merchant's customer, whatever id the caller was handed
        return None
    attributes = _attributes(row["attributes"])
    new_value, result = mutation(attributes.get(key))
    attributes[key] = new_value
    await accessor.update_attributes(
        txn, merchant_id, customer_id, json.dumps(attributes, default=str), {}
    )
    return merchant_id, result
