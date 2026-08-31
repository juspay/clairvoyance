"""outreach's db layer door — the ONLY surface logic imports for db-world
things (module rules §1): atomically() (the only boundary door), the
opaque handle, domain-named errors. A logic file touches a handle in
exactly ONE place: the txn parameter of an _in_txn body; accessors
self-scope everything else. asyncpg exists only in shared/db and db/.
"""

from app.crm.shared.db import DbTxn, UniqueViolation, atomically

__all__ = ["DbTxn", "UniqueViolation", "atomically"]
