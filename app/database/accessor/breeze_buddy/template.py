"""
Database accessor functions for templates.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
    WorkflowType,
)
from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.template import decode_template
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.call_execution_config import (
    get_reseller_id_by_merchant_identifier_from_config_query,
)
from app.database.queries.breeze_buddy.template import (
    check_template_usage_query,
    create_template_query,
    delete_template_if_not_referenced_query,
    get_all_templates_by_outbound_number_id_query,
    get_template_by_id_query,
    get_template_by_outbound_number_id_query,
    get_template_in_scope_query,
    get_templates_count_query,
    get_templates_list_query,
    replace_template_query,
    select_templates_by_scope_query,
    update_template_configurations_query,
)
from app.schemas.breeze_buddy.template import (
    BatchConfigResponse,
    BatchConfigResultItem,
    TemplateMetadata,
)


def get_row_count(result: Optional[list[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def get_template_in_scope(
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
) -> Optional[TemplateModel]:
    """Get a template by its exact (reseller, merchant, name) scope.

    merchant_id=None matches only reseller-level rows (merchant_id IS NULL).
    There is deliberately NO merchant→reseller fallback: runtime resolution
    is id-only (get_template_by_id); this exists for uniqueness checks and
    fixed internal lookups.
    """
    try:
        query, values = get_template_in_scope_query(reseller_id, merchant_id, name)
        result = await run_parameterized_query(query, values)

        if result and get_row_count(result) > 0:
            return decode_template(result[0])
        return None

    except Exception as e:
        logger.error(f"Error getting template in scope for {reseller_id}: {e}")
        return None


async def create_template(
    template_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    flow: dict,
    expected_payload_schema: Optional[dict],
    expected_callback_response_schema: Optional[dict],
    now,
    configurations: Optional[dict] = None,
    secrets: Optional[dict] = None,
    outbound_number_id: Optional[str] = None,
    is_active: bool = True,
    supported_channels: Optional[List[str]] = None,
    workflow: Optional[WorkflowType] = None,
) -> Optional[TemplateModel]:
    """Create a new template with flow stored as JSON."""
    logger.info(f"Creating template with ID: {template_id}")

    try:
        # Convert flow to JSON string
        flow_json = json.dumps(flow)
        expected_payload_schema_json = (
            json.dumps(expected_payload_schema)
            if expected_payload_schema is not None
            else None
        )
        expected_callback_response_schema_json = (
            json.dumps(expected_callback_response_schema)
            if expected_callback_response_schema is not None
            else None
        )

        # Convert configurations to JSON string
        configurations_json = (
            json.dumps(configurations) if configurations is not None else None
        )

        # Convert secrets to JSON string
        secrets_json = json.dumps(secrets) if secrets is not None else None

        workflow_value = (
            workflow.value if workflow is not None else WorkflowType.NON_SHOPIFY.value
        )

        query, values = create_template_query(
            template_id,
            reseller_id,
            merchant_id,
            name,
            flow_json,
            expected_payload_schema_json,
            expected_callback_response_schema_json,
            configurations_json,
            secrets_json,
            outbound_number_id,  # Moved: now matches SQL column order
            is_active,
            supported_channels or ["voice"],
            now,
            now,
            workflow=workflow_value,
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


async def get_templates_list(
    filters: Dict[str, Any],
) -> Tuple[List[TemplateMetadata], int]:
    """
    Get list of templates (metadata only, no flow) based on filters.

    Implements fallback mechanism: if searching by merchant_id and no results found,
    falls back to reseller-level (generic) templates where merchant_id IS NULL.

    Auto-detects when reseller_id looks like a merchant_id (contains domain) and
    resolves it to the actual parent reseller_id.

    Args:
        filters: Dictionary containing:
            - reseller_ids (optional): List of reseller IDs to filter by
            - merchant_ids (optional): List of merchant identifiers to filter by
            - is_active (optional): Filter by active status
            - reseller_id (optional): Single reseller ID to filter by
            - merchant_id (optional): Single merchant identifier to filter by

    Returns:
        List of TemplateMetadata objects
    """
    logger.info(f"Getting templates list with filters: {filters}")

    try:
        # Auto-detect if reseller_id is actually a merchant_id (contains domain-like pattern)
        if "reseller_id" in filters and filters["reseller_id"]:
            reseller_id_value = filters["reseller_id"]
            # Check if it looks like a merchant identifier (contains domain patterns)
            if "." in reseller_id_value and (
                "myshopify.com" in reseller_id_value or "http" in reseller_id_value
            ):
                logger.info(
                    f"Detected reseller_id '{reseller_id_value}' looks like merchant_id, resolving to actual reseller_id from call_execution_config"
                )

                # Look up the actual reseller_id for this merchant_id from call_execution_config table
                lookup_query, lookup_values = (
                    get_reseller_id_by_merchant_identifier_from_config_query(
                        reseller_id_value
                    )
                )
                lookup_result = await run_parameterized_query(
                    lookup_query, lookup_values
                )

                if lookup_result and len(lookup_result) > 0:
                    actual_reseller_id = lookup_result[0]["reseller_id"]
                    logger.info(
                        f"Resolved merchant '{reseller_id_value}' to reseller_id '{actual_reseller_id}'"
                    )

                    # Update filters: move reseller_id to merchant_id and use resolved reseller_id
                    filters = {k: v for k, v in filters.items() if k != "reseller_id"}
                    filters["reseller_id"] = actual_reseller_id
                    filters["merchant_id"] = reseller_id_value
                else:
                    logger.warning(
                        f"Could not resolve merchant_id '{reseller_id_value}' to reseller_id"
                    )
                    # Continue with original filters, will likely return empty

        effective_filters = filters
        query, values = get_templates_list_query(filters)
        result = await run_parameterized_query(query, values)

        # If no results found and we're filtering by merchant_id, try fallback
        # to the reseller's generic templates. Two guards (2026-07-13 fix —
        # an admin console scoped to an empty merchant used to get EVERY
        # template on the platform back):
        #   1. Only fall back when a reseller constraint exists — with no
        #      reseller filter (admin wildcard), dropping the merchant filter
        #      would unscope the query entirely; empty means empty.
        #   2. The fallback pins merchant_id IS NULL instead of merely
        #      removing the merchant filter, so it returns the reseller's
        #      generic templates — not other merchants' copies.
        has_reseller_scope = bool(
            filters.get("reseller_id") or filters.get("reseller_ids")
        )
        if (
            not result
            and has_reseller_scope
            and ("merchant_id" in filters or "merchant_ids" in filters)
        ):
            logger.info(
                "No merchant-specific templates found, falling back to generic reseller templates (merchant_id IS NULL)"
            )

            fallback_filters = {
                k: v
                for k, v in filters.items()
                if k not in ["merchant_id", "merchant_ids"]
            }
            fallback_filters["merchant_id_is_null"] = True

            # Query for generic templates (merchant_id IS NULL)
            effective_filters = fallback_filters
            query, values = get_templates_list_query(fallback_filters)
            result = await run_parameterized_query(query, values)

        if not result:
            logger.info("No templates found matching filters (including fallback)")
            return [], 0

        # Convert database records to TemplateMetadata objects
        templates = []
        for row in result:
            templates.append(
                TemplateMetadata(
                    id=str(row["id"]),  # Convert UUID to string
                    reseller_id=row["reseller_id"],
                    merchant_id=row.get("merchant_id"),
                    name=row["name"],
                    workflow=row.get("workflow") or "non-shopify",
                    is_active=row["is_active"],
                    supported_channels=list(row["supported_channels"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        # Total for pagination: when a limit is set, `result` is a single page,
        # so COUNT(*) gives the real total; otherwise the page is everything.
        if effective_filters.get("limit") is not None:
            count_query, count_values = get_templates_count_query(effective_filters)
            count_result = await run_parameterized_query(count_query, count_values)
            total = count_result[0]["total"] if count_result else len(templates)
        else:
            total = len(templates)

        logger.info(f"Found {len(templates)} templates (total={total})")
        return templates, total

    except Exception as e:
        logger.error(f"Error getting templates list: {e}", exc_info=True)
        return [], 0


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
    reseller_id: str,
    name: str,
    flow: dict,
    expected_payload_schema: Optional[dict],
    expected_callback_response_schema: Optional[dict],
    configurations: Optional[dict],
    secrets: Optional[dict],
    outbound_number_id: Optional[str],
    is_active: bool,
    merchant_id: Optional[str],
    now,
    supported_channels: Optional[List[str]] = None,
) -> Optional[TemplateModel]:
    """
    Update an existing template.

    Args:
        template_id: Template UUID
        reseller_id: Reseller identifier (required, caller resolves omitted values
            to the persisted one)
        name: Template name (required)
        flow: Flow structure (required)
        expected_payload_schema: Expected payload schema (optional, set to NULL if not provided)
        expected_callback_response_schema: Expected callback response schema (optional, set to NULL if not provided)
        configurations: Template configurations (optional, set to NULL if not provided)
        secrets: Secrets and variables for HTTP functions (optional, set to NULL if not provided)
        outbound_number_id: Outbound number ID (optional, set to NULL if not provided)
        is_active: Whether template is active (required)
        merchant_id: Merchant identifier (optional, set to NULL if not provided)
        now: Current timestamp

    Returns:
        Updated TemplateModel if successful, None otherwise
    """
    logger.info(f"Updating template with ID: {template_id}")

    try:
        # Convert flow to JSON string
        flow_json = json.dumps(flow)
        expected_payload_schema_json = (
            json.dumps(expected_payload_schema)
            if expected_payload_schema is not None
            else None
        )
        expected_callback_response_schema_json = (
            json.dumps(expected_callback_response_schema)
            if expected_callback_response_schema is not None
            else None
        )

        # Convert configurations to JSON string
        configurations_json = (
            json.dumps(configurations) if configurations is not None else None
        )

        # Convert secrets to JSON string
        secrets_json = json.dumps(secrets) if secrets is not None else None

        query, values = replace_template_query(
            template_id,
            reseller_id,
            name,
            flow_json,
            expected_payload_schema_json,
            expected_callback_response_schema_json,
            configurations_json,
            secrets_json,
            outbound_number_id,
            is_active,
            merchant_id,
            supported_channels or ["voice"],
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


async def check_template_usage(template_id: str) -> Dict[str, int]:
    """
    Check if a template is referenced by other tables.

    Checks:
    - call_execution_config: configs that reference this template
    - lead_call_tracker: active leads (BACKLOG, RETRY, PROCESSING) using this template

    Args:
        template_id: Template UUID

    Returns:
        Dict mapping source table name to reference count.
        Example: {"call_execution_config": 2, "lead_call_tracker": 0}
    """
    logger.info(f"Checking usage for template: {template_id}")

    try:
        query, values = check_template_usage_query(template_id)
        result = await run_parameterized_query(query, values)

        usage: Dict[str, int] = {}
        if result:
            for row in result:
                usage[row["source"]] = row["reference_count"]

        logger.info(f"Template {template_id} usage: {usage}")
        return usage

    except Exception as e:
        logger.error(f"Error checking template usage: {e}", exc_info=True)
        return {}


async def delete_template_if_not_referenced(
    template_id: str,
) -> Optional[TemplateMetadata]:
    """
    Atomically delete a template only if it is not referenced by call_execution_config or active leads.
    Returns the deleted TemplateMetadata if successful, None otherwise.
    """
    logger.info(f"Attempting atomic delete for template: {template_id}")
    try:
        query, values = delete_template_if_not_referenced_query(template_id)
        result = await run_parameterized_query(query, values)
        if result and get_row_count(result) > 0:
            row = result[0]
            deleted = TemplateMetadata(
                id=str(row["id"]),
                reseller_id=row["reseller_id"],
                merchant_id=row.get("merchant_id"),
                name=row["name"],
                workflow=row.get("workflow") or "non-shopify",
                is_active=row["is_active"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            logger.info(f"Atomic delete succeeded for template: {deleted.id}")
            return deleted
        logger.info(
            f"Atomic delete failed (template in use or not found): {template_id}"
        )
        return None
    except Exception as e:
        logger.error(f"Error in atomic delete for template: {e}", exc_info=True)
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


_MISSING = object()


def _resolve_config_path(config: Dict[str, Any], path_parts: List[str]) -> Any:
    """Return the value at a dotted path inside a configurations dict, or
    ``_MISSING`` if any segment is absent."""
    current: Any = config
    for part in path_parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


async def batch_update_template_configurations(
    filters: Dict[str, Any],
    patches: Dict[str, Any],
    create: bool,
    dry_run: bool,
) -> BatchConfigResponse:
    """Apply a flat map of dotted configuration paths to every template
    matching the given scope.

    Only the ``configurations`` JSONB is touched (never flow/secrets). When
    ``dry_run`` is True nothing is written; the returned per-template ``keys``
    map previews the classification of each requested path.

    Args:
        filters: Scope filters consumed by select_templates_by_scope_query
            (reseller/merchant scope, optional template_name, optional workflows).
        patches: ``{"dotted.path": value}`` to apply.
        create: Create keys that do not already exist (otherwise skip them).
        dry_run: Preview only; perform no writes.
    """
    logger.info(
        f"Batch config update (dry_run={dry_run}, create={create}) "
        f"filters={filters} keys={list(patches.keys())}"
    )

    query, values = select_templates_by_scope_query(filters)
    rows = await run_parameterized_query(query, values)

    results: List[BatchConfigResultItem] = []
    total_patched = 0
    pending_updates: List[Tuple[str, List[Any]]] = []

    for row in rows or []:
        configurations = row.get("configurations")
        if isinstance(configurations, str):
            configurations = json.loads(configurations)
        if not isinstance(configurations, dict):
            configurations = {}

        keys_status: Dict[str, str] = {}
        set_operations: List[Any] = []

        for dotted_path, new_value in patches.items():
            path_parts = dotted_path.split(".")
            current = _resolve_config_path(configurations, path_parts)

            if current is _MISSING:
                if create:
                    keys_status[dotted_path] = "created"
                    set_operations.append((path_parts, json.dumps(new_value)))
                else:
                    keys_status[dotted_path] = "skipped_not_present"
            elif current == new_value:
                keys_status[dotted_path] = "unchanged_same_value"
            else:
                keys_status[dotted_path] = "patched"
                set_operations.append((path_parts, json.dumps(new_value)))

        if set_operations:
            total_patched += 1
            if not dry_run:
                pending_updates.append(
                    update_template_configurations_query(
                        str(row["id"]), set_operations, create
                    )
                )

        results.append(
            BatchConfigResultItem(
                id=str(row["id"]),
                reseller_id=row["reseller_id"],
                merchant_id=row.get("merchant_id"),
                name=row["name"],
                workflow=row.get("workflow") or "non-shopify",
                keys=keys_status,
            )
        )

    # Apply every matched template's update inside one transaction so the batch
    # is all-or-nothing -- a failure midway rolls back earlier writes.
    if pending_updates:
        async for conn in get_db_connection():
            async with conn.transaction():
                for update_query, update_values in pending_updates:
                    await conn.execute(update_query, *update_values)
            break

    logger.info(
        f"Batch config update matched {len(results)} templates, "
        f"{total_patched} with changes (dry_run={dry_run})"
    )

    return BatchConfigResponse(
        dry_run=dry_run,
        total_templates=len(results),
        total_patched=total_patched,
        results=results,
    )
