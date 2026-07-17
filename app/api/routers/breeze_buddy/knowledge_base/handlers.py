"""
Business logic handlers for knowledge base operations.
"""

import asyncio
import io
import time
from typing import Optional, Union
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config.dynamic import KB_MAX_DOCUMENTS_PER_KB, KB_MAX_FILE_MB
from app.core.logger import logger
from app.database.accessor.breeze_buddy.knowledge_base import (
    create_kb_document,
    create_knowledge_base,
    delete_kb_document,
    delete_knowledge_base,
    find_templates_using_kb,
    get_kb_document_by_id,
    get_knowledge_base_by_id,
    list_kb_documents,
    list_knowledge_bases,
    mark_kb_document_for_resync,
    update_knowledge_base,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.knowledge_base import (
    CreateKnowledgeBaseRequest,
    DeleteKnowledgeBaseResponse,
    EmbeddingConfig,
    KbDocument,
    KbDocumentListResponse,
    KbDocumentSourceType,
    KbUsageResponse,
    KbUsageTemplate,
    KnowledgeBase,
    KnowledgeBaseListResponse,
    QueryKnowledgeBaseRequest,
    QueryKnowledgeBaseResponse,
    UpdateKnowledgeBaseRequest,
)
from app.services.gcp.storage.storage import GCSStorage
from app.services.knowledge_base import kick_ingestion, retrieve
from app.services.knowledge_base.ingestion import bump_kb_version

from .rbac import (
    accessible_scope_filters,
    require_kb_write_access,
    validate_kb_access,
)

ALLOWED_UPLOAD_EXTENSIONS = {"pdf", "docx", "xlsx", "csv", "txt", "md"}


def _require_uuid(value: str, label: str) -> None:
    """Non-UUID path params 404 cleanly instead of surfacing an asyncpg
    DataError as a 500 (uuid columns reject non-UUID parameter text)."""
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} {value} not found",
        )


async def _get_kb_or_404(kb_id: str) -> KnowledgeBase:
    """Shared entry check for every KB-scoped endpoint: validate the path
    param is a UUID, load the KB, 404 if absent. Callers follow with the
    RBAC check appropriate to their operation (read vs write gate)."""
    _require_uuid(kb_id, "Knowledge base")
    kb = await get_knowledge_base_by_id(kb_id)
    if kb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge base {kb_id} not found",
        )
    return kb


async def create_kb_handler(
    request: CreateKnowledgeBaseRequest, current_user: UserInfo
) -> KnowledgeBase:
    """Create a knowledge base (POST /knowledge-bases).

    Write-gated by ``require_kb_write_access`` at the route (a merchant user
    may only create a KB for a merchant they own).
    Duplicate (reseller, merchant, name) maps to 409 via the typed
    unique-violation catch; embedding config defaults to OpenAI 768-dim
    and is frozen after creation.
    """
    logger.info(
        f"User {current_user.username} creating knowledge base '{request.name}' "
        f"for reseller {request.reseller_id}"
    )
    try:
        kb = await create_knowledge_base(
            reseller_id=request.reseller_id,
            merchant_id=request.merchant_id,
            name=request.name,
            description=request.description,
            embedding_config=(
                request.embedding_config or EmbeddingConfig()
            ).model_dump(),
            settings=request.settings or {},
        )
    except asyncpg.UniqueViolationError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A knowledge base named '{request.name}' already exists "
                f"for this reseller/merchant"
            ),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create knowledge base",
        ) from e
    if kb is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create knowledge base",
        )
    return kb


async def list_kbs_handler(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    include_archived: bool,
    search: Optional[str],
    page: Optional[int],
    limit: Optional[int],
) -> KnowledgeBaseListResponse:
    """List knowledge bases visible to the caller (GET /knowledge-bases/list).

    Visibility comes from the JWT via ``accessible_scope_filters`` — request
    params can only narrow it, never widen it. Backs the loom KB list page
    and its template-attachment KB picker.
    """
    reseller_ids, merchant_ids = accessible_scope_filters(
        current_user, reseller_id, merchant_id
    )
    knowledge_bases, total = await list_knowledge_bases(
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        include_archived=include_archived,
        search=search,
        page=page,
        limit=limit,
    )
    return KnowledgeBaseListResponse(knowledge_bases=knowledge_bases, total=total)


async def get_kb_handler(kb_id: str, current_user: UserInfo) -> KnowledgeBase:
    """Get one knowledge base with live counts (GET /knowledge-bases/{id}).

    Read-gated: any user whose JWT covers the KB's reseller/merchant may
    view. Backs the loom KB detail page header.
    """
    kb = await _get_kb_or_404(kb_id)
    validate_kb_access(current_user, kb.reseller_id, kb.merchant_id, "read")
    return kb


