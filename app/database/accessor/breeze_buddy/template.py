"""
Database accessor functions for templates.
"""

import json
from typing import Optional

import asyncpg

from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)
from app.core.logger import logger
from app.database.decoder.breeze_buddy.template import decode_template
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.template import (
    create_template_query,
    get_template_by_merchant_query,
    update_template_approval_query,
)


def get_row_count(result: Optional[list[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def get_template_by_merchant(
    merchant_id: str, shop_identifier: str = None, name: str = None
) -> Optional[TemplateModel]:
    """Get a template by merchant ID and optional filters."""
    logger.info(f"Getting template by merchant: {merchant_id}")

    try:
        query, values = get_template_by_merchant_query(
            merchant_id, shop_identifier, name
        )
        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])

            logger.info(f"Template found: {decoded_result.id} with flow structure")
            return decoded_result

        # If no template found with shop_identifier, retry with shop_identifier=None
        if shop_identifier is not None:
            logger.info(
                f"No template found with shop_identifier: {shop_identifier}, retrying with shop_identifier=None"
            )
            query, values = get_template_by_merchant_query(merchant_id, None, name)
            result = await run_parameterized_query(query, values)

            if result and get_row_count(result) > 0:
                decoded_result = decode_template(result[0])

                logger.info(
                    f"Template found: {decoded_result.id} with flow structure (shop_identifier=None)"
                )
                return decoded_result

        logger.info(f"No template found for merchant: {merchant_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting template by merchant: {e}")
        return None


async def create_template(
    template_id: str,
    merchant: str,
    identifier: str,
    name: str,
    flow: dict,
    expected_payload_schema: Optional[dict],
    expected_callback_response_schema: Optional[dict],
    is_active: bool,
    now,
) -> Optional[TemplateModel]:
    """Create a new template with flow stored as JSON."""
    logger.info(f"Creating template with ID: {template_id}")

    try:
        # Convert flow to JSON string
        flow_json = json.dumps(flow)
        expected_payload_schema_json = (
            json.dumps(expected_payload_schema) if expected_payload_schema else None
        )
        expected_callback_response_schema_json = (
            json.dumps(expected_callback_response_schema)
            if expected_callback_response_schema
            else None
        )

        query, values = create_template_query(
            template_id,
            merchant,
            identifier,
            name,
            flow_json,
            expected_payload_schema_json,
            expected_callback_response_schema_json,
            is_active,
            now,
            now,
        )

        result = await run_parameterized_query(query, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])
            logger.info(f"Template created successfully: {decoded_result.id}")
            return decoded_result

        logger.error("Failed to create template")
        return None

    except Exception as e:
        logger.error(f"Error creating template: {e}")
        return None


async def update_template_approval(
    template_id: str,
    is_approved: bool,
    now,
) -> Optional[TemplateModel]:
    """Update approval status for a template."""
    logger.info(f"Updating template approval for template ID: {template_id}")

    try:
        query, values = update_template_approval_query(template_id, is_approved, now)
        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])
            logger.info(
                f"Template approval updated successfully: {decoded_result.id}"
            )
            return decoded_result

        logger.error("Failed to update template approval")
        return None
    except Exception as e:
        logger.error(f"Error updating template approval: {e}")
        return None
