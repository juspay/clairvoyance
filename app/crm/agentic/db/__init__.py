"""agentic's db layer door — the ONLY surface logic imports for db-world
things (module rules §1). Accessors self-scope single statements; logic
never holds a handle here (no multi-statement atom exists yet).
"""

from app.crm.shared.db import DbTxn, UniqueViolation, atomically

__all__ = ["DbTxn", "UniqueViolation", "atomically"]
