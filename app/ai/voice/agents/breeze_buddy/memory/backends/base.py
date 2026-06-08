"""MemoryBackend interface and the backend-agnostic MemoryIdentity.

A backend owns *where memory lives and how it is extracted*. Everything else
(identity resolution, the Redis extraction queue, the drain worker's transcript
fetch, and the read/enqueue call-sites) is backend-agnostic and lives outside
this package.

Two concrete backends:
  - pgvector    : our own Postgres + pgvector store (app-side LLM extraction,
                  Azure embeddings, cosine dedup, supersede, phone->id merge).
  - supermemory : supermemory.ai hosted memory (it owns extraction/dedup).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional


@dataclass
class MemoryIdentity:
    """The resolved customer scope a memory operation applies to.

    `customer_key` is either a real `customer_id` or a provisional
    `phone:<normalized>` key (see memory/identity.py). `phone` and
    `explicit_customer_id` are only populated when a single conversation
    carried both pieces of identity, which is what drives the merge.
    """

    reseller_id: str
    merchant_id: str
    customer_key: str
    key_type: str  # "customer_id" | "phone"
    phone: Optional[str] = None
    explicit_customer_id: Optional[str] = None

    @property
    def scope_tag(self) -> str:
        """Deterministic single-tag namespace for hosted backends.

        supermemory containerTags use *exact array matching*, so the whole
        per-customer scope must collapse into one tag rather than a list.
        """
        return f"{self.reseller_id}:{self.merchant_id}:{self.customer_key}"


class MemoryBackend(ABC):
    """A pluggable memory provider.

    All methods are best-effort: implementations log and swallow their own
    errors so a failing memory backend never breaks a conversation.
    """

    name: ClassVar[str]

    @abstractmethod
    async def get_profile_block(
        self, identity: MemoryIdentity, max_facts: int = 20
    ) -> Optional[str]:
        """Return a `<user_memory>...</user_memory>` block for role_messages, or None."""

    @abstractmethod
    async def ingest(
        self,
        identity: MemoryIdentity,
        transcript: List[Dict[str, Any]],
        source_channel: str,
    ) -> None:
        """Persist durable memory from a conversation transcript.

        The backend owns extraction: pgvector runs the LLM consolidation +
        embedding + dedup itself; supermemory hands the transcript off and
        lets the service extract.
        """

    @abstractmethod
    async def search(
        self, identity: MemoryIdentity, query: str, k: int = 5
    ) -> List[str]:
        """Semantic recall over this customer's own facts. Returns fact strings."""

    async def merge_identity(self, identity: MemoryIdentity) -> MemoryIdentity:
        """Repoint provisional phone:* memory onto the canonical customer_id.

        Default is a no-op (return unchanged). Backends that store under a
        phone key override this to consolidate when a real customer_id and
        phone were both seen, then return the canonicalized identity.
        """
        return identity
