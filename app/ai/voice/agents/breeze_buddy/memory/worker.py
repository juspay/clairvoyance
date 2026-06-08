"""Memory extraction drain worker.

Registered as a BackgroundTaskScheduler task. Each tick pops items from the
Redis queue, re-reads the transcript from DB, and hands (identity, transcript)
to the backend the enqueuing template chose. Transcript fetch and the phone ->
customer_id merge trigger live here (backend-agnostic); the actual extraction
and persistence live inside the backend.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.memory.backends import (
    MemoryIdentity,
    get_memory_backend,
)
from app.core.config.static import MEMORY_EXTRACTION_BATCH_SIZE
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    list_chat_messages_for_session,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import get_lead_by_id
from app.services.redis.client import get_redis_service

_QUEUE_KEY = "memory:extract:queue"


async def drain_memory_queue() -> None:
    """Pop up to MEMORY_EXTRACTION_BATCH_SIZE items from the queue and process each."""
    try:
        redis = await get_redis_service()
        client = await redis.get_client()
    except Exception as e:
        logger.error(f"[memory.worker] Redis unavailable: {e}")
        return

    processed = 0
    for _ in range(MEMORY_EXTRACTION_BATCH_SIZE):
        try:
            raw = await client.lpop(_QUEUE_KEY)  # type: ignore[union-attr]
            if raw is None:
                break
            item = json.loads(
                raw if isinstance(raw, (str, bytes, bytearray)) else str(raw)
            )
            await _process_item(item)
            processed += 1
        except json.JSONDecodeError as e:
            logger.warning(f"[memory.worker] bad queue item (skip): {e}")
        except Exception as e:
            logger.error(f"[memory.worker] item processing error: {e}", exc_info=True)

    if processed:
        logger.info(f"[memory.worker] processed {processed} extraction item(s)")


async def _process_item(item: Dict[str, Any]) -> None:
    kind: Optional[str] = item.get("kind") or None
    record_id: Optional[str] = item.get("record_id") or None
    customer_key: Optional[str] = item.get("customer_key") or None
    key_type: str = str(item.get("key_type") or "phone")
    reseller_id: Optional[str] = item.get("reseller_id") or None
    merchant_id: Optional[str] = item.get("merchant_id") or None
    source_channel: str = str(item.get("source_channel") or "voice")
    phone: Optional[str] = item.get("phone") or None
    explicit_customer_id: Optional[str] = item.get("explicit_customer_id") or None
    backend_name: Optional[str] = item.get("backend") or None
    extraction_prompt: Optional[str] = item.get("extraction_prompt") or None

    if not (kind and record_id and customer_key and reseller_id and merchant_id):
        logger.warning(f"[memory.worker] incomplete item, skipping: {item}")
        return

    transcript = await _fetch_transcript(kind, record_id)
    if not transcript:
        logger.warning(
            f"[memory.worker] empty transcript for {kind}:{record_id}, skipping"
        )
        return

    backend = get_memory_backend(backend_name)
    identity = MemoryIdentity(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        customer_key=customer_key,
        key_type=key_type,
        phone=phone,
        explicit_customer_id=explicit_customer_id,
    )

    # Repoint provisional phone:* memory onto the canonical customer_id when a
    # single conversation carried both. No-op unless that condition holds.
    if phone and explicit_customer_id and key_type == "phone":
        identity = await backend.merge_identity(identity)

    await backend.ingest(identity, transcript, source_channel, extraction_prompt)


async def _fetch_transcript(kind: str, record_id: str) -> List[Dict[str, Any]]:
    """Re-read transcript from DB by kind (voice_lead | chat_session)."""
    try:
        if kind == "voice_lead":
            lead = await get_lead_by_id(record_id)
            if lead and lead.metaData:
                return lead.metaData.get("transcription", [])

        elif kind == "chat_session":
            messages = await list_chat_messages_for_session(record_id)
            return [
                {"role": m.role, "content": m.content}
                for m in (messages or [])
                if m.role in ("user", "assistant") and m.content
            ]

    except Exception as e:
        logger.error(
            f"[memory.worker] fetch_transcript failed ({kind}:{record_id}): {e}",
            exc_info=True,
        )

    return []
