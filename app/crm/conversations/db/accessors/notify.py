"""The Inbox's wake-up: NOTIFY inside the caller's atom (delivered on commit)
or on its own; and the one long-lived LISTEN connection per process."""

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Optional

from app.crm.conversations.db.queries.notify import INBOX_CHANNEL, notify_query
from app.crm.shared.db import DbTxn, crm_connection


async def notify(payload: Dict[str, Any], txn: Optional[DbTxn] = None) -> None:
    query, values = notify_query(payload)
    if txn is not None:
        await txn.execute(query, *values)
        return
    async with crm_connection() as conn:
        await conn.execute(query, *values)


@asynccontextmanager
async def listening(
    on_payload: Callable[[Dict[str, Any]], None],
) -> AsyncIterator[None]:
    """Hold one pooled connection LISTENing on the Inbox channel for as long
    as the block runs, handing each decoded payload to ``on_payload``. The
    pool's reset runs UNLISTEN when the connection goes back."""

    def _callback(_conn: Any, _pid: int, _channel: str, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if isinstance(payload, dict):
            on_payload(payload)

    async with crm_connection() as conn:
        await conn.add_listener(INBOX_CHANNEL, _callback)
        try:
            yield
        finally:
            await conn.remove_listener(INBOX_CHANNEL, _callback)
