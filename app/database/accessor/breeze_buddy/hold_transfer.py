"""
Database accessor for hold-transfer specific queries.

Returns lightweight dicts instead of full Pydantic models (TemplateModel)
to avoid importing from ai.voice.agents.breeze_buddy.template.types,
which would create a circular dependency with the handler layer.
"""

import json
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.template import (
    get_template_by_telephony_number_id_query,
)


async def get_template_summary_by_telephony_number_id(
    telephony_number_id: str,
    enable_inbound_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """Get a template summary by telephony_number_id.

    Returns a plain dict instead of TemplateModel so this module never
    imports from the ai.voice layer — avoiding the circular dependency:
      accessor.template → template.types → template.__init__ → template.builder
      → handlers.internal.__init__ → builtin_dispatcher → hold_and_consult
      → accessor.template

    Args:
        telephony_number_id: Telephony number UUID.
        enable_inbound_only: If True, only return templates with
                             configurations.enable_inbound = true.

    Returns:
        Dict with keys: id, name, reseller_id, merchant_id, expected_payload_schema.
        None if no template found or on error.
    """
    logger.info(
        f"Getting template summary by telephony_number_id: {telephony_number_id}"
    )

    try:
        query, values = get_template_by_telephony_number_id_query(
            telephony_number_id, enable_inbound_only
        )
        result = await run_parameterized_query(query, values)

        if result and len(result) > 0:
            row = result[0]
            raw_schema = row.get("expected_payload_schema")
            summary = {
                "id": row["id"],
                "name": row["name"],
                "reseller_id": row["reseller_id"],
                "merchant_id": row["merchant_id"],
                "expected_payload_schema": (
                    json.loads(raw_schema)
                    if isinstance(raw_schema, str)
                    else raw_schema
                ),
            }
            logger.info(f"Template summary found: {summary['id']}")
            return summary

        logger.info(
            f"No template found with telephony_number_id: {telephony_number_id}"
        )
        return None

    except Exception as e:
        logger.error(
            f"Error getting template summary by telephony_number_id: {e}",
            exc_info=True,
        )
        return None