async def get_kb_usage_handler(kb_id: str, current_user: UserInfo) -> KbUsageResponse:
    """Templates whose ENABLED configurations reference this KB.

    Read-gated like get_kb_handler; backs the loom KB detail "Used by" rail
    (the delete safety check reuses the same accessor).
    """
    kb = await _get_kb_or_404(kb_id)
    validate_kb_access(current_user, kb.reseller_id, kb.merchant_id, "read")
    rows = await find_templates_using_kb(kb_id)
    return KbUsageResponse(templates=[KbUsageTemplate(**row) for row in rows])


async def update_kb_handler(
    kb_id: str, request: UpdateKnowledgeBaseRequest, current_user: UserInfo
) -> KnowledgeBase:
    """Update KB name/description/settings/status (PUT /knowledge-bases/{id}).

    Write-gated. Only provided fields change; embedding config is
    intentionally not editable here (stored vectors depend on it).
    """
    kb = await _get_kb_or_404(kb_id)
    require_kb_write_access(
        current_user, kb.reseller_id, kb.merchant_id, "update knowledge bases"
    )
    updated = await update_knowledge_base(
        kb_id,
        name=request.name,
        description=request.description,
        settings=request.settings,
        status=request.status.value if request.status else None,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update knowledge base",
        )
    return updated


async def delete_kb_handler(
    kb_id: str, current_user: UserInfo
) -> DeleteKnowledgeBaseResponse:
    """Delete a knowledge base after the template in-use safety check."""
    kb = await _get_kb_or_404(kb_id)
    require_kb_write_access(
        current_user, kb.reseller_id, kb.merchant_id, "delete knowledge bases"
    )

    templates_using = await find_templates_using_kb(kb_id)
    if templates_using:
        names = ", ".join(t["name"] for t in templates_using[:5])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Knowledge base is attached to {len(templates_using)} "
                f"template(s) ({names}). Detach it before deleting."
            ),
        )

    # Best-effort GCS cleanup before the CASCADE wipes the rows.
    documents = await list_kb_documents(kb_id)
    gcs_paths = [
        d.source_ref.get("gcs_path")
        for d in documents
        if d.source_type == KbDocumentSourceType.FILE and d.source_ref.get("gcs_path")
    ]
    if gcs_paths:

        def _cleanup():
            # Best-effort sweep of the KB's stored files (sync client, run
            # in a thread). Failures here don't block the DB delete.
            storage = GCSStorage()
            for path in gcs_paths:
                storage.delete_file(path)

        await asyncio.to_thread(_cleanup)

    deleted = await delete_knowledge_base(kb_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge base {kb_id} not found",
        )
    await bump_kb_version(kb_id)
    return DeleteKnowledgeBaseResponse(
        status="success",
        kb_id=kb_id,
        message=f"Knowledge base '{kb.name}' deleted with {len(documents)} document(s)",
    )


async def upload_document_handler(
    kb_id: str, file: UploadFile, current_user: UserInfo
) -> KbDocument:
    """Upload a file into a knowledge base: validate -> GCS -> PENDING doc
    -> immediate ingestion kick."""
    kb = await _get_kb_or_404(kb_id)
    require_kb_write_access(
        current_user, kb.reseller_id, kb.merchant_id, "upload documents"
    )

    filename = file.filename or "upload"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type '.{extension}'. Supported: "
                f"{', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}"
            ),
        )

    max_docs = await KB_MAX_DOCUMENTS_PER_KB()
    documents = await list_kb_documents(kb_id)
    if len(documents) >= max_docs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Knowledge base already has {len(documents)} documents "
            f"(cap: {max_docs})",
        )

    max_mb = await KB_MAX_FILE_MB()
    max_bytes = max_mb * 1024 * 1024
    # Reject before buffering: file.size covers well-formed multipart bodies,
    # and the capped read bounds memory even when size is absent or wrong.
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File is {file.size // (1024 * 1024)}MB; the cap is {max_mb}MB",
        )
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {max_mb}MB cap",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty"
        )

    gcs_path = f"kb/{kb_id}/{uuid4()}/{filename}"

    def _upload() -> bool:
        # Sync GCS client — runs via asyncio.to_thread so the upload doesn't
        # block the event loop shared with live calls.
        import io

        return GCSStorage().upload_file_object(
            io.BytesIO(data), gcs_path, content_type=file.content_type
        )

    uploaded = await asyncio.to_thread(_upload)
    if not uploaded:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to store the file; try again",
        )

    document = await create_kb_document(
        kb_id=kb_id,
        source_type=KbDocumentSourceType.FILE.value,
        name=filename,
        source_ref={"gcs_path": gcs_path},
        mime_type=file.content_type,
        size_bytes=len(data),
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register the document",
        )

    kick_ingestion()
    return document


