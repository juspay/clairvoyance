"""The Inbox's wake-up signal: a Postgres NOTIFY, delivered only when the
transaction that sent it commits — so a wake-up never announces a write that
rolled back."""

import json
from typing import Any, Dict, List, Tuple

#: The one channel every API pod listens on (realtime.py).
INBOX_CHANNEL = "crm_inbox"


def notify_query(payload: Dict[str, Any]) -> Tuple[str, List[Any]]:
    query = "SELECT pg_notify($1, $2)"
    return query, [INBOX_CHANNEL, json.dumps(payload, separators=(",", ":"))]
