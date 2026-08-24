"""log_decision() — every automated verdict, written as it is made (B4).

Nothing can rebuild this table later: the inputs a decision saw are gone by
the time anyone asks about it. One row per verdict, allow and refuse alike.
"""

import json
from typing import Any, Dict, Optional

from app.crm.permission.db import DbTxn, accessor
from app.crm.permission.schemas import DecisionKind, DecisionRecord


async def log_decision(
    txn: DbTxn,
    *,
    merchant_id: str,
    decision_kind: DecisionKind,
    chosen: Dict[str, Any],
    customer_id: Optional[str] = None,
) -> DecisionRecord:
    """Append one decision inside a transaction the caller already owns.

    There is deliberately no variant that opens its own: a decision that
    commits separately can outlive the thing it authorised, leaving the log
    asserting a send that never happened. ``chosen`` must carry a verdict —
    the table CHECKs it, and `default=str` keeps non-JSON natives (the clocks
    a check compared against) from breaking the dump.
    """
    return await accessor.insert_decision(
        txn,
        merchant_id,
        customer_id,
        decision_kind,
        json.dumps(chosen, default=str, allow_nan=False),
    )
