"""
Decoder functions for blueprint sessions.
"""

from __future__ import annotations

from typing import Optional

from app.core.logger import logger
from app.schemas.blueprint.session import BlueprintSessionModel


def decode_session(row) -> Optional[BlueprintSessionModel]:
    """
    Decode a single blueprint session from a database row.
    """
    try:
        return BlueprintSessionModel(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            reseller_id=row["reseller_id"],
            merchant_id=row.get("merchant_id"),
            mode=row["mode"],
            template_id=str(row["template_id"]) if row.get("template_id") else None,
            langgraph_thread_id=row["langgraph_thread_id"],
            current_step=row.get("current_step"),
            status=row["status"],
            result_template_id=(
                str(row["result_template_id"])
                if row.get("result_template_id")
                else None
            ),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            expires_at=row.get("expires_at"),
        )
    except Exception as e:
        logger.error(f"Error decoding blueprint session: {e}")
        return None
