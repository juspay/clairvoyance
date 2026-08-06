"""Crash-safe drain worker for backend-neutral memory extraction."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from app.ai.voice.agents.breeze_buddy.memory.backends import get_memory_backend
from app.ai.voice.agents.breeze_buddy.memory.backends.supermemory.client import (
    SupermemoryPermanentError,
)
from app.ai.voice.agents.breeze_buddy.memory.curator import consolidate
from app.ai.voice.agents.breeze_buddy.memory.queue import MemoryQueue
from app.core.config.dynamic import (
    BUDDY_MEMORY_ENABLED,
    MEMORY_EXTRACTION_BATCH_SIZE,
    MEMORY_EXTRACTION_MAX_ATTEMPTS,
    MEMORY_EXTRACTION_RETRY_BASE_SECONDS,
    MEMORY_EXTRACTION_VISIBILITY_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    list_chat_messages_for_session,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import get_lead_by_id
from app.database.accessor.breeze_buddy.user_memory import CustomerIdentityConflict
from app.schemas.breeze_buddy.memory import MemoryExtractionJob


async def drain_memory_queue() -> None:
    """Claim and process one bounded batch. Scheduler registration is deferred."""
    if not await BUDDY_MEMORY_ENABLED():
        return
    queue = await MemoryQueue.create()
    batch_size = await MEMORY_EXTRACTION_BATCH_SIZE()
    visibility = await MEMORY_EXTRACTION_VISIBILITY_TIMEOUT_SECONDS()
    max_attempts = await MEMORY_EXTRACTION_MAX_ATTEMPTS()
    retry_base = await MEMORY_EXTRACTION_RETRY_BASE_SECONDS()

    try:
        claims = await queue.claim(batch_size, visibility)
    except Exception as error:
        logger.error(f"[memory.worker] claim failed (error={type(error).__name__})")
        return

    processed = retried = poisoned = 0
    for claim in claims:
        if claim.job is None:
            await queue.poison(
                claim.job_id,
                claim_token=claim.claim_token,
                attempt=0,
                error=claim.validation_error or "invalid job",
            )
            poisoned += 1
            continue
        try:
            await _process_job(claim.job)
            if await queue.ack(claim.job_id, claim.claim_token):
                processed += 1
            else:
                logger.warning(
                    "[memory.worker] completed after lease expiry; "
                    "new owner will reconcile idempotently"
                )
        except (
            CustomerIdentityConflict,
            SupermemoryPermanentError,
            ValueError,
        ) as error:
            await queue.poison(
                claim.job_id,
                claim_token=claim.claim_token,
                attempt=claim.job.attempt,
                error=type(error).__name__,
            )
            poisoned += 1
        except Exception as error:
            next_attempt = claim.job.attempt + 1
            if next_attempt >= max_attempts:
                await queue.poison(
                    claim.job_id,
                    claim_token=claim.claim_token,
                    attempt=next_attempt,
                    error=type(error).__name__,
                )
                poisoned += 1
                continue
            retry_job = claim.job.model_copy(
                update={
                    "attempt": next_attempt,
                    "last_error": type(error).__name__,
                }
            )
            delay_seconds = min(retry_base * (2 ** (next_attempt - 1)), 3600)
            await queue.retry(
                claim.job_id,
                claim.claim_token,
                retry_job,
                int(time.time() * 1000) + delay_seconds * 1000,
            )
            retried += 1

    if claims:
        logger.info(
            "[memory.worker] drain result "
            f"(processed={processed}, retried={retried}, poisoned={poisoned})"
        )


async def _process_job(job: MemoryExtractionJob) -> None:
    transcript = await _fetch_transcript(job.kind, job.record_id)
    if not transcript:
        raise RuntimeError("memory source transcript is empty")

    backend = get_memory_backend(job.backend)
    identity = job.identity
    if identity.phone and identity.explicit_customer_id:
        identity = await backend.merge_identity(identity)

    existing = await backend.list_facts(identity, job.max_facts)
    operations = await consolidate(existing, transcript)
    await backend.apply_operations(
        identity,
        operations,
        source_channel=job.source_channel,
        operation_key=job.idempotency_key,
        retention_days=job.retention_days,
        max_facts=job.max_facts,
        embedding_config=job.embedding,
    )


async def _fetch_transcript(kind: str, record_id: str) -> List[Dict[str, Any]]:
    if kind == "voice_lead":
        lead = await get_lead_by_id(record_id)
        if lead and lead.metaData:
            transcription = lead.metaData.get("transcription", [])
            return transcription if isinstance(transcription, list) else []
        return []

    if kind == "chat_session":
        messages = await list_chat_messages_for_session(record_id)
        return [
            {"role": message.role, "content": message.content}
            for message in (messages or [])
            if message.role in ("user", "assistant") and message.content
        ]

    raise ValueError("unsupported memory job kind")
