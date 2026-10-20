"""Seed ONE due enrollment so the walker has real work to claim.

Faithful to the real path: boots the same DB pool the workers use and inserts
through the outreach `insert_enrollment` accessor (the same function enrol()
would call), so the row is born exactly as production creates it. Pins the run
to the existing `demo-interview-avail` plan (merchant, workflow, version 1)
standing on its pure `wait-submission` wait node -- no connector egress, so
walking it is side-effect-free.

With the `walker` worker running (scripts/run_crm_worker.py walker), this run
is claimed on the next poll tick and, because `wait-submission` has no outgoing
edge, exits `completed` -- writing a crm_workflow_step and flipping the row to
exited. Watch both tables in pgweb.

Usage:  uv run python scripts/seed_demo_enrollment.py
Requires the same CRM/DB env vars as the workers (see run_crm_worker.py).
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.core.logger import logger
from app.crm.outreach.db.accessors import enrollment as enrollment_accessor
from app.database import close_db_pool, db_connection, init_db_pool

MERCHANT_ID = "demo-interview-avail"
WORKFLOW_ID = "cbedb83a-09fb-4586-b0de-be399a79bcaa"
WORKFLOW_VERSION = 1
CURRENT_NODE = "wait-submission"  # pure wait, no egress -- safe to walk


async def _seed() -> None:
    await init_db_pool()
    customer_id = str(uuid.uuid4())
    async with db_connection() as conn:
        run = await enrollment_accessor.insert_enrollment(
            conn,
            MERCHANT_ID,
            WORKFLOW_ID,
            WORKFLOW_VERSION,
            customer_id,
            CURRENT_NODE,
            datetime.now(timezone.utc) - timedelta(seconds=1),  # due immediately
            {},
            f"demo-{uuid.uuid4()}",
        )
    logger.info(
        f"seeded due enrollment id={run.id} customer={customer_id} "
        f"node={run.current_node} status={run.status}"
    )
    await close_db_pool()


def main() -> None:
    asyncio.run(_seed())


if __name__ == "__main__":
    main()
