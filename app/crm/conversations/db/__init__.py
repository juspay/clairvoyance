"""The db door — the only db-world names conversations' logic may import."""

from app.crm.shared.db import DbTxn, UniqueViolation, atomically

__all__ = ["DbTxn", "UniqueViolation", "atomically"]
