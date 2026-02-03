"""
Database accessor functions for templates.
"""

import json
from typing import Any, Dict, List, Optional

import asyncpg

from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)
from app.core.logger import logger
from app.database.decoder.breeze_buddy.template import decode_template
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.call_execution_config import (
    get_merchant_id_by_shop_identifier_from_config_query,
)
from app.database.queries.breeze_buddy.template import (
    create_template_query,
    get_all_templates_by_outbound_number_id_query,
    get_template_by_id_query,
    get_template_by_merchant_query,
    get_template_by_outbound_number_id_query,
    get_templates_list_query,
    replace_template_query,
)
from app.schemas.breeze_buddy.template import TemplateMetadata


def get_row_count(result: Optional[list[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def get_template_by_merchant(
    merchant_id: str,
    shop_identifier: Optional[str] = None,
    name: Optional[str] = None,
    should_prioritize_merchant_specific: bool = True,
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
            if decoded_result:
                logger.info(f"Template found: {decoded_result.id} with flow structure")
            else:
                logger.info(f"Template decoding failed for merchant: {merchant_id}")
            return decoded_result

        # If no template found with shop_identifier, retry with shop_identifier=None
        if shop_identifier is not None and should_prioritize_merchant_specific:
            logger.info(
                f"No template found with shop_identifier: {shop_identifier}, retrying with shop_identifier=None"
            )
            query, values = get_template_by_merchant_query(merchant_id, None, name)
            result = await run_parameterized_query(query, values)

            if result and get_row_count(result) > 0:
                decoded_result = decode_template(result[0])
                if decoded_result:
                    logger.info(
                        f"Template found: {decoded_result.id} with flow structure (shop_identifier=None)"
                    )
                else:
                    logger.info(f"Template decoding failed for merchant: {merchant_id}")
                return decoded_result

        logger.info(f"No template found for merchant: {merchant_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting template by merchant: {e}")
        return None


async def create_template(
    template_id: str,
    merchant: str,
    identifier: Optional[str],
    name: str,
    flow: dict,
    expected_payload_schema: Optional[dict],
    expected_callback_response_schema: Optional[dict],
    now,
    configurations: Optional[dict] = None,
    secrets: Optional[dict] = None,
    outbound_number_id: Optional[str] = None,
    is_active: bool = True,
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

        # Convert configurations to JSON string
        configurations_json = json.dumps(configurations) if configurations else None

        # Convert secrets to JSON string
        secrets_json = json.dumps(secrets) if secrets else None

        query, values = create_template_query(
            template_id,
            merchant,
            identifier,
            name,
            flow_json,
            expected_payload_schema_json,
            expected_callback_response_schema_json,
            configurations_json,
            secrets_json,
            outbound_number_id,  # Moved: now matches SQL column order
            is_active,
            now,
            now,
        )

        result = await run_parameterized_query(query, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])
            if decoded_result:
                logger.info(f"Template created successfully: {decoded_result.id}")
            else:
                logger.error("Template decoding failed after creation")
            return decoded_result

        logger.error("Failed to create template")
        return None

    except Exception as e:
        logger.error(f"Error creating template: {e}")
        return None


async def get_templates_list(filters: Dict[str, Any]) -> List[TemplateMetadata]:
    """
    Get list of templates (metadata only, no flow) based on filters.

    Implements fallback mechanism: if searching by shop_identifier and no results found,
    falls back to merchant-level (generic) templates where shop_identifier IS NULL.

    Auto-detects when merchant_id looks like a shop identifier (contains domain) and
    resolves it to the actual parent merchant_id.

    Args:
        filters: Dictionary containing:
            - merchant_ids (optional): List of merchant IDs to filter by
            - shop_identifiers (optional): List of shop identifiers to filter by
            - is_active (optional): Filter by active status
            - merchant_id (optional): Single merchant ID to filter by
            - shop_identifier (optional): Single shop identifier to filter by

    Returns:
        List of TemplateMetadata objects
    """
    logger.info(f"Getting templates list with filters: {filters}")

    try:
        # Auto-detect if merchant_id is actually a shop_identifier (contains domain-like pattern)
        if "merchant_id" in filters and filters["merchant_id"]:
            merchant_id_value = filters["merchant_id"]
            # Check if it looks like a shop identifier (contains domain patterns)
            if "." in merchant_id_value and (
                "myshopify.com" in merchant_id_value or "http" in merchant_id_value
            ):
                logger.info(
                    f"Detected merchant_id '{merchant_id_value}' looks like shop_identifier, resolving to actual merchant_id from call_execution_config"
                )

                # Look up the actual merchant_id for this shop_identifier from call_execution_config table
                lookup_query, lookup_values = (
                    get_merchant_id_by_shop_identifier_from_config_query(
                        merchant_id_value
                    )
                )
                lookup_result = await run_parameterized_query(
                    lookup_query, lookup_values
                )

                if lookup_result and len(lookup_result) > 0:
                    actual_merchant_id = lookup_result[0]["merchant_id"]
                    logger.info(
                        f"Resolved shop '{merchant_id_value}' to merchant_id '{actual_merchant_id}'"
                    )

                    # Update filters: move merchant_id to shop_identifier and use resolved merchant_id
                    filters = {k: v for k, v in filters.items() if k != "merchant_id"}
                    filters["merchant_id"] = actual_merchant_id
                    filters["shop_identifier"] = merchant_id_value
                else:
                    logger.warning(
                        f"Could not resolve shop_identifier '{merchant_id_value}' to merchant_id"
                    )
                    # Continue with original filters, will likely return empty

        query, values = get_templates_list_query(filters)
        result = await run_parameterized_query(query, values)

        # If no results found and we're filtering by shop_identifier, try fallback to generic templates
        if not result and (
            "shop_identifier" in filters or "shop_identifiers" in filters
        ):
            logger.info(
                "No shop-specific templates found, falling back to generic merchant templates (shop_identifier IS NULL)"
            )

            # Create fallback filters without shop_identifier
            fallback_filters = {
                k: v
                for k, v in filters.items()
                if k not in ["shop_identifier", "shop_identifiers"]
            }

            # Query for generic templates (shop_identifier IS NULL)
            query, values = get_templates_list_query(fallback_filters)
            result = await run_parameterized_query(query, values)

        if not result:
            logger.info("No templates found matching filters (including fallback)")
            return []

        # Convert database records to TemplateMetadata objects
        templates = []
        for row in result:
            templates.append(
                TemplateMetadata(
                    id=str(row["id"]),  # Convert UUID to string
                    merchant_id=row["merchant_id"],
                    shop_identifier=row.get("shop_identifier"),
                    name=row["name"],
                    is_active=row["is_active"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        logger.info(f"Found {len(templates)} templates matching filters")
        return templates

    except Exception as e:
        logger.error(f"Error getting templates list: {e}", exc_info=True)
        return []


async def get_template_by_id(template_id: str) -> Optional[TemplateModel]:
    """
    Get a complete template by ID (includes full flow).

    Args:
        template_id: Template UUID

    Returns:
        TemplateModel if found, None otherwise
    """
    logger.info(f"Getting template by ID: {template_id}")

    try:
        query, values = get_template_by_id_query(template_id)
        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])
            if decoded_result:
                logger.info(f"Template found: {decoded_result.id}")
            else:
                logger.info(f"Template decoding failed for ID: {template_id}")
            return decoded_result

        logger.info(f"No template found with ID: {template_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting template by ID: {e}", exc_info=True)
        return None


async def replace_template(
    template_id: str,
    name: str,
    flow: dict,
    expected_payload_schema: Optional[dict],
    expected_callback_response_schema: Optional[dict],
    configurations: Optional[dict],
    secrets: Optional[dict],
    outbound_number_id: Optional[str],
    is_active: bool,
    shop_identifier: Optional[str],
    now,
) -> Optional[TemplateModel]:
    """
    Update an existing template.

    Args:
        template_id: Template UUID
        name: Template name (required)
        flow: Flow structure (required)
        expected_payload_schema: Expected payload schema (optional, set to NULL if not provided)
        expected_callback_response_schema: Expected callback response schema (optional, set to NULL if not provided)
        configurations: Template configurations (optional, set to NULL if not provided)
        secrets: Secrets and variables for HTTP functions (optional, set to NULL if not provided)
        outbound_number_id: Outbound number ID (optional, set to NULL if not provided)
        is_active: Whether template is active (required)
        shop_identifier: Shop identifier (optional, set to NULL if not provided)
        now: Current timestamp

    Returns:
        Updated TemplateModel if successful, None otherwise
    """
    logger.info(f"Updating template with ID: {template_id}")

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

        # Convert configurations to JSON string
        configurations_json = json.dumps(configurations) if configurations else None

        # Convert secrets to JSON string
        secrets_json = json.dumps(secrets) if secrets else None

        query, values = replace_template_query(
            template_id,
            name,
            flow_json,
            expected_payload_schema_json,
            expected_callback_response_schema_json,
            configurations_json,
            secrets_json,
            outbound_number_id,
            is_active,
            shop_identifier,
            now,
        )

        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])
            if decoded_result:
                logger.info(f"Template updated successfully: {decoded_result.id}")
            else:
                logger.error("Template decoding failed after update")
            return decoded_result

        logger.error(f"Failed to update template: {template_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating template: {e}", exc_info=True)
        return None


async def get_template_by_outbound_number_id(
    outbound_number_id: str,
    enable_inbound_only: bool = False,
) -> Optional[TemplateModel]:
    """
    Get a template by outbound_number_id.

    Args:
        outbound_number_id: Outbound number UUID
        enable_inbound_only: If True, only return templates with
                             configurations.enable_inbound = true

    Returns:
        TemplateModel if found, None otherwise
    """
    logger.info(f"Getting template by outbound_number_id: {outbound_number_id}")

    try:
        query, values = get_template_by_outbound_number_id_query(
            outbound_number_id, enable_inbound_only
        )
        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_template(result[0])
            if decoded_result:
                logger.info(f"Template found: {decoded_result.id}")
            else:
                logger.info(
                    f"Template decoding failed for outbound_number_id: {outbound_number_id}"
                )
            return decoded_result

        logger.info(f"No template found with outbound_number_id: {outbound_number_id}")
        return None

    except Exception as e:
        logger.error(
            f"Error getting template by outbound_number_id: {e}", exc_info=True
        )
        return None


async def get_all_templates_by_outbound_number_id(
    outbound_number_id: str,
) -> List[TemplateModel]:
    """
    Get ALL templates by outbound_number_id.
    Used for IVR to list all available templates for a phone number.

    Args:
        outbound_number_id: Outbound number UUID

    Returns:
        List of TemplateModel (empty list if none found)
    """
    logger.info(f"Getting all templates by outbound_number_id: {outbound_number_id}")

    try:
        query, values = get_all_templates_by_outbound_number_id_query(
            outbound_number_id
        )
        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            templates = [
                t for t in (decode_template(row) for row in result) if t is not None
            ]
            logger.info(
                f"Found {len(templates)} templates for outbound_number_id: {outbound_number_id}"
            )
            return templates

        logger.info(f"No templates found with outbound_number_id: {outbound_number_id}")
        return []

    except Exception as e:
        logger.error(
            f"Error getting all templates by outbound_number_id: {e}", exc_info=True
        )
        return []
