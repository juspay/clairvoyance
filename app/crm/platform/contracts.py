"""platform module — public surface (module rules §1).

The ONLY file other modules may import from app/crm/platform.
"""

from app.crm.platform.suppression import (
    ensure_identities,
    is_suppressed,
    record_suppression,
)

__all__ = ["ensure_identities", "is_suppressed", "record_suppression"]
