"""
Pydantic schemas for Knowledge Base (RAG) entities and API contracts.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class KnowledgeBaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class KbDocumentSourceType(str, Enum):
    FILE = "file"
    GOOGLE_SHEET = "google_sheet"
    TEXT = "text"


class KbDocumentStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    ERROR = "ERROR"


class EmbeddingConfig(BaseModel):
    """Per-KB embedding provider configuration.

    All providers are normalized to 768 dimensions (Matryoshka truncation +
    L2 renorm) so a single ``halfvec(768)`` column/index serves every KB.
    """

    provider: str = Field("openai", description="Embedding provider key.")
    model: str = Field(
        "text-embedding-3-small", description="Provider model identifier."
    )
    dimensions: int = Field(768, description="Stored vector dimensions (fixed).")


class KnowledgeBase(BaseModel):
    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    embedding_config: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    settings: Dict[str, Any] = Field(default_factory=dict)
    status: KnowledgeBaseStatus = KnowledgeBaseStatus.ACTIVE
    document_count: Optional[int] = None
    chunk_count: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class KbDocument(BaseModel):
    id: str
    kb_id: str
    source_type: KbDocumentSourceType
    name: str
    source_ref: Dict[str, Any] = Field(default_factory=dict)
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    status: KbDocumentStatus = KbDocumentStatus.PENDING
    error_message: Optional[str] = None
    content_hash: Optional[str] = None
    chunk_count: int = 0
    token_count: int = 0
    synced_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RetrievedChunk(BaseModel):
    """A chunk returned by hybrid retrieval, with fused + per-leg scores."""

    id: str
    document_id: str
    kb_id: str
    chunk_index: int
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    token_count: int = 0
    score: float = Field(0.0, description="RRF-fused relevance score.")
    vector_similarity: Optional[float] = Field(
        None, description="Cosine similarity from the vector leg (0-1)."
    )
    document_name: Optional[str] = None


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------


class CreateKnowledgeBaseRequest(BaseModel):
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    embedding_config: Optional[EmbeddingConfig] = None
    settings: Optional[Dict[str, Any]] = None


class KbDocumentDownloadResponse(BaseModel):
    """Response of GET /knowledge-bases/{kb_id}/documents/{id}/download.

    For uploaded files ``url`` is a short-lived signed GCS link (the browser
    downloads the original bytes directly, nothing streams through the app);
    for google_sheet documents it is the sheet's normal Google URL and
    ``expires_in_seconds`` is null.
    """

    url: str
    filename: str
    expires_in_seconds: Optional[int] = None


class UpdateKnowledgeBaseRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    status: Optional[KnowledgeBaseStatus] = None


class KnowledgeBaseListResponse(BaseModel):
    knowledge_bases: List[KnowledgeBase]
    total: int


class KbDocumentListResponse(BaseModel):
    documents: List[KbDocument]
    total: int


class DeleteKnowledgeBaseResponse(BaseModel):
    status: str
    kb_id: str
    message: str


class AddSheetDocumentRequest(BaseModel):
    """Connect a Google Sheet as a knowledge base document."""

    sheet_url: str = Field(
        ..., min_length=10, description="Full Google Sheets URL (or bare ID)."
    )
    ranges: Optional[List[str]] = Field(
        None,
        description="Sheet tabs / A1 ranges to sync. Omit to sync all tabs.",
    )
    name: Optional[str] = Field(
        None, description="Display name; defaults to the spreadsheet title."
    )


class SheetsServiceAccountResponse(BaseModel):
    """The service-account email merchants share their sheet with."""

    email: str


class QueryKnowledgeBaseRequest(BaseModel):
    """Retrieval-testing request ("hit testing")."""

    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(6, ge=1, le=20)
    score_threshold: float = Field(0.0, ge=0.0, le=1.0)


class QueryKnowledgeBaseResponse(BaseModel):
    query: str
    chunks: List[RetrievedChunk]
    latency_ms: float
