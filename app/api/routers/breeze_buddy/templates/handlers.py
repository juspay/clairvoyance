"""
Business logic handlers for template operations.
All handlers perform database operations and enforce business rules.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.template.types import (
    CreateTemplateRequest,
    ReplaceTemplateRequest,
)
from app.ai.voice.agents.breeze_buddy.utils.secrets import (
    mask_template_secrets,
    merge_secrets,
)
from app.core.logger import logger
from app.database.accessor import get_outbound_number_by_id, get_template_by_merchant
from app.database.accessor.breeze_buddy.template import (
    check_template_usage,
    create_template,
    delete_template_if_not_referenced,
    get_template_by_id,
    get_templates_list,
    replace_template,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template import (
    DeleteTemplateResponse,
    TemplateListResponse,
)

from .rbac import apply_hierarchical_template_filters, validate_template_access


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
        if not flow:
            raise ValueError("Flow structure is required")

        if "initial_node" not in flow:
            raise ValueError("initial_node must be specified in flow structure")

        if "nodes" not in flow or not flow["nodes"]:
            raise ValueError("nodes must be specified in flow structure")

        # Check if template already exists
        existing = await get_template_by_merchant(
            template_data.reseller_id,
            template_data.merchant_id,
            template_data.name,
            should_prioritize_merchant_specific=False,
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

        # Build configurations dict from the ConfigurationModel
        configurations = None
        if template_data.configurations:
            configurations = template_data.configurations.model_dump(exclude_none=True)

        template = await create_template(
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
            now=now,
        )

        if not template:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create template",
            )

        logger.info(
            f"Successfully created template with id: {template.id} containing flow "
            f"with {len(flow.get('nodes', []))} nodes"
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


async def get_template_handler(
    reseller_id: str,
    merchant_id: Optional[str],
    name: Optional[str],
    current_user: UserInfo,
):
    """
    Get template(s) by reseller, shop, and name.

    Args:
        reseller_id: Reseller ID
        merchant_id: Optional shop identifier
        name: Optional template name
        current_user: Current authenticated user

    Returns:
        Template object or list of templates
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting template "
        f"for reseller: {reseller_id}, shop: {merchant_id}, name: {name}"
    )

    try:
        template = await get_template_by_merchant(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            name=name,
        )

        if template:
            logger.info(f"Template found: {template.id}")
            return mask_template_secrets(template)
        else:
            logger.info(
                f"No template found for reseller: {reseller_id}, "
                f"merchant_id: {merchant_id}, name: {name}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template '{name}' not found for reseller: {reseller_id}",
            )

    except Exception as e:
        logger.error(f"Error getting template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting template: {str(e)}",
        )


async def list_templates_handler(
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    include_inactive: bool,
    current_user: UserInfo,
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

        # Get templates from database
        templates = await get_templates_list(filters)

        logger.info(
            f"Returning {len(templates)} templates for user {current_user.username}"
        )

        return TemplateListResponse(templates=templates, total=len(templates))

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

        # Validate flow structure
        flow = template_data.flow
        if not flow:
            raise ValueError("Flow structure is required")

        if "initial_node" not in flow:
            raise ValueError("initial_node must be specified in flow structure")

        if "nodes" not in flow or not flow["nodes"]:
            raise ValueError("nodes must be specified in flow structure")

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

        # Build configurations dict from the ConfigurationModel
        # If configurations is explicitly provided, use it; otherwise set to None (NULL)
        configurations = None
        if template_data.configurations:
            configurations = template_data.configurations.model_dump(exclude_none=True)

        # Merge secrets: preserve **** values from existing, update real values
        merged_secrets = merge_secrets(
            incoming_secrets=template_data.secrets,
            existing_secrets=existing_template.secrets,
        )
        updated_template = await replace_template(
            template_id=template_id,
            name=template_data.name,
            flow=flow,
            expected_payload_schema=template_data.expected_payload_schema,
            expected_callback_response_schema=template_data.expected_callback_response_schema,
            configurations=configurations,
            secrets=merged_secrets,
            outbound_number_id=template_data.outbound_number_id,
            is_active=template_data.is_active,
            merchant_id=template_data.merchant_id,
            now=now,
        )

        if not updated_template:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update template",
            )

        logger.info(
            f"Successfully updated template with id: {updated_template.id} containing flow "
            f"with {len(flow.get('nodes', []))} nodes"
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


async def get_configuration_options_handler():

    from app.ai.voice.agents.breeze_buddy.template.types import (
        BackgroundSoundFile,
        KeywordMatchType,
        NoiseFilterType,
        TTSVoiceName,
    )
    from app.ai.voice.agents.breeze_buddy.utils.language_utils.language_detector import (
        LANGUAGE_NAMES,
        SHORT_TO_FULL_LANGUAGE_CODE,
    )
    from app.ai.voice.agents.breeze_buddy.utils.tts_utils.tts_provider_selector import (
        TTS_PROVIDER_TO_VOICE_NAME,
    )

    voice_to_provider = {v.value: k for k, v in TTS_PROVIDER_TO_VOICE_NAME.items()}

    # TTS voices with provider info
    tts_voices = [
        {
            "value": voice.value,
            "label": f"{voice.value.capitalize()} ({voice_to_provider.get(voice.value, 'Unknown')})",
        }
        for voice in TTSVoiceName
    ]

    # STT languages (Soniox supported) - dynamically from constants
    stt_languages = [
        {"value": code, "label": LANGUAGE_NAMES.get(full_code, code)}
        for code, full_code in SHORT_TO_FULL_LANGUAGE_CODE.items()
    ]

    # Background sounds
    background_sounds = [
        {"value": sound.value, "label": sound.value.replace("-", " ").title()}
        for sound in BackgroundSoundFile
    ]

    # Noise filter types
    noise_filter_types = [
        {"value": filter_type.value, "label": filter_type.value.upper()}
        for filter_type in NoiseFilterType
    ]

    # Keyword match types
    keyword_match_types = [
        {"value": match_type.value, "label": match_type.value.replace("_", " ").title()}
        for match_type in KeywordMatchType
    ]

    return {
        "tts_voices": tts_voices,
        "stt_languages": stt_languages,
        "background_sounds": background_sounds,
        "noise_filter_types": noise_filter_types,
        "keyword_match_types": keyword_match_types,
    }
