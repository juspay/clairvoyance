"""
Knowledge base (RAG) services: connectors, chunking, ingestion, retrieval.
"""

from app.services.knowledge_base.ingestion import (
    kick_ingestion,
    process_pending_documents,
)
from app.services.knowledge_base.retrieval import (
    get_full_kb_text,
    get_kb_tab_text,
    get_kb_token_count,
    list_kb_tabs,
    retrieve,
)

__all__ = [
    "get_full_kb_text",
    "get_kb_tab_text",
    "get_kb_token_count",
    "kick_ingestion",
    "list_kb_tabs",
    "process_pending_documents",
    "retrieve",
]
