"""
Decoder functions for templates.
"""

import json
from typing import List, Optional

import asyncpg

from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)


def decode_template(result: asyncpg.Record) -> Optional[TemplateModel]:
    """
    Decode a single template from database result.
    """
    if not result:
        return None

    # Parse flow from JSON
    flow_data = result.get("flow", {})

    if flow_data and isinstance(flow_data, str):
        # If it's a string, parse it
        flow_data = json.loads(flow_data)

    # Parse expected_payload_schema from JSON
    expected_payload_schema_data = result.get("expected_payload_schema")

    # Parse expected_callback_response_schema from JSON
    expected_callback_response_schema_data = result.get(
        "expected_callback_response_schema"
    )

    if expected_payload_schema_data and isinstance(expected_payload_schema_data, str):
        # If it's a string, parse it
        expected_payload_schema_data = json.loads(expected_payload_schema_data)

    if expected_callback_response_schema_data and isinstance(
        expected_callback_response_schema_data, str
    ):
        # If it's a string, parse it
        expected_callback_response_schema_data = json.loads(
            expected_callback_response_schema_data
        )

    return TemplateModel(
        id=str(result["id"]),
        merchant_id=result["merchant_id"],
        shop_identifier=result["shop_identifier"],
        name=result["name"],
        flow=flow_data,
        expected_payload_schema=expected_payload_schema_data,
        expected_callback_response_schema=expected_callback_response_schema_data,
        is_active=result["is_active"],
        is_approved=result["is_approved"],
    )


def decode_templates(result: List[asyncpg.Record]) -> List[TemplateModel]:
    """
    Decode templates from database result.
    """
    if not result:
        return []

    templates: List[TemplateModel] = []
    for row in result:
        t = decode_template(row)
        if t is not None:
            templates.append(t)
    return templates
