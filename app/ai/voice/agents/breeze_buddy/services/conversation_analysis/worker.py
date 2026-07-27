"""Post-conversation topic extraction."""

import asyncio
import time

from app.core.logger import logger
from app.database.accessor.breeze_buddy.conversation_analysis import (
    claim_analysis_by_id,
    complete_analysis,
    fail_analysis,
    get_analysis_transcript,
)

from .extractor import extract_topics
from .queue import dequeue_topic_analysis

_consumer_task: asyncio.Task | None = None
_ANALYSIS_TIMEOUT_SECONDS = 60
_ANALYSIS_MAX_ATTEMPTS = 2


async def _consume_queue() -> None:
    """Consume Redis jobs sequentially."""
    while True:
        try:
            analysis_id = await dequeue_topic_analysis()
            await _analyze(analysis_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Topic analysis queue consumer failed: {exc}")
            await asyncio.sleep(1)


async def _analyze(row_id: str) -> None:
    job = await claim_analysis_by_id(row_id)
    if not job:
        return  # already claimed or gone

    analysis_id = str(job["id"])
    started_at = time.monotonic()
    logger.info(f"Conversation analysis {analysis_id} started")
    try:
        transcript = await get_analysis_transcript(job)
        topics = []
        for attempt in range(1, _ANALYSIS_MAX_ATTEMPTS + 1):
            attempt_started_at = time.monotonic()
            try:
                topics = await asyncio.wait_for(
                    extract_topics(
                        transcript,
                        job.get("accepted_topics") or [],
                        job.get("evaluation_configuration"),
                    ),
                    timeout=_ANALYSIS_TIMEOUT_SECONDS,
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elapsed = time.monotonic() - attempt_started_at
                logger.warning(
                    f"Conversation analysis {analysis_id} attempt "
                    f"{attempt}/{_ANALYSIS_MAX_ATTEMPTS} failed after "
                    f"{elapsed:.1f}s: {type(exc).__name__}: {exc}"
                )
                if attempt == _ANALYSIS_MAX_ATTEMPTS:
                    raise

        await complete_analysis(analysis_id, topics)
        elapsed = time.monotonic() - started_at
        logger.info(
            f"Conversation analysis {analysis_id} completed in "
            f"{elapsed:.1f}s with {len(topics)} topics"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        error = f"{type(exc).__name__}: {exc}"
        logger.error(
            f"Conversation analysis {analysis_id} failed after {elapsed:.1f}s: {error}"
        )
        await fail_analysis(analysis_id, error)


async def start_analysis_worker() -> None:
    global _consumer_task
    if _consumer_task is not None and not _consumer_task.done():
        return
    _consumer_task = asyncio.create_task(
        _consume_queue(), name="topic-analysis-consumer"
    )
    logger.info("Conversation analysis worker started")


async def stop_analysis_worker() -> None:
    global _consumer_task
    if _consumer_task is None:
        return
    _consumer_task.cancel()
    await asyncio.gather(_consumer_task, return_exceptions=True)
    _consumer_task = None
    logger.info("Conversation analysis worker stopped")
