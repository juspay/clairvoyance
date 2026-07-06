"""
Knowledge base management endpoints with RBAC.

Merchants upload documents (and later link Google Sheets) into
merchant-scoped knowledge bases; agents retrieve from them at runtime via
the template's ``configurations.knowledge_base`` attachment.

Endpoints:
- POST   /knowledge-bases                              - Create knowledge base
- GET    /knowledge-bases/list                         - List (RBAC-filtered)
- GET    /knowledge-bases/{id}                         - Get by ID
- PUT    /knowledge-bases/{id}                         - Update metadata
- DELETE /knowledge-bases/{id}                         - Delete (in-use check)
- POST   /knowledge-bases/{id}/documents               - Upload file (multipart)
- GET    /knowledge-bases/{id}/documents               - List documents + status
- DELETE /knowledge-bases/{id}/documents/{doc_id}      - Delete document
- POST   /knowledge-bases/{id}/documents/{doc_id}/sync - Manual re-sync
- POST   /knowledge-bases/{id}/query                   - Retrieval testing
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.knowledge_base import (
    CreateKnowledgeBaseRequest,
    DeleteKnowledgeBaseResponse,
    KbDocument,
    KbDocumentListResponse,
    KnowledgeBase,
    KnowledgeBaseListResponse,
    QueryKnowledgeBaseRequest,
    QueryKnowledgeBaseResponse,
    UpdateKnowledgeBaseRequest,
)

from .handlers import (
    create_kb_handler,
    delete_document_handler,
    delete_kb_handler,
    get_kb_handler,
    list_documents_handler,
    list_kbs_handler,
    query_kb_handler,
    resync_document_handler,
    update_kb_handler,
    upload_document_handler,
)
from .rbac import require_admin_or_reseller_owner

router = APIRouter()


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBase,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    request: CreateKnowledgeBaseRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Create a knowledge base scoped to a reseller (optionally a merchant)."""
    require_admin_or_reseller_owner(
        current_user, request.reseller_id, "create knowledge bases"
    )
    return await create_kb_handler(request, current_user)


@router.get("/knowledge-bases/list", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    reseller_id: Optional[str] = Query(None, description="Filter by reseller ID"),
    merchant_id: Optional[str] = Query(None, description="Filter by merchant ID"),
    include_archived: bool = Query(False, description="Include archived KBs"),
    search: Optional[str] = Query(None, description="Case-insensitive name search"),
    page: Optional[int] = Query(None, ge=1, description="Page number (1-based)"),
    limit: Optional[int] = Query(None, ge=1, le=100, description="Page size"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List knowledge bases accessible to the caller (JWT-scoped)."""
    return await list_kbs_handler(
        current_user, reseller_id, merchant_id, include_archived, search, page, limit
    )


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBase)
async def get_knowledge_base(
    kb_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Get a knowledge base by ID (includes document/chunk counts)."""
    return await get_kb_handler(kb_id, current_user)


@router.put("/knowledge-bases/{kb_id}", response_model=KnowledgeBase)
async def update_knowledge_base(
    kb_id: str,
    request: UpdateKnowledgeBaseRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Update knowledge base metadata/settings (embedding config is
    immutable -- stored vectors depend on it)."""
    return await update_kb_handler(kb_id, request, current_user)


@router.delete("/knowledge-bases/{kb_id}", response_model=DeleteKnowledgeBaseResponse)
async def delete_knowledge_base(
    kb_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Delete a knowledge base. 409 if any template still references it."""
    return await delete_kb_handler(kb_id, current_user)


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=KbDocument,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Upload a document (pdf/docx/xlsx/csv/txt/md). Stored in GCS, then
    parsed/chunked/embedded by the background ingestion worker; poll the
    documents list for READY/ERROR status."""
    return await upload_document_handler(kb_id, file, current_user)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=KbDocumentListResponse)
async def list_documents(
    kb_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List a knowledge base's documents with ingestion status."""
    return await list_documents_handler(kb_id, current_user)


@router.delete("/knowledge-bases/{kb_id}/documents/{document_id}")
async def delete_document(
    kb_id: str,
    document_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Delete a document and its chunks; the stored file is removed and
    runtime knowledge caches refresh on the next turn."""
    return await delete_document_handler(kb_id, document_id, current_user)


@router.post(
    "/knowledge-bases/{kb_id}/documents/{document_id}/sync",
    response_model=KbDocument,
)
async def sync_document(
    kb_id: str,
    document_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Manually re-sync a document (re-parse file / re-fetch sheet)."""
    return await resync_document_handler(kb_id, document_id, current_user)


@router.post(
    "/knowledge-bases/{kb_id}/query", response_model=QueryKnowledgeBaseResponse
)
async def query_knowledge_base(
    kb_id: str,
    request: QueryKnowledgeBaseRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Retrieval testing ("hit testing"): run a query against this KB and
    inspect the scored chunks. Powers the management UI test panel and
    pre-launch accuracy evals."""
    return await query_kb_handler(kb_id, request, current_user)
