"""
Decode template_version rows into Pydantic models.

Pure transformation functions (no I/O), mirroring decoder/breeze_buddy/template.py.
"""

import json
from typing import Any, List, Optional

import asyncpg

from app.schemas.breeze_buddy.template import (
    TemplateVersionDetail,
    TemplateVersionMetadata,
)


def _parse_jsonb(value: Any) -> Any:
    """asyncpg may hand JSONB back as str or already-decoded object."""
    if value is not None and isinstance(value, str):
        return json.loads(value)
    return value


def decode_template_version(
    result: Optional[asyncpg.Record],
) -> Optional[TemplateVersionDetail]:
    if not result:
        return None

    return TemplateVersionDetail(
        id=str(result["id"]),
        template_id=str(result["template_id"]),
        version_number=result["version_number"],
        name=result["name"],
        flow=_parse_jsonb(result.get("flow")) or {},
        configurations=_parse_jsonb(result.get("configurations")),
        expected_payload_schema=_parse_jsonb(result.get("expected_payload_schema")),
        expected_callback_response_schema=_parse_jsonb(
            result.get("expected_callback_response_schema")
        ),
        updated_by=result.get("updated_by"),
        change_source=result["change_source"],
        restored_from=result.get("restored_from"),
        created_at=result["created_at"],
    )


def decode_template_version_metadata_list(
    result: Optional[List[asyncpg.Record]],
) -> List[TemplateVersionMetadata]:
    if not result:
        return []

    return [
        TemplateVersionMetadata(
            id=str(row["id"]),
            template_id=str(row["template_id"]),
            version_number=row["version_number"],
            name=row["name"],
            updated_by=row.get("updated_by"),
            change_source=row["change_source"],
            restored_from=row.get("restored_from"),
            created_at=row["created_at"],
        )
        for row in result
    ]
