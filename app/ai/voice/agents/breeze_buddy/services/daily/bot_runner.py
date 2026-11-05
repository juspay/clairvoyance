"""Standalone entrypoint: run ONE Daily bot in its own OS process.

Spawned per call by ``services/daily/daily.py::_launch_daily_bot``:

    python -m app.ai.voice.agents.breeze_buddy.services.daily.bot_runner

The launch payload arrives as one ``BotLaunchPayload`` JSON object on
**stdin** (never argv — the Daily bot token must not be visible in ``ps``
output). The child runs the exact same ``daily_bot`` code path as the
in-process launch; all coordination with the API process (``/voice/end``,
channel flips, approvals, the per-session lock) goes through Postgres /
Redis / the Daily room, so nothing changes functionally by crossing the
process boundary. See ``_launch_daily_bot`` for the full rationale.
"""

# Load .env before the app imports below so a manually launched child
# (``python -m ... < payload.json``) resolves static config from the on-disk
# .env exactly like ``run.py`` does. This works because the package chain
# ``-m`` imports first is side-effect free (``services/daily/__init__.py`` is
# intentionally empty) — nothing reads the environment before this line. In
# deployments the parent passes its full environment and this is a no-op.
from dotenv import load_dotenv

load_dotenv()

import asyncio  # noqa: E402
import sys  # noqa: E402

from pipecat.runner.types import DailyRunnerArguments  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.ai.voice.agents.breeze_buddy.agent import daily_bot  # noqa: E402
from app.ai.voice.agents.breeze_buddy.services.daily.daily import (  # noqa: E402
    daily_completion_function,
)
from app.ai.voice.agents.breeze_buddy.services.daily.launch_payload import (  # noqa: E402
    BotLaunchPayload,
)
from app.core.config.static import (  # noqa: E402
    BB_VOICE_BOT_DB_MAX_OVERFLOW,
    BB_VOICE_BOT_DB_POOL_SIZE,
)
from app.core.logger import logger  # noqa: E402
from app.core.transport.http_client import create_aiohttp_session  # noqa: E402
from app.database import close_db_pool, init_db_pool  # noqa: E402
from app.services.redis import close_redis_connections  # noqa: E402

# A stuck teardown must not keep the child process (and its Postgres/Redis
# connections) alive after the call ended — the process exits either way and
# the OS reclaims the sockets.
_TEARDOWN_TIMEOUT_SECS = 10.0


def _parse_payload(raw: str) -> DailyRunnerArguments:
    """Parse the stdin JSON payload into DailyRunnerArguments.

    Raises:
        ValueError: If the payload is not valid JSON or misses required fields.
    """
    try:
        payload = BotLaunchPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"bot_runner payload invalid: {exc}") from exc
    return DailyRunnerArguments(
        room_url=payload.room_url, token=payload.token, body=payload.body
    )


async def _amain(runner_args: DailyRunnerArguments) -> None:
    """Run one bot to completion, then release the child's pools."""
    # `or {}` narrows pipecat's `body: Any | None` — _parse_payload already
    # guarantees a dict with a truthy lead_id.
    lead_id = (runner_args.body or {})["lead_id"]
    logger.info(f"[bot_runner] starting Daily bot subprocess for lead {lead_id}")
    # Size the per-call pool explicitly BEFORE anything touches the DB —
    # lazy first-use init would open the API pod's full default pool
    # (min_size=POSTGRES_POOL_SIZE) in every child.
    await init_db_pool(
        min_size=BB_VOICE_BOT_DB_POOL_SIZE,
        max_size=BB_VOICE_BOT_DB_POOL_SIZE + BB_VOICE_BOT_DB_MAX_OVERFLOW,
    )
    try:
        # daily_bot's own finally closes the aiohttp session.
        await daily_bot(
            runner_args, daily_completion_function, create_aiohttp_session()
        )
    finally:
        # Best-effort, time-bounded: pool.close() waits indefinitely for
        # acquired connections, and a straggler must not pin a finished
        # child process alive.
        for closer in (close_db_pool, close_redis_connections):
            try:
                await asyncio.wait_for(closer(), timeout=_TEARDOWN_TIMEOUT_SECS)
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                logger.warning(f"[bot_runner] {closer.__name__} failed: {exc!r}")
        logger.info(f"[bot_runner] Daily bot subprocess done for lead {lead_id}")


def main() -> None:
    """Read the launch payload from stdin and run the bot."""
    raw = sys.stdin.read()
    try:
        runner_args = _parse_payload(raw)
    except ValueError as exc:
        logger.error(f"[bot_runner] invalid launch payload: {exc}")
        sys.exit(2)
    asyncio.run(_amain(runner_args))


if __name__ == "__main__":
    main()
