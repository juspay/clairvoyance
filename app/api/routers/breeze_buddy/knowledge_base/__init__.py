"""
Knowledge Base management endpoints for Breeze Buddy RAG.

These endpoints let operators manage the RAG knowledge base for a given
Breeze Buddy template without touching GCS directly.

Knowledge files live at:
    gs://<RAG_GCS_BUCKET>/<merchant_id>/<template_id>/

Endpoints:
  POST   /knowledge-base/upload         - Upload a file into the template's GCS folder
  POST   /knowledge-base/index          - Trigger (re-)indexing for a template
  GET    /knowledge-base/status         - Index stats for a template
  DELETE /knowledge-base/invalidate     - Delete all pgvector chunks for a template

All endpoints require a valid Breeze Buddy JWT (admin role).
"""

import asyncio
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.config.static import GCS_CREDENTIALS_JSON, RAG_ENABLED, RAG_GCS_BUCKET
from app.core.logger import logger
from app.core.security.authorization import require_admin
from app.schemas import UserInfo

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    status: str
    gcs_path: str
    size_bytes: int
    message: str


class IndexResponse(BaseModel):
    status: str
    template_id: str
    gcs_path: str
    message: str


class StatusResponse(BaseModel):
    template_id: str
    gcs_path: str
    chunk_count: int
    total_documents: int
    index_size_bytes: int
    last_indexed_at: Optional[str]
    error_message: Optional[str]
    rag_enabled_globally: bool


class InvalidateResponse(BaseModel):
    status: str
    gcs_path: str
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_rag_enabled() -> None:
    """Raise 503 if RAG is disabled globally."""
    if not RAG_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG is disabled globally (RAG_ENABLED=false).",
        )


def _require_rag_bucket() -> str:
    """Return the RAG GCS bucket or raise 503 if not configured."""
    if not RAG_GCS_BUCKET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG_GCS_BUCKET env var is not configured on the server.",
        )
    return RAG_GCS_BUCKET


async def _load_template(template_id: str):
    """Return (template, kb_config) or raise 404/422."""
    from app.database.accessor.breeze_buddy.template import get_template_by_id

    template = await get_template_by_id(template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found.",
        )

    configurations = template.configurations
    if configurations is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Template has no configurations block.",
        )

    kb_config = getattr(configurations, "knowledge_base", None)
    if kb_config is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Template does not have a knowledge_base configuration.",
        )

    return template, kb_config


