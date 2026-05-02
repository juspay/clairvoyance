"""Blueprint graph checkpointer — Postgres when available, memory as fallback.

LangGraph persists the full graph state (messages, draft, validation
issues, completed groups, …) via its checkpointer between ``ainvoke``
calls. In production we need that state to survive pod restarts and
be visible across replicas; that means Postgres.

LangGraph only ships a psycopg3-based ``AsyncPostgresSaver``. The rest
of this app uses asyncpg, so we run a small dedicated psycopg connection
pool purely for the checkpointer. This is a deliberate second driver —
the Blueprint checkpoint tables are self-contained and don't share
transactions with application queries, so there's no consistency cost
to mixing drivers.

Lifecycle:

* ``init_checkpointer()`` — called once from the FastAPI lifespan on
  startup. Opens the psycopg pool, creates the LangGraph checkpoint
  tables (idempotent), stores the saver in a module-level global.
* ``get_checkpointer()`` — called lazily from ``handlers.py`` on each
  request. Returns the Postgres-backed saver if ``init`` succeeded,
  otherwise a process-local ``MemorySaver`` fallback so dev without
  Postgres still works.
* ``close_checkpointer()`` — called from the lifespan on shutdown.
"""

from __future__ import annotations

from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config.static import (
    POSTGRES_DB,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
)
from app.core.logger import logger
from app.services.aws.kms import decrypt_kms

# Module-level singletons — initialized in ``init_checkpointer``,
# read by ``get_checkpointer``, closed in ``close_checkpointer``.
_pool: Optional[AsyncConnectionPool] = None
_postgres_saver: Optional[AsyncPostgresSaver] = None
_memory_fallback = MemorySaver()


async def init_checkpointer() -> None:
    """Stand up the Postgres-backed checkpointer.

    Safe to call when Postgres env vars are missing — logs a warning and
    leaves the memory fallback in place. Idempotent: re-entry is a no-op.
    """
    global _pool, _postgres_saver

    if _postgres_saver is not None:
        return

    required = (
        POSTGRES_USER,
        POSTGRES_PASSWORD,
        POSTGRES_HOST,
        POSTGRES_PORT,
        POSTGRES_DB,
    )
    if not all(required):
        logger.warning(
            "Blueprint checkpointer: Postgres env vars missing — falling back "
            "to in-process MemorySaver. Graph state will not survive restarts."
        )
        return

    password = await decrypt_kms(POSTGRES_PASSWORD)
    if password is None:
        logger.warning(
            "Blueprint checkpointer: KMS decrypt failed — using raw password."
        )
        password = POSTGRES_PASSWORD

    conninfo = (
        f"host={POSTGRES_HOST} port={POSTGRES_PORT} "
        f"dbname={POSTGRES_DB} user={POSTGRES_USER} password={password}"
    )

    try:
        # ``autocommit=True`` + ``prepare_threshold=0`` are required for
        # the LangGraph checkpointer; it manages its own transactions and
        # creates DDL during ``setup()``. ``row_factory=dict_row`` is
        # what ``AsyncPostgresSaver`` types against.
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=5,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
        )
        await pool.open()
        # ``AsyncConnectionPool`` is generic over the connection's row type,
        # which we set via kwargs at runtime — pyrefly can't see that, so
        # the cast is needed to satisfy ``AsyncPostgresSaver``'s signature.
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
    except Exception as exc:
        logger.error(
            "Blueprint checkpointer: Postgres init failed, "
            f"falling back to MemorySaver: {exc}",
            exc_info=True,
        )
        return

    _pool = pool
    _postgres_saver = saver
    logger.info("Blueprint checkpointer: Postgres-backed saver ready.")


async def close_checkpointer() -> None:
    """Tear down the Postgres pool on shutdown."""
    global _pool, _postgres_saver
    _postgres_saver = None
    if _pool is not None:
        try:
            await _pool.close()
        except Exception as exc:
            logger.warning(f"Blueprint checkpointer: pool close failed: {exc}")
        _pool = None


def get_checkpointer() -> BaseCheckpointSaver:
    """Return the active checkpointer — Postgres if initialized, else memory.

    Called by ``handlers.py`` per request.
    """
    return _postgres_saver or _memory_fallback


__all__ = ["close_checkpointer", "get_checkpointer", "init_checkpointer"]
