"""supermemory.ai memory backend.

Hands conversations off to supermemory, which owns extraction, embedding,
dedup, and retrieval server-side. Each customer is namespaced by a single
deterministic containerTag (`identity.scope_tag`) — supermemory's container
tags use exact matching, so one composite tag per scope.

Reads use the v4 memories search (extracted facts), writes use document add,
and the phone->customer_id merge re-tags the customer's documents onto the
canonical scope. Best-effort throughout: a failing call degrades to an empty
result so a conversation is never blocked.
"""

from __future__ import annotations

from typing import Any, ClassVar, List, Optional

from app.ai.voice.agents.breeze_buddy.memory.backends.base import (
    MemoryBackend,
    MemoryIdentity,
)
from app.ai.voice.agents.breeze_buddy.memory.backends.supermemory.client import (
    SupermemoryClient,
)
from app.core.logger import logger

# Generic prompt used to pull a customer's salient stored facts for the
# start-of-conversation profile block (supermemory ranks by relevance to it).
_PROFILE_QUERY = "key facts, preferences, and prior outcomes for this user"


def _memory_text(result: Any) -> Optional[str]:
    """Extract the fact string from a v4 memories search Result."""
    memory = getattr(result, "memory", None)
    if isinstance(memory, str) and memory.strip():
        return memory.strip()
    chunks = getattr(result, "chunks", None) or []
    parts = [
        getattr(c, "content", None)
        for c in chunks
        if isinstance(getattr(c, "content", None), str)
    ]
    parts = [p.strip() for p in parts if p and p.strip()]
    if parts:
        return " ".join(parts)
    return None


def _document_id(doc: Any) -> Optional[str]:
    doc_id = getattr(doc, "id", None)
    return str(doc_id) if doc_id else None


class SupermemoryMemoryBackend(MemoryBackend):
    name: ClassVar[str] = "supermemory"

    def __init__(self, client: Optional[SupermemoryClient] = None) -> None:
        self._client = client or SupermemoryClient()

    async def get_profile_block(
        self, identity: MemoryIdentity, max_facts: int = 20
    ) -> Optional[str]:
        try:
            results = await self._client.search_memories(
                q=_PROFILE_QUERY, container_tag=identity.scope_tag, limit=max_facts
            )
            facts = [t for t in (_memory_text(r) for r in results) if t]
            if not facts:
                return None
            body = "\n".join(f"- {f}" for f in facts[:max_facts])
            return f"<user_memory>\n{body}\n</user_memory>"
        except Exception as e:
            logger.error(
                f"[memory.supermemory] get_profile_block failed "
                f"(scope={identity.scope_tag!r}): {e}",
                exc_info=True,
            )
            return None

    async def ingest(
        self,
        identity: MemoryIdentity,
        transcript: List[dict],
        source_channel: str,
        extraction_prompt: Optional[str] = None,
    ) -> None:
        if not transcript:
            return
        try:
            convo_text = "\n".join(
                f"{t.get('role')}: {t.get('content')}"
                for t in transcript
                if t.get("role") in ("user", "assistant") and t.get("content")
            )
            if not convo_text.strip():
                return
            # Prepend the extraction prompt as a hint so supermemory's server-side
            # AI sees the custom instructions when deciding what to extract.
            content = (
                f"{extraction_prompt}\n\nCONVERSATION:\n{convo_text}"
                if extraction_prompt
                else convo_text
            )
            await self._client.add(
                content=content,
                container_tags=[identity.scope_tag],
                metadata={
                    "source_channel": source_channel,
                    "key_type": identity.key_type,
                    "reseller_id": identity.reseller_id,
                    "merchant_id": identity.merchant_id,
                    "customer_key": identity.customer_key,
                },
            )
        except Exception as e:
            logger.error(
                f"[memory.supermemory] ingest failed "
                f"(scope={identity.scope_tag!r}): {e}",
                exc_info=True,
            )

    async def search(
        self, identity: MemoryIdentity, query: str, k: int = 5
    ) -> List[str]:
        try:
            results = await self._client.search_memories(
                q=query, container_tag=identity.scope_tag, limit=k
            )
            return [t for t in (_memory_text(r) for r in results) if t][:k]
        except Exception as e:
            logger.error(
                f"[memory.supermemory] search failed "
                f"(scope={identity.scope_tag!r}): {e}",
                exc_info=True,
            )
            return []

    async def merge_identity(self, identity: MemoryIdentity) -> MemoryIdentity:
        """Re-tag the provisional phone:* documents onto the customer_id scope."""
        if not (
            identity.phone
            and identity.explicit_customer_id
            and identity.key_type == "phone"
        ):
            return identity

        canonical = MemoryIdentity(
            reseller_id=identity.reseller_id,
            merchant_id=identity.merchant_id,
            customer_key=identity.explicit_customer_id,
            key_type="customer_id",
            phone=identity.phone,
            explicit_customer_id=identity.explicit_customer_id,
        )
        try:
            old_docs = await self._client.list_documents(
                [identity.scope_tag], limit=100
            )
            for doc in old_docs:
                doc_id = _document_id(doc)
                if doc_id:
                    await self._client.update_document(doc_id, [canonical.scope_tag])
        except Exception as merge_err:
            logger.warning(
                f"[memory.supermemory] merge retag failed for "
                f"phone={identity.phone!r}: {merge_err}"
            )
        return canonical