def _gcs_prefix(merchant_id: Optional[str], template_id: str) -> str:
    """Return the canonical GCS prefix for a template's knowledge files."""
    merchant = merchant_id or "default"
    return f"{merchant}/{template_id}/"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/knowledge-base/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a knowledge file to GCS",
    tags=["knowledge-base"],
)
async def upload_knowledge_file(
    template_id: str = Query(
        ..., description="Template UUID to associate the file with"
    ),
    file: UploadFile = File(..., description="File to upload (.txt, .md, .pdf, etc.)"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Upload a document to the template's GCS knowledge folder:
      ``gs://<RAG_GCS_BUCKET>/<merchant_id>/<template_id>/<filename>``

    After uploading, call ``POST /knowledge-base/index`` to rebuild the index.

    Permissions: admin only.
    """
    require_admin(current_user)
    _require_rag_enabled()
    gcs_bucket = _require_rag_bucket()

    if not GCS_CREDENTIALS_JSON:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GCS_CREDENTIALS_JSON is not configured on the server.",
        )

    template, _kb_config = await _load_template(template_id)
    prefix = _gcs_prefix(template.merchant_id, template_id)

    try:
        import json

        from google.cloud import storage
        from google.oauth2 import service_account

        creds_dict = json.loads(GCS_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        client = storage.Client(
            project=creds_dict.get("project_id"), credentials=credentials
        )

        blob_name = f"{prefix}{file.filename}"
        blob = client.bucket(gcs_bucket).blob(blob_name)

        content = await file.read()
        await asyncio.to_thread(
            blob.upload_from_string,
            content,
            content_type=file.content_type or "application/octet-stream",
        )

        gcs_path = f"gs://{gcs_bucket}/{blob_name}"
        logger.info(
            "Knowledge file uploaded: %s (%d bytes) by %s",
            gcs_path,
            len(content),
            current_user.username,
        )

        return UploadResponse(
            status="success",
            gcs_path=gcs_path,
            size_bytes=len(content),
            message=f"File uploaded to {gcs_path}. Run /knowledge-base/index to rebuild the index.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Knowledge file upload failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed. Check server logs for details.",
        )


@router.post(
    "/knowledge-base/index",
    response_model=IndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a (re-)index of the knowledge base",
    tags=["knowledge-base"],
)
async def index_knowledge_base(
    background_tasks: BackgroundTasks,
    template_id: str = Query(..., description="Template UUID to index"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Enqueue a (re-)index of the template's GCS knowledge folder into pgvector.

    Returns **202 Accepted** immediately; the indexing job runs in the
    background.  Poll ``GET /knowledge-base/status`` to check progress.

    Permissions: admin only.
    """
    require_admin(current_user)
    _require_rag_enabled()
    gcs_bucket = _require_rag_bucket()

    template, kb_config = await _load_template(template_id)
    prefix = _gcs_prefix(template.merchant_id, template_id)
    gcs_path = f"gs://{gcs_bucket}/{prefix}"

    from app.ai.voice.agents.breeze_buddy.services.rag.embeddings import (
        EmbeddingProvider,
    )
    from app.ai.voice.agents.breeze_buddy.services.rag.index_manager import (
        build_knowledge_base,
    )
    from app.core.config.static import (
        RAG_EMBEDDING_API_KEY,
        RAG_EMBEDDING_DEPLOYMENT,
        RAG_EMBEDDING_DIMENSION,
        RAG_EMBEDDING_ENDPOINT,
    )

    emb_endpoint = RAG_EMBEDDING_ENDPOINT
    emb_api_key = RAG_EMBEDDING_API_KEY

    if not emb_endpoint or not emb_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG embedding credentials are not configured "
            "(RAG_EMBEDDING_ENDPOINT / RAG_EMBEDDING_API_KEY).",
        )

    embedding_provider = EmbeddingProvider(
        api_key=emb_api_key,
        endpoint=emb_endpoint,
        deployment=RAG_EMBEDDING_DEPLOYMENT,
        dimension=RAG_EMBEDDING_DIMENSION,
    )

    async def _run_indexing() -> None:
        try:
            chunk_count = await build_knowledge_base(
                kb_config=kb_config,
                embedding_provider=embedding_provider,
                merchant_id=template.merchant_id or "default",
                template_id=template_id,
                gcs_bucket=gcs_bucket,
                gcs_prefix=prefix,
            )
            logger.info(
                "Knowledge base re-indexed for template %s: %d chunks at %s",
                template_id,
                chunk_count,
                gcs_path,
            )
        except Exception as exc:
            logger.error(
                "Background indexing failed for template %s: %s", template_id, exc
            )

    background_tasks.add_task(_run_indexing)

    return IndexResponse(
        status="accepted",
        template_id=template_id,
        gcs_path=gcs_path,
        message=f"Indexing job enqueued for {gcs_path}. Poll /knowledge-base/status to track progress.",
    )


@router.get(
    "/knowledge-base/status",
    response_model=StatusResponse,
    summary="Get knowledge base index status",
    tags=["knowledge-base"],
)
async def knowledge_base_status(
    template_id: str = Query(..., description="Template UUID"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Return live stats for the pgvector knowledge base for this template.

    If the knowledge base has not been indexed yet, numeric fields
    will be 0 and ``last_indexed_at`` will be null.

    Permissions: admin only.
    """
    require_admin(current_user)
    _require_rag_enabled()
    gcs_bucket = _require_rag_bucket()

    template, _kb_config = await _load_template(template_id)
    prefix = _gcs_prefix(template.merchant_id, template_id)
    gcs_path = f"gs://{gcs_bucket}/{prefix}"

    try:
        from app.ai.voice.agents.breeze_buddy.services.rag.index_manager import (
            get_cached_index_stats,
        )

        stats = await get_cached_index_stats(
            template.merchant_id or "default", template_id
        )

        return StatusResponse(
            template_id=template_id,
            gcs_path=gcs_path,
            chunk_count=stats.get("chunk_count", 0),
            total_documents=stats.get("total_documents", 0),
            index_size_bytes=stats.get("index_size_bytes", 0),
            last_indexed_at=stats.get("last_indexed_at"),
            error_message=stats.get("error_message"),
            rag_enabled_globally=RAG_ENABLED,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get KB status for template %s: %s", template_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Status check failed. Check server logs for details.",
        )


@router.delete(
    "/knowledge-base/invalidate",
    response_model=InvalidateResponse,
    summary="Invalidate (delete) the pgvector knowledge base chunks",
    tags=["knowledge-base"],
)
async def invalidate_knowledge_base(
    template_id: str = Query(..., description="Template UUID"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete all pgvector chunks for this template's knowledge base.

    The next ``POST /knowledge-base/index`` call will rebuild the embeddings
    from GCS.  Use this after uploading new documents to force a clean re-index.

    Permissions: admin only.
    """
    require_admin(current_user)
    _require_rag_enabled()
    gcs_bucket = _require_rag_bucket()

    template, _kb_config = await _load_template(template_id)
    prefix = _gcs_prefix(template.merchant_id, template_id)
    gcs_path = f"gs://{gcs_bucket}/{prefix}"

    try:
        from app.ai.voice.agents.breeze_buddy.services.rag.index_manager import (
            invalidate_index,
        )

        await invalidate_index(template.merchant_id or "default", template_id)

        logger.info(
            "Knowledge base index invalidated for template %s (%s) by %s",
            template_id,
            gcs_path,
            current_user.username,
        )

        return InvalidateResponse(
            status="success",
            gcs_path=gcs_path,
            message=f"Index for {gcs_path} deleted from pgvector.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("KB invalidation failed for template %s: %s", template_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalidation failed. Check server logs for details.",
        )
