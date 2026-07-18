"""
Business logic handlers for template operations.
All handlers perform database operations and enforce business rules.
"""

import copy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.cache import invalidate_template
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    CreateTemplateRequest,
    FlowMode,
    ReplaceTemplateRequest,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.utils.secrets import (
    mask_template_secrets,
    merge_masked_mcp_auth,
    merge_secrets,
)
from app.core.logger import logger
from app.database.accessor import get_outbound_number_by_id, get_template_in_scope
from app.database.accessor.breeze_buddy.template import (
    check_template_usage,
    create_template,
    delete_template_if_not_referenced,
    get_template_by_id,
    get_templates_list,
    replace_template,
)
from app.database.accessor.breeze_buddy.template_version import (
    get_template_version_by_number,
    list_template_versions,
)
from app.database.decoder.breeze_buddy.template import _migrate_legacy_voice_config
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template import (
    DeleteTemplateResponse,
    RollbackTemplateResponse,
    TemplateListResponse,
    TemplateVersionDetail,
    TemplateVersionListResponse,
)

from .rbac import apply_hierarchical_template_filters, validate_template_access


def _validate_flow_shape(flow: Optional[Dict[str, Any]]) -> None:
    """Shared flow-shape validation for create / replace / rollback.

    Direct-mode flows have a flat ``system_prompt`` + ``functions`` list and
    no ``initial_node`` / ``nodes`` (see template/builder.py
    ``_build_direct_flow_config``); node-mode requires both.
    """
    if not flow:
        raise ValueError("Flow structure is required")
    if flow.get("mode") == FlowMode.DIRECT.value:
        if "system_prompt" not in flow:
            raise ValueError(
                "system_prompt must be specified in direct-mode flow structure"
            )
    else:
        if "initial_node" not in flow:
            raise ValueError("initial_node must be specified in flow structure")
        if "nodes" not in flow or not flow["nodes"]:
            raise ValueError("nodes must be specified in flow structure")


async def create_template_handler(
    template_data: CreateTemplateRequest, current_user: UserInfo
):
    """
    Create a new template.

    Args:
        template_data: Template creation request
        current_user: Current authenticated user

    Returns:
        Success response with template ID

    Raises:
        HTTPException: 409 if template exists, 400/500 on error
    """

    logger.info(
        f"User {current_user.username} (role: {current_user.role}) creating template "
        f"for reseller: {template_data.reseller_id}, name: {template_data.name}"
    )

    try:
        # Validate flow structure
        flow = template_data.flow
        _validate_flow_shape(flow)

        # Check if template already exists
        existing = await get_template_in_scope(
            template_data.reseller_id,
            template_data.merchant_id,
            template_data.name,
        )

        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Template already exists for reseller {template_data.reseller_id} "
                f"and template name: {template_data.name}",
            )

        # Validate outbound_number_id if provided
        if template_data.outbound_number_id:
            outbound_number = await get_outbound_number_by_id(
                template_data.outbound_number_id
            )
            if not outbound_number:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Outbound number with ID {template_data.outbound_number_id} does not exist",
                )

        # Create the template
        now = datetime.now(timezone.utc)

        # Build configurations dict from the ConfigurationModel.
        # mode="json" + reveal_secrets context unwraps SecretStr fields
        # (e.g. mcp.servers[*].auth.token) to their underlying string so
        # the downstream json.dumps in ``create_template`` writes the
        # real value (typically a ``{credential_name}`` placeholder).
        # Without the context flag, HttpAuthConfig's serializer falls
        # back to the masked "**********" form, which is what we want
        # everywhere except this persistence path.
        configurations = None
        if template_data.configurations:
            configurations = template_data.configurations.model_dump(
                exclude_none=True,
                mode="json",
                context={"reveal_secrets": True},
            )

        snapshot_configurations = (
            template_data.configurations.model_dump(exclude_none=True, mode="json")
            if template_data.configurations
            else None
        )

        template, version_number = await create_template(
            template_id=str(uuid4()),
            reseller_id=template_data.reseller_id,
            merchant_id=template_data.merchant_id,
            name=template_data.name,
            flow=flow,
            expected_payload_schema=template_data.expected_payload_schema,
            expected_callback_response_schema=template_data.expected_callback_response_schema,
            configurations=configurations,
            secrets=template_data.secrets,
            outbound_number_id=template_data.outbound_number_id,
            is_active=template_data.is_active,
            supported_channels=list(template_data.supported_channels),
            now=now,
            updated_by=current_user.username,
            snapshot_configurations=snapshot_configurations,
        )

        if not template:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create template",
            )

        logger.info(
            f"Successfully created template with id: {template.id} containing flow "
            f"with {len(flow.get('nodes', []))} nodes, version {version_number}"
        )

        return {
            "status": "success",
            "template_id": template.id,
            "message": f"Template '{template_data.name}' created successfully "
            f"with {len(flow.get('nodes', []))} nodes",
        }

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Validation error creating template: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating template: {str(e)}",
        )


