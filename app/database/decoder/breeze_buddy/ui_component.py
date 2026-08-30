"""Row → Pydantic decoder for ui_component (migration 057)."""

import json
from typing import Any, Dict, Optional

from app.schemas.breeze_buddy.ui_component import UiComponentResponse


def _jsonb(value: Any) -> Optional[Dict[str, Any]]:
    """asyncpg returns JSONB as str without a codec — accept both."""
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def decode_ui_component(row) -> UiComponentResponse:
    """Build UiComponentResponse from a ui_component asyncpg Record."""
    return UiComponentResponse(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        name=row["name"],
        version=row["version"],
        props_schema=_jsonb(row["props_schema"]) or {},
        flags=_jsonb(row["flags"]) or {},
        render_def=_jsonb(row["render_def"]),
        prompt_hint=row["prompt_hint"],
        is_active=row.get("is_active", True),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
