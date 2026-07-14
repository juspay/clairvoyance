"""
Source connector registry.

Keyed by ``kb_document.source_type``. Adding a source (google_sheet, notion,
website, ...) = one connector module + one entry here.
"""

from typing import Dict

from app.services.knowledge_base.connectors.base import KBConnector
from app.services.knowledge_base.connectors.file_upload import FileUploadConnector
from app.services.knowledge_base.connectors.google_sheets import GoogleSheetsConnector

SOURCE_CONNECTORS: Dict[str, KBConnector] = {
    FileUploadConnector.source_type: FileUploadConnector(),
    GoogleSheetsConnector.source_type: GoogleSheetsConnector(),
}


def get_connector(source_type: str) -> KBConnector:
    """Resolve the connector for a document's source_type.

    Ingestion's single dispatch point (``_process_document``): the worker
    never knows source specifics, it just calls ``connector.load``.
    Raises ValueError (-> document ERROR with a clear message) for
    unregistered types, e.g. rows written by a newer deploy during a
    rolling update.
    """
    connector = SOURCE_CONNECTORS.get(source_type)
    if connector is None:
        raise ValueError(
            f"No connector registered for source type '{source_type}'. "
            f"Available: {sorted(SOURCE_CONNECTORS)}"
        )
    return connector


__all__ = ["KBConnector", "SOURCE_CONNECTORS", "get_connector"]