async def list_templates_handler(
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    include_inactive: bool,
    current_user: UserInfo,
    page: Optional[int] = None,
    limit: Optional[int] = None,
    search: Optional[str] = None,
) -> TemplateListResponse:
    """
    List templates with RBAC enforcement.

    Returns metadata only (no flow) for optimal performance.

    Args:
        reseller_id: Optional reseller ID to filter by
        merchant_id: Optional merchant identifier to filter by
        include_inactive: Whether to include inactive templates
        current_user: Current authenticated user

    Returns:
        TemplateListResponse with list of template metadata

    Raises:
        HTTPException: 403 if user tries to access unauthorized merchants/shops
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting templates list: "
        f"reseller_id={reseller_id}, merchant_id={merchant_id}, include_inactive={include_inactive}"
    )

    try:
        # Build filters from query params
        filters: Dict[str, Any] = {}
        if reseller_id:
            filters["reseller_id"] = reseller_id
        if merchant_id:
            filters["merchant_id"] = merchant_id
        if not include_inactive:
            filters["is_active"] = True

        # Apply RBAC filtering (validates access and injects user's accessible merchants/shops)
        filters = apply_hierarchical_template_filters(filters, current_user)

        # Optional search + pagination. Backward-compatible: when `limit` is
        # omitted the query returns all rows (existing behavior).
        if search:
            filters["search"] = search
        current_page = page if (page and page > 0) else 1
        if limit is not None:
            filters["limit"] = limit
            filters["offset"] = (current_page - 1) * limit

        # Get templates from database (total reflects the full filtered set)
        templates, total = await get_templates_list(filters)

        logger.info(
            f"Returning {len(templates)} templates (total={total}) for user {current_user.username}"
        )

        total_pages = max(1, (total + limit - 1) // limit) if limit else 1
        return TemplateListResponse(
            templates=templates,
            total=total,
            page=current_page if limit is not None else 1,
            page_size=limit,
            total_pages=total_pages,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing templates: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing templates: {str(e)}",
        )


async def get_template_by_id_handler(template_id: str, current_user: UserInfo):
    """
    Get complete template by ID with RBAC validation.

    Returns full template including flow structure.

    Args:
        template_id: Template UUID
        current_user: Current authenticated user

    Returns:
        Complete TemplateModel with flow

    Raises:
        HTTPException: 404 if template not found, 403 if access denied
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting template by ID: {template_id}"
    )

    try:
        # Get template by ID
        template = await get_template_by_id(template_id)

        if not template:
            logger.warning(f"Template not found: {template_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template not found: {template_id}",
            )

        # Validate RBAC access
        validate_template_access(
            current_user,
            template.reseller_id,
            template.merchant_id,
            operation="access template",
        )

        logger.info(f"Returning template {template_id} to user {current_user.username}")

        return mask_template_secrets(template)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting template by ID: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting template: {str(e)}",
        )


