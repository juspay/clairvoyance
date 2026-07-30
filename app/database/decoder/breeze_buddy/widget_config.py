"""Row → Pydantic decoder for widget_config (migration 030)."""

from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse


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
        human_assist_enabled=row.get("human_assist_enabled", False),
        human_assist_platform=row.get("human_assist_platform") or "native",
        active=row.get("active", True),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
