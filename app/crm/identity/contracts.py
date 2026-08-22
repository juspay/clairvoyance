"""identity module — public surface (module rules §1).

The ONLY file other modules (and buddy's sync-door callers) may import
from app/crm/identity.
"""

from app.crm.identity.facts import assert_facts
from app.crm.identity.resolve import resolve

__all__ = ["resolve", "assert_facts"]
