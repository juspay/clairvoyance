"""Decoder for template_version rows."""

from typing import Any

from app.schemas.breeze_buddy.template_version import TemplateVersionMetadata


def decode_template_version_meta(row: Any) -> TemplateVersionMetadata:
    return TemplateVersionMetadata(
        template_id=str(row["template_id"]),
        version=row["version"],
        change_source=row["change_source"],
        bulk_op_id=(str(row["bulk_op_id"]) if row.get("bulk_op_id") else None),
        changed_by=row.get("changed_by"),
        created_at=row["created_at"],
    )