async def list_documents_handler(
    kb_id: str, current_user: UserInfo
) -> KbDocumentListResponse:
    """List a KB's documents with ingestion status (GET .../documents).

    Backs the loom documents table — status badges (PENDING/PROCESSING/
    READY/ERROR), chunk counts and last-synced timestamps come from here.
    """
    kb = await _get_kb_or_404(kb_id)
    validate_kb_access(current_user, kb.reseller_id, kb.merchant_id, "read")
    documents = await list_kb_documents(kb_id)
    return KbDocumentListResponse(documents=documents, total=len(documents))


async def delete_document_handler(
    kb_id: str, document_id: str, current_user: UserInfo
) -> dict:
    """Delete one document (DELETE .../documents/{id}).

    Chunks go with it via CASCADE; the GCS object is removed best-effort in
    a thread; the KB cache version is bumped so full-injection/token caches
    refresh. The document must belong to the KB in the path (guards against
    cross-KB ID probing).
    """
    kb = await _get_kb_or_404(kb_id)
    require_kb_write_access(
        current_user, kb.reseller_id, kb.merchant_id, "delete documents"
    )

    _require_uuid(document_id, "Document")
    document = await get_kb_document_by_id(document_id)
    if document is None or document.kb_id != kb_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found in knowledge base {kb_id}",
        )

    gcs_path = document.source_ref.get("gcs_path")
    if document.source_type == KbDocumentSourceType.FILE and gcs_path:
        await asyncio.to_thread(lambda: GCSStorage().delete_file(gcs_path))

    await delete_kb_document(document_id)
    await bump_kb_version(kb_id)
    return {
        "status": "success",
        "document_id": document_id,
        "message": f"Document '{document.name}' deleted",
    }


async def download_document_handler(
    kb_id: str, document_id: str, current_user: UserInfo
) -> Union[JSONResponse, StreamingResponse]:
    """Serve a document's original source (GET .../documents/{id}/download).

    Read-gated like the other read endpoints. FILE docs stream from GCS
    through the API (dev/user credentials can't mint signed URLs, so no
    presigned links); SHEET docs answer with their sheet URL as JSON
    ``{"url": ...}``. Same cross-KB ID-probing guard as delete/resync.
    """
    kb = await _get_kb_or_404(kb_id)
    validate_kb_access(current_user, kb.reseller_id, kb.merchant_id, "download")

    _require_uuid(document_id, "Document")
    document = await get_kb_document_by_id(document_id)
    if document is None or document.kb_id != kb_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found in knowledge base {kb_id}",
        )

    if document.source_type == KbDocumentSourceType.GOOGLE_SHEET:
        sheet_url = (
            document.source_ref.get("sheet_url")
            or document.source_ref.get("spreadsheet_url")
            or document.source_ref.get("url")
        )
        if not sheet_url:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This document has no downloadable source",
            )
        return JSONResponse({"url": sheet_url})

    gcs_path = document.source_ref.get("gcs_path")
    if not gcs_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This document has no downloadable source",
        )
    data = await asyncio.to_thread(lambda: GCSStorage().download_as_bytes(gcs_path))
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not fetch the file from storage",
        )
    # RFC 5987 filename* so names with spaces/unicode survive the header
    disposition = f"attachment; filename*=UTF-8''{quote(document.name or 'document')}"
    return StreamingResponse(
        io.BytesIO(data),
        media_type=document.mime_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


async def resync_document_handler(
    kb_id: str, document_id: str, current_user: UserInfo
) -> KbDocument:
    """Manual re-sync: send READY/ERROR back to PENDING and kick ingestion.

    For files this re-parses the stored object; for google_sheet documents
    (follow-up PR) it re-fetches the sheet.
    """
    kb = await _get_kb_or_404(kb_id)
    require_kb_write_access(
        current_user, kb.reseller_id, kb.merchant_id, "sync documents"
    )

    _require_uuid(document_id, "Document")
    document = await get_kb_document_by_id(document_id)
    if document is None or document.kb_id != kb_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found in knowledge base {kb_id}",
        )

    requeued = await mark_kb_document_for_resync(document_id)
    if requeued is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document is currently {document.status.value}; "
            "wait for it to finish before re-syncing",
        )

    kick_ingestion()
    return requeued


async def query_kb_handler(
    kb_id: str, request: QueryKnowledgeBaseRequest, current_user: UserInfo
) -> QueryKnowledgeBaseResponse:
    """Retrieval testing ("hit testing"): query -> scored chunks."""
    kb = await _get_kb_or_404(kb_id)
    validate_kb_access(current_user, kb.reseller_id, kb.merchant_id, "query")

    started = time.perf_counter()
    try:
        chunks = await retrieve(
            kb_ids=[kb_id],
            query=request.query,
            top_k=request.top_k,
            score_threshold=request.score_threshold,
        )
    except Exception as e:
        logger.error(f"Retrieval test failed for KB {kb_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Retrieval failed",
        )
    latency_ms = (time.perf_counter() - started) * 1000
    return QueryKnowledgeBaseResponse(
        query=request.query, chunks=chunks, latency_ms=round(latency_ms, 1)
    )
