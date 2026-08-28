"""permission's db layer door — the ONLY surface logic imports for db-world
things (module rules §1): atomically(), the opaque handle, domain-named
errors. asyncpg exists only in shared/db and db/ packages.
"""

import asyncpg

from app.crm.shared.db import DbTxn, UniqueViolation, atomically

# The tenancy FK: (merchant_id, customer_id) must name a real pair.
TenancyViolation = asyncpg.ForeignKeyViolationError

__all__ = ["DbTxn", "UniqueViolation", "TenancyViolation", "atomically"]
