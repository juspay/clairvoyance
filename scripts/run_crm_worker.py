"""Boot ONE CRM worker role against a local database for observation.

Faithful to the production path (app/main.py lifespan for a non-api CRM_ROLE):
init the DB pool, then register + start the drain loop for the named role, and
idle until SIGTERM/SIGINT. Skips uvicorn/HTTP and the Daily zygote, which add
nothing to a worker pod's lifecycle.

Usage:  uv run python scripts/run_crm_worker.py <event-worker|walker|dispatcher>
"""

import asyncio
import signal
import sys

from app.core.logger import logger
from app.crm.worker_main import ROLES, start_worker_role, stop_worker_role
from app.database import close_db_pool, init_db_pool


async def _run(role: str) -> None:
    stop = asyncio.Event()

    def _signal(_signum, _frame) -> None:  # noqa: ANN001
        logger.info(f"received shutdown signal for {role}")
        stop.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    await init_db_pool()
    await start_worker_role(role)
    logger.info(f"CRM worker role '{role}' started (idling, awaiting SIGTERM)")

    await stop.wait()

    await stop_worker_role()
    await close_db_pool()
    logger.info(f"CRM worker role '{role}' stopped cleanly")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ROLES:
        raise SystemExit(f"usage: run_crm_worker.py <{'|'.join(ROLES)}>")
    role = sys.argv[1]
    asyncio.run(_run(role))


if __name__ == "__main__":
    main()
