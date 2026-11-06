"""
Source connector contract for knowledge base ingestion.

Onyx-inspired minimal interface: ``load`` does a full fetch + normalization;
``detect_change`` is the cheap freshness probe used by scheduled sync (a
Drive ``modifiedTime`` call for sheets, a content-hash comparison for files).
New sources (notion, website, ...) plug in via ``SOURCE_CONNECTORS`` in
``app/services/knowledge_base/connectors/__init__.py``.
"""

from abc import ABC, abstractmethod

from app.schemas.breeze_buddy.knowledge_base import KbDocument
from app.services.knowledge_base.types import NormalizedContent


class KBConnector(ABC):
    """Async connector for one document source type."""

    source_type: str = ""

    @abstractmethod
    async def load(self, document: KbDocument) -> NormalizedContent:
        """Fetch and normalize the document's full content.

        Raises on unrecoverable errors; the ingestion worker converts those
        into ERROR document status with the message surfaced to the UI.
        """
        raise NotImplementedError

    async def detect_change(self, document: KbDocument) -> bool:
        """Cheap probe: has the source changed since ``document.synced_at``?

        Default: assume changed (manual re-sync always re-fetches).
        """
        return True
