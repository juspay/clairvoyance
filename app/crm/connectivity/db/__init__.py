"""The db door — the only db-world names logic may import.

Nothing here opens a transaction today (every statement is a single UPDATE);
the door still re-exports these so the first one has somewhere to import from.
"""

from app.crm.shared.db import DbTxn, UniqueViolation, atomically

__all__ = ["DbTxn", "UniqueViolation", "atomically"]
