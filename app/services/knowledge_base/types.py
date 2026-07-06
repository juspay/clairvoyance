"""
Internal data types shared by the knowledge base ingestion pipeline.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TableData:
    """One extracted table/sheet: header row + data rows."""

    name: str
    headers: List[str]
    rows: List[List[str]]


@dataclass
class NormalizedContent:
    """Connector output: prose text and/or tables, ready for chunking."""

    text: Optional[str] = None
    tables: List[TableData] = field(default_factory=list)
    content_hash: Optional[str] = None


@dataclass
class PreparedChunk:
    """A chunk ready for hash-diffing and embedding."""

    text: str
    metadata: Dict[str, Any]
    token_count: int
    content_hash: str
