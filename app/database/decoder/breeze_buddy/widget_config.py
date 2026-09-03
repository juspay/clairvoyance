"""Row → Pydantic decoder for widget_config (migration 030)."""

import json
from typing import Any, Dict

from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse


def _appearance(value: Any) -> Dict[str, Any]:
    """asyncpg hands jsonb back as a JSON *string* unless a codec is
    registered, and the column is only guaranteed present on rows written
    since migration 055 — so accept str, dict, and absent alike. A blob that
    won't parse decodes to {} rather than failing the whole row: appearance
    is cosmetic, and the caller's fallback is the right answer for it."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def decode_widget_config(row) -> WidgetConfigResponse:
    """Build WidgetConfigResponse from a widget_config asyncpg Record."""
    return WidgetConfigResponse(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        public_widget_key=row["public_widget_key"],
        template_id=str(row["template_id"]),
        allowed_origins=list(row.get("allowed_origins") or []),
        max_sessions_per_ip_hour=row["max_sessions_per_ip_hour"],
        max_messages_per_ip_hour=row["max_messages_per_ip_hour"],
        max_concurrent_per_ip=row["max_concurrent_per_ip"],
        max_voice_sessions_per_ip_hour=row["max_voice_sessions_per_ip_hour"],
        appearance=_appearance(row.get("appearance")),
        active=row.get("active", True),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
