"""
A knowledge-base upload's filename is client-written, so the Cloud Storage key
carries only a cleaned copy of it (no folder parts, safe characters, capped
length, the allow-listed extension), while the document keeps the original
name for display and parsing.

Nothing here reaches Cloud Storage or Postgres: both are faked.
"""

import re
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from starlette.datastructures import Headers, UploadFile

from app.api.routers.breeze_buddy.knowledge_base import handlers as kb

SAFE_KEY_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(pdf|docx|xlsx|csv|txt|md)$"
)


@pytest.mark.parametrize(
    "filename, extension, expected",
    [
        ("menu.pdf", "pdf", "menu.pdf"),
        ("Price-List_2026.v2.xlsx", "xlsx", "Price-List_2026.v2.xlsx"),
        ("my report (final).docx", "docx", "my_report_final.docx"),
        ("Menu.PDF", "pdf", "Menu.pdf"),
        # folder parts, either slash, are dropped
        ("../../other-kb/x.pdf", "pdf", "x.pdf"),
        ("a/b/c/d.csv", "csv", "d.csv"),
        ("C:\\Users\\me\\notes.txt", "txt", "notes.txt"),
        # nothing safe left: a fixed stem
        (".pdf", "pdf", "upload.pdf"),
        ("..pdf", "pdf", "upload.pdf"),
        ("मेनू.pdf", "pdf", "upload.pdf"),
        ("line\nbreak\r\x00.md", "md", "line_break.md"),
    ],
)
def test_the_storage_name_is_a_cleaned_copy(filename, extension, expected):
    assert kb._storage_name(filename, extension) == expected


def test_a_long_filename_is_capped():
    name = kb._storage_name("a" * 2000 + ".pdf", "pdf")

    assert name == "a" * kb._MAX_KEY_NAME_CHARS + ".pdf"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    """Fake everything around the upload; record the GCS key and the document."""
    seen: Dict[str, List[Any]] = {"keys": [], "documents": []}

    async def get_kb(kb_id: str) -> Any:
        return SimpleNamespace(id=kb_id, reseller_id="r-1", merchant_id="m-1")

    async def ten() -> int:
        return 10

    async def no_documents(kb_id: str) -> List[Any]:
        return []

    class Storage:
        def upload_file_object(self, file_obj, path, content_type=None) -> bool:
            seen["keys"].append(path)
            return True

    async def create_kb_document(**fields: Any) -> Any:
        seen["documents"].append(fields)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(kb, "_get_kb_or_404", get_kb)
    monkeypatch.setattr(kb, "require_kb_write_access", lambda *a, **k: None)
    monkeypatch.setattr(kb, "KB_MAX_DOCUMENTS_PER_KB", ten)
    monkeypatch.setattr(kb, "KB_MAX_FILE_MB", ten)
    monkeypatch.setattr(kb, "list_kb_documents", no_documents)
    monkeypatch.setattr(kb, "GCSStorage", Storage)
    monkeypatch.setattr(kb, "create_kb_document", create_kb_document)
    monkeypatch.setattr(kb, "kick_ingestion", lambda: None)
    return seen


@pytest.mark.parametrize(
    "filename",
    [
        "../../other-kb/x.pdf",
        "a/b/c/menu.pdf",
        "evil\nname.pdf",
        "x" * 3000 + ".pdf",
        "menu.pdf",
    ],
)
async def test_an_upload_is_stored_under_a_clean_key_with_its_name_kept(
    stored: Dict[str, List[Any]], filename: str
) -> None:
    upload = UploadFile(
        file=BytesIO(b"%PDF-1.4 data"),
        filename=filename,
        size=13,
        headers=Headers({"content-type": "application/pdf"}),
    )

    await kb.upload_document_handler("kb-1", upload, current_user=None)  # type: ignore[arg-type]

    [key] = stored["keys"]
    prefix, folder, name = key.rsplit("/", 2)
    assert prefix == "kb/kb-1"
    assert re.fullmatch(r"[0-9a-f-]{36}", folder)
    assert SAFE_KEY_NAME.match(name)
    [document] = stored["documents"]
    assert document["name"] == filename
    assert document["source_ref"] == {"gcs_path": key}
