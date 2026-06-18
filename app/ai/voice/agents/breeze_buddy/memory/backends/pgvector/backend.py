"""pgvector memory backend.

Owns the full DIY memory pipeline: app-side LLM extraction (consolidate),
Azure embeddings, cosine dedup, supersede, and the phone -> customer_id merge.
Reads/writes our own `user_memory` + `customer_identity` tables via the
existing three-layer DB accessors.
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.memory.backends.base import (
    MemoryBackend,
    MemoryIdentity,
)
from app.ai.voice.agents.breeze_buddy.memory.backends.pgvector.embeddings import (
    embed_single,
)
from app.ai.voice.agents.breeze_buddy.memory.backends.pgvector.extract import (
    _cosine_similarity,
    _find_duplicate,
    consolidate,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.customer_identity import upsert_alias
from app.database.accessor.breeze_buddy.user_memory import (
    insert_user_memory,
    list_user_memories,
    merge_phone_key_into_customer_id,
    supersede_memory,
)
from app.database.queries.breeze_buddy.blacklisted_numbers import (
    normalize_phone_number,
)


class PgVectorMemoryBackend(MemoryBackend):
    name: ClassVar[str] = "pgvector"

    async def get_profile_block(
        self, identity: MemoryIdentity, max_facts: int = 20
    ) -> Optional[str]:
        """Render active facts for the identity into a <user_memory> block."""
        try:
            memories = await list_user_memories(
                identity.reseller_id, identity.merchant_id, identity.customer_key
            )
            if not memories:
                return None

            lines = [m.fact for m in memories[:max_facts]]
            body = "\n".join(f"- {line}" for line in lines)
            return f"<user_memory>\n{body}\n</user_memory>"
        except Exception as e:
            logger.error(
                f"[memory.pgvector] get_profile_block failed "
                f"(key={identity.customer_key!r}): {e}",
                exc_info=True,
            )
            return None

    async def ingest(
        self,
        identity: MemoryIdentity,
        transcript: List[Dict[str, Any]],
        source_channel: str,
        extraction_prompt: Optional[str] = None,
    ) -> None:
        """Extract durable facts from the transcript and upsert them."""
        if not transcript:
            return

        try:
            existing = await list_user_memories(
                identity.reseller_id, identity.merchant_id, identity.customer_key
            )
        except Exception as e:
            logger.error(f"[memory.pgvector] fetch existing facts failed: {e}")
            existing = []

        ops = await consolidate(
            existing_facts=existing,
            transcript=transcript,
            extraction_prompt=extraction_prompt,
        )
        if not ops:
            return

        for op in ops:
            try:
                await self._apply_op(
                    op=op,
                    existing=existing,
                    identity=identity,
                    source_channel=source_channel,
                )
            except Exception as op_err:
                logger.error(
                    f"[memory.pgvector] apply_op failed: {op_err} op={op}",
                    exc_info=True,
                )

    async def search(
        self, identity: MemoryIdentity, query: str, k: int = 5
    ) -> List[str]:
        """Embed the query and cosine-rank the user's own facts."""
        try:
            memories = await list_user_memories(
                identity.reseller_id, identity.merchant_id, identity.customer_key
            )
            if not memories:
                return []

            query_embedding = await embed_single(query)
            scored = []
            for m in memories:
                if query_embedding and m.embedding:
                    sim = _cosine_similarity(query_embedding, m.embedding)
                    scored.append((sim, m.fact))
                else:
                    scored.append((0.0, m.fact))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [fact for _, fact in scored[:k]]
        except Exception as e:
            logger.error(
                f"[memory.pgvector] search failed (key={identity.customer_key!r}): {e}",
                exc_info=True,
            )
            return []

    async def merge_identity(self, identity: MemoryIdentity) -> MemoryIdentity:
        """Upsert the phone<->id alias and repoint provisional phone:* rows."""
        if not (
            identity.phone
            and identity.explicit_customer_id
            and identity.key_type == "phone"
        ):
            return identity

        try:
            normalized = normalize_phone_number(identity.phone)
            await upsert_alias(
                identity.reseller_id,
                identity.merchant_id,
                normalized,
                identity.explicit_customer_id,
            )
            await merge_phone_key_into_customer_id(
                identity.reseller_id,
                identity.merchant_id,
                identity.customer_key,
                identity.explicit_customer_id,
            )
            return MemoryIdentity(
                reseller_id=identity.reseller_id,
                merchant_id=identity.merchant_id,
                customer_key=identity.explicit_customer_id,
                key_type="customer_id",
                phone=identity.phone,
                explicit_customer_id=identity.explicit_customer_id,
            )
        except Exception as merge_err:
            logger.warning(
                f"[memory.pgvector] merge failed for phone={identity.phone!r}: "
                f"{merge_err}"
            )
            return identity

    async def _apply_op(
        self,
        op: Dict[str, Any],
        existing: list,
        identity: MemoryIdentity,
        source_channel: str,
    ) -> None:
        verb = op.get("op", "").upper()
        fact = (op.get("fact") or "").strip()
        if not fact:
            return

        category = op.get("category")
        structured = op.get("structured") or {}

        if verb == "ADD":
            embedding = await embed_single(fact)
            dup = _find_duplicate(embedding, fact, existing)
            if dup:
                logger.debug(
                    f"[memory.pgvector] ADD deduped against existing fact id={dup.id}"
                )
                return
            await insert_user_memory(
                reseller_id=identity.reseller_id,
                merchant_id=identity.merchant_id,
                customer_key=identity.customer_key,
                key_type=identity.key_type,
                fact=fact,
                category=category,
                structured=structured,
                embedding=embedding,
                source_channel=source_channel,
            )

        elif verb == "UPDATE":
            old_fact_text = (op.get("supersedes_fact") or "").strip()

            # 1. Exact match (free, no embedding call)
            old_mem = next(
                (m for m in existing if m.fact.strip() == old_fact_text), None
            )

            # 2. Embedding similarity fallback — catches LLM paraphrases of the
            #    stored fact (the LLM rarely reproduces exact stored text).
            if not old_mem and old_fact_text and existing:
                old_embedding = await embed_single(old_fact_text)
                if old_embedding:
                    best_sim, best_mem = 0.0, None
                    for m in existing:
                        if m.embedding:
                            sim = _cosine_similarity(old_embedding, m.embedding)
                            if sim > best_sim:
                                best_sim, best_mem = sim, m
                    if best_mem and best_sim >= 0.80:
                        old_mem = best_mem
                        logger.debug(
                            f"[memory.pgvector] UPDATE fuzzy-matched supersedes_fact "
                            f"sim={best_sim:.3f} old={old_mem.fact!r}"
                        )

            if old_mem:
                await supersede_memory(str(old_mem.id))
            embedding = await embed_single(fact)
            await insert_user_memory(
                reseller_id=identity.reseller_id,
                merchant_id=identity.merchant_id,
                customer_key=identity.customer_key,
                key_type=identity.key_type,
                fact=fact,
                category=category,
                structured=structured,
                embedding=embedding,
                source_channel=source_channel,
            )

        elif verb == "DELETE":
            old_fact_text = (op.get("supersedes_fact") or "").strip()
            old_mem = next(
                (m for m in existing if m.fact.strip() == old_fact_text), None
            )
            if old_mem:
                await supersede_memory(str(old_mem.id))
