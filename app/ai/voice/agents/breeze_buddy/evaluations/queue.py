"""Producer for the internal evaluation lead queue.

When a call ends, the lead_id is pushed onto a Redis list so the
evaluation worker (later phase) can drain it, read the full lead
(transcription, payload, outcome, reseller/merchant, tags, ...) from our
own DB, and run LLM-as-judge evaluations. ONLY the lead_id is stored here
— every other field is read from the lead record by the worker, so there
is no Langfuse fetch and no trace-export race. (We still *send* traces to
Langfuse for the existing ScoreMonitor and human debugging; we just don't
read them back for evaluation.)

This module owns only the producer half of the key layout. The consumer
half (inflight ZSET + atomic pop + self-sweep) ships with the worker in a
later phase and imports the constants below:

  evaluation:lead_queue       LIST   lead_ids awaiting the worker
  evaluation:enqueued:{id}    STRING (SETNX) dedup-at-enqueue marker

The dedup marker and the queue push run inside a single Redis Lua script so
they are atomic: a failure between them can never leave a marker that
suppresses retries for the TTL window. This mirrors how the dispatch system
uses ``run_script`` for atomic multi-key ops, and is safe on the single-node
Redis that prod runs.
"""

from app.core.logger import logger
from app.services.redis.client import get_redis_service

LEAD_QUEUE_KEY = "evaluation:lead_queue"
ENQUEUED_KEY_PREFIX = "evaluation:enqueued:"
# A lead is "done" once judged; cap the dedup/replay window at 24h so a
# retried call-end within that window won't requeue an already-queued lead.
ENQUEUED_TTL_SECONDS = 24 * 3600

# Atomically set the dedup marker AND enqueue the lead_id, so a crash or
# Redis failure between the two can never leave a marker that suppresses
# retries. Returns 1 if newly enqueued, 0 if already enqueued (dedup).
#   KEYS[1] = evaluation:lead_queue (LIST)
#   KEYS[2] = evaluation:enqueued:{lead_id} (STRING marker)
#   ARGV[1] = lead_id   ARGV[2] = marker TTL (seconds)
_ENQUEUE_SCRIPT = """
if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[2]) then
    redis.call('RPUSH', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


async def enqueue_lead_for_evaluation(lead_id: str) -> bool:
    """Push a lead_id onto the evaluation queue, once per lead.

    Dedups via a SETNX marker inside the same atomic Lua script that RPUSHes
    the lead_id, so the marker and the queue entry can never diverge.
    Best-effort: any failure (Redis down, script error) is logged and
    swallowed so it can never break call teardown. Returns True only when the
    lead was newly enqueued.
    """
    if not lead_id:
        return False

    try:
        redis = await get_redis_service()
        marker = f"{ENQUEUED_KEY_PREFIX}{lead_id}"
        result = await redis.run_script(
            _ENQUEUE_SCRIPT,
            keys=[LEAD_QUEUE_KEY, marker],
            args=[lead_id, ENQUEUED_TTL_SECONDS],
        )
        if result == 1:
            logger.info(f"Enqueued lead {lead_id} for evaluation")
            return True
        if result == 0:
            logger.debug(f"Lead {lead_id} already enqueued for evaluation; skipping")
            return False
        # run_script swallows Redis errors and returns None on failure.
        logger.error(f"Failed to enqueue lead {lead_id}: script returned {result!r}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to enqueue lead for evaluation: {e}")
        return False