async def replace_template_handler(
    template_id: str,
    template_data: ReplaceTemplateRequest,
    current_user: UserInfo,
):
    """
    Update an existing template.

    Args:
        template_id: Template UUID
        template_data: Template update request
        current_user: Current authenticated user

    Returns:
        Updated TemplateModel

    Raises:
        HTTPException: 404 if template not found, 400/500 on error
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) updating template "
        f"{template_id}"
    )

    try:
        # Check if template exists
        existing_template = await get_template_by_id(template_id)
        if not existing_template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template not found: {template_id}",
            )

        # Validate RBAC access
        validate_template_access(
            current_user,
            existing_template.reseller_id,
            existing_template.merchant_id,
            operation="access template",
        )

        # Resolve the target reseller. ``None`` (field omitted) preserves the
        # persisted value — older PUT clients never sent reseller_id. When the
        # template moves to a different reseller, the user must also have
        # rights on the destination.
        reseller_id = (
            template_data.reseller_id
            if template_data.reseller_id is not None
            else existing_template.reseller_id
        )
        if reseller_id != existing_template.reseller_id:
            validate_template_access(
                current_user,
                reseller_id,
                template_data.merchant_id,
                operation="move template to reseller",
            )

        # If the (reseller, merchant, name) identity changes, it must not
        # collide with another template — the DB unique indexes would
        # otherwise reject the UPDATE and surface as an opaque 500.
        identity_changed = (
            reseller_id != existing_template.reseller_id
            or template_data.merchant_id != existing_template.merchant_id
            or template_data.name != existing_template.name
        )
        if identity_changed:
            conflicting = await get_template_in_scope(
                reseller_id,
                template_data.merchant_id,
                template_data.name,
            )
            if conflicting and str(conflicting.id) != str(template_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Template already exists for reseller {reseller_id} "
                    f"and template name: {template_data.name}",
                )

        # Validate flow structure (direct mode has a different shape — see
        # the create handler for the matching branch).
        flow = template_data.flow
        _validate_flow_shape(flow)

        # Validate outbound_number_id if provided
        if template_data.outbound_number_id:
            outbound_number = await get_outbound_number_by_id(
                template_data.outbound_number_id
            )
            if not outbound_number:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Outbound number with ID {template_data.outbound_number_id} does not exist",
                )

        # Update the template
        now = datetime.now(timezone.utc)

        # Build configurations dict from the ConfigurationModel.
        # mode="json" + reveal_secrets context unwraps SecretStr fields
        # (e.g. mcp.servers[*].auth.token) to their underlying string so
        # the downstream json.dumps in ``replace_template`` writes the
        # real value (typically a ``{credential_name}`` placeholder).
        # Without the context flag, HttpAuthConfig's serializer falls
        # back to the masked "**********" form, which is what we want
        # everywhere except this persistence path.
        configurations = None
        if template_data.configurations:
            configurations = template_data.configurations.model_dump(
                exclude_none=True,
                mode="json",
                context={"reveal_secrets": True},
            )
            # GET /templates returns auth secrets as "**********" (the
            # field_serializer masks when no reveal_secrets context).
            # A typical UI does GET → edit-one-unrelated-field → PUT,
            # which sends the masked literal back. Without this merge
            # the masked literal would land in DB and silently break
            # auth (runtime would emit ``Bearer **********``). Treat
            # masked auth values as "unchanged" — same shape as the
            # ``merge_secrets`` call below for top-level secrets.
            existing_configurations = (
                existing_template.configurations.model_dump(
                    exclude_none=True,
                    mode="json",
                    context={"reveal_secrets": True},
                )
                if existing_template.configurations
                else None
            )
            configurations = merge_masked_mcp_auth(
                configurations, existing_configurations
            )

        # Snapshot the FINAL persisted configuration, masked — reconstructing
        # through ConfigurationModel makes the SecretStr serializer emit
        # "**********" for auth values (no reveal_secrets context), so the
        # history row always mirrors what actually went live, minus secrets.
        snapshot_configurations = (
            ConfigurationModel(**configurations).model_dump(
                exclude_none=True, mode="json"
            )
            if configurations
            else None
        )

        # Merge secrets: preserve **** values from existing, update real values
        merged_secrets = merge_secrets(
            incoming_secrets=template_data.secrets,
            existing_secrets=existing_template.secrets,
        )
        # Preserve persisted ``supported_channels`` when the client doesn't
        # explicitly send the field — older PUT clients don't know about it,
        # and a default-driven overwrite would silently revert chat-enabled
        # templates to voice-only on unrelated edits.
        supported_channels: List[str] = [
            str(ch)
            for ch in (
                template_data.supported_channels
                if template_data.supported_channels is not None
                else existing_template.supported_channels
            )
        ]

        updated_template, version_number = await replace_template(
            template_id=template_id,
            reseller_id=reseller_id,
            name=template_data.name,
            flow=flow,
            expected_payload_schema=template_data.expected_payload_schema,
            expected_callback_response_schema=template_data.expected_callback_response_schema,
            configurations=configurations,
            secrets=merged_secrets,
            outbound_number_id=template_data.outbound_number_id,
            is_active=template_data.is_active,
            merchant_id=template_data.merchant_id,
            supported_channels=supported_channels,
            now=now,
            updated_by=current_user.username,
            snapshot_configurations=snapshot_configurations,
        )

        if not updated_template:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update template",
            )

        # Cache invalidation is best-effort: the DB write has already
        # committed, so a Redis blip here must not surface as a 500 to a
        # client whose mutation actually succeeded. Stale cache entries
        # self-correct on TTL expiry.
        try:
            await invalidate_template(template_id)
        except Exception as cache_exc:
            logger.warning(
                f"Template cache invalidation failed for {template_id}: {cache_exc}"
            )

        logger.info(
            f"Successfully updated template with id: {updated_template.id} containing flow "
            f"with {len(flow.get('nodes', []))} nodes, version {version_number}"
        )

        return mask_template_secrets(updated_template)

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Validation error updating template: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating template: {str(e)}",
        )


async def delete_template_handler(
    template_id: str, current_user: UserInfo
) -> DeleteTemplateResponse:
    """
    Delete a template by ID after verifying it can be safely removed.

    Safety checks:
    - Template must exist
    - Template must not be referenced by any call_execution_config
    - Template must not have active leads (BACKLOG, RETRY, PROCESSING) in lead_call_tracker

    Args:
        template_id: Template UUID
        current_user: Current authenticated user (must be admin)

    Returns:
        DeleteTemplateResponse with deleted template metadata

    Raises:
        HTTPException: 404 if not found, 409 if in use, 500 on error
    """
    logger.info(
        f"Admin {current_user.username} requesting deletion of template: {template_id}"
    )

    try:
        # Check if template exists
        existing_template = await get_template_by_id(template_id)
        if not existing_template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template not found: {template_id}",
            )

        deleted = await delete_template_if_not_referenced(template_id)

        if not deleted:
            # Fetch fresh usage counts for error message
            usage = await check_template_usage(template_id)
            blockers = []
            config_count = usage.get("call_execution_config", 0) if usage else 0
            active_leads_count = usage.get("lead_call_tracker", 0) if usage else 0

            if config_count > 0:
                blockers.append(
                    f"{config_count} call execution config(s) reference this template"
                )

            if active_leads_count > 0:
                blockers.append(
                    f"{active_leads_count} active lead(s) (BACKLOG/RETRY/PROCESSING) are using this template"
                )

            detail = (
                f"Template '{existing_template.name}' cannot be safely deleted. "
                f"Reasons: {'; '.join(blockers)}. "
                f"Please remove these references before deleting the template."
            )
            logger.warning(f"Template {template_id} deletion blocked: {detail}")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            )

        try:
            await invalidate_template(template_id)
        except Exception as cache_exc:
            logger.warning(
                f"Template cache invalidation failed for {template_id}: {cache_exc}"
            )

        logger.info(
            f"Admin {current_user.username} successfully deleted template: "
            f"{deleted.id} ({deleted.name})"
        )

        return DeleteTemplateResponse(
            status="success",
            message=f"Template '{deleted.name}' deleted successfully",
            deleted_template=deleted,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting template: {str(e)}",
        )


async def _get_template_with_access(template_id: str, current_user: UserInfo):
    """Shared 404 + RBAC gate for the version endpoints."""
    template = await get_template_by_id(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )
    validate_template_access(
        current_user,
        template.reseller_id,
        template.merchant_id,
        operation="access template",
    )
    return template


async def list_template_versions_handler(
    template_id: str, page: int, limit: int, current_user: UserInfo
) -> TemplateVersionListResponse:
    try:
        await _get_template_with_access(template_id, current_user)
        offset = (page - 1) * limit
        versions, total = await list_template_versions(template_id, limit, offset)
        # Active version == MAX(version_number) == head of the newest-first
        # order; page 1 already holds it, later pages fetch just the head.
        if page == 1 and versions:
            active_version = versions[0].version_number
        else:
            head, _ = await list_template_versions(template_id, 1, 0)
            active_version = head[0].version_number if head else None
        return TemplateVersionListResponse(
            versions=versions, total=total, active_version=active_version
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing template versions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error listing template versions",
        )


async def get_template_version_handler(
    template_id: str, version_number: int, current_user: UserInfo
) -> TemplateVersionDetail:
    try:
        await _get_template_with_access(template_id, current_user)
        version = await get_template_version_by_number(template_id, version_number)
        if not version:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Version {version_number} not found for template {template_id}",
            )
        return version
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting template version: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error getting template version",
        )


def build_rollback_template_args(
    existing: TemplateModel, snapshot: TemplateVersionDetail
) -> Dict[str, Any]:
    """kwargs for replace_template that restore a snapshot.

    Restored from the snapshot: flow, configurations, both payload schemas.
    Kept from the live row: name, secrets, outbound_number_id, is_active,
    scoping, supported_channels (design doc §4 — avoids rename-uniqueness
    collisions and surprise number/credential changes).

    Snapshot MCP auth is stored masked; merge_masked_mcp_auth swaps the
    masks for the live row's real values (or clears them when there is no
    live counterpart, so a broken restore fails loudly at MCP setup).

    The merged dict is then validated like a PUT body (design doc §6):
    snapshots are raw dicts that may predate today's ``ConfigurationModel``
    shape, so we normalize legacy fields and construct the model before
    this is ever written back to the live row. Without this, a rollback to
    an old snapshot can commit a config that no longer decodes, soft-
    bricking the template (get_template_by_id returns None -> 404s).
    Raises ``ValueError`` on an invalid snapshot; the caller maps that to
    a 400.
    """
    existing_configurations = (
        existing.configurations.model_dump(
            exclude_none=True, mode="json", context={"reveal_secrets": True}
        )
        if existing.configurations
        else None
    )
    merged_configurations = (
        merge_masked_mcp_auth(
            copy.deepcopy(snapshot.configurations), existing_configurations
        )
        if snapshot.configurations
        else None
    )
    restored_configurations = None
    if merged_configurations is not None:
        normalized = _migrate_legacy_voice_config(merged_configurations)
        try:
            validated_configurations = ConfigurationModel(**normalized)
        except ValidationError as e:
            raise ValueError(
                f"Version {snapshot.version_number} configurations are no "
                f"longer valid against the current template schema and "
                f"cannot be restored: {e}"
            ) from e
        restored_configurations = validated_configurations.model_dump(
            exclude_none=True, mode="json", context={"reveal_secrets": True}
        )
    return {
        "template_id": str(existing.id),
        "reseller_id": existing.reseller_id,
        "name": existing.name,
        "flow": snapshot.flow,
        "expected_payload_schema": snapshot.expected_payload_schema,
        "expected_callback_response_schema": snapshot.expected_callback_response_schema,
        "configurations": restored_configurations,
        "secrets": existing.secrets,
        "outbound_number_id": existing.outbound_number_id,
        "is_active": existing.is_active,
        "merchant_id": existing.merchant_id,
        "supported_channels": [str(ch) for ch in existing.supported_channels],
    }


async def rollback_template_handler(
    template_id: str, version_number: int, current_user: UserInfo
) -> RollbackTemplateResponse:
    """Restore version n as a NEW latest version (append-only history)."""
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) rolling back "
        f"template {template_id} to version {version_number}"
    )
    try:
        existing_template = await _get_template_with_access(template_id, current_user)
        snapshot = await get_template_version_by_number(template_id, version_number)
        if not snapshot:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Version {version_number} not found for template {template_id}",
            )

        _validate_flow_shape(snapshot.flow)

        args = build_rollback_template_args(existing_template, snapshot)
        now = datetime.now(timezone.utc)
        updated_template, new_version = await replace_template(
            **args,
            now=now,
            updated_by=current_user.username,
            # Snapshot the FINAL persisted (validated + merged) configuration,
            # masked — not the raw historical snapshot being restored from.
            # Reconstructing through ConfigurationModel with no reveal_secrets
            # context makes the SecretStr serializer emit "**********", so
            # this version row mirrors what actually went live, minus
            # secrets, same as the PUT path.
            snapshot_configurations=(
                ConfigurationModel(**args["configurations"]).model_dump(
                    exclude_none=True, mode="json"
                )
                if args["configurations"]
                else None
            ),
            change_source="rollback",
            restored_from=version_number,
        )
        if not updated_template or new_version is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to roll back template",
            )

        # Best-effort cache bust — same contract as PUT: the DB write is
        # committed; a Redis blip must not surface as a 500.
        try:
            await invalidate_template(template_id)
        except Exception as cache_exc:
            logger.warning(
                f"Template cache invalidation failed for {template_id}: {cache_exc}"
            )

        logger.info(
            f"Template {template_id} rolled back to version {version_number} "
            f"as new version {new_version}"
        )
        return RollbackTemplateResponse(
            template_id=str(template_id),
            restored_from=version_number,
            new_version=new_version,
            message=(
                f"Version {version_number} restored as version {new_version}; "
                f"it is now live"
            ),
        )
    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Validation error rolling back template: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error rolling back template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error rolling back template",
        )
