"""identity module — public surface (module rules §1).

The ONLY file other modules (and buddy's sync-door callers) may import
from app/crm/identity.
"""

from app.crm.identity.attributes import (
    find_customer_by_attribute,
    mutate_customer_attribute,
    read_customer_attribute,
)
from app.crm.identity.db.accessor import get_customer, get_customers
from app.crm.identity.facts import assert_facts, customer_facts
from app.crm.identity.person import merged_pairs, person_records
from app.crm.identity.resolve import resolve
from app.crm.identity.schemas import CustomerFacts

# The phone handle's one normal form — the key resolve() probes on. Buddy's
# per-customer call limit (ADR 0025) hashes the dialled number in this form,
# so "+91 98…" and "98…" are one customer to it, exactly as to identity.
from app.crm.shared.normalize import normalize_phone

__all__ = [
    "resolve",
    "assert_facts",
    "customer_facts",
    "CustomerFacts",
    "get_customer",
    # A broadcast's admission page reads its customers in ONE query.
    "get_customers",
    "read_customer_attribute",
    "find_customer_by_attribute",
    "mutate_customer_attribute",
    "normalize_phone",
    # One person across a merge (canon: reads across a pair) — every
    # per-customer read of runs, letters and segments asks through these.
    "person_records",
    "merged_pairs",
]
