"""identity module — public surface (module rules §1).

The ONLY file other modules (and buddy's sync-door callers) may import
from app/crm/identity.
"""

from app.crm.identity.attributes import (
    find_customer_by_attribute,
    mutate_customer_attribute,
    read_customer_attribute,
)
from app.crm.identity.db.accessor import get_customer
from app.crm.identity.facts import assert_facts
from app.crm.identity.resolve import resolve

__all__ = [
    "resolve",
    "assert_facts",
    "get_customer",
    "read_customer_attribute",
    "find_customer_by_attribute",
    "mutate_customer_attribute",
]
