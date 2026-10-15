"""The Inbox's wake-up: NOTIFY inside the caller's atom (delivered on commit)
or on its own; and the one long-lived LISTEN connection per process."""

import asyncio
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


class Listener:
    """The held LISTEN connection, as its holder may see it: whether it was
    lost, and a probe. A lost connection raises nothing on its own — asyncpg
    drops the listener and releases the slot silently — so the holder must
    ask."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.lost = asyncio.Event()

    def _on_terminated(self, _conn: Any) -> None:
        self.lost.set()

    async def probe(self, timeout: float) -> None:
        """Raise unless the connection answers within ``timeout`` — the only
        way to notice a half-open link, which never reports itself lost."""
        try:
            await asyncio.wait_for(self._conn.fetchval("SELECT 1"), timeout=timeout)
        except Exception:
            self.lost.set()
            raise


@asynccontextmanager
async def listening(
    on_payload: Callable[[Dict[str, Any]], None],
) -> AsyncIterator[Listener]:
    """Hold one pooled connection LISTENing on the Inbox channel for as long
    as the block runs, handing each decoded payload to ``on_payload``. The
    connection is closed when the block ends, never handed back."""

    def _callback(_conn: Any, _pid: int, _channel: str, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if isinstance(payload, dict):
            on_payload(payload)

    async with crm_connection() as conn:
        link = Listener(conn)
        conn.add_termination_listener(link._on_terminated)
        await conn.add_listener(INBOX_CHANNEL, _callback)
        try:
            yield link
        finally:
            # Closed outright, lost or not: handing it back costs an UNLISTEN
            # and the pool's reset, and on a half-open socket no probe caught
            # yet both wait out TCP (minutes) while the hub cannot listen
            # again. The next listen takes a fresh connection (a connection
            # asyncpg already dropped refuses this; fine).
            try:
                conn.terminate()
            except Exception:  # noqa: BLE001 — already gone
                pass
