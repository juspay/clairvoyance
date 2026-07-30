"""Buddy Assist onboarding SSE endpoint."""

import asyncio
import json
import secrets
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Tuple
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.template.cache import invalidate_template
from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.database.accessor.breeze_buddy.template import (
    create_template,
    delete_template_if_not_referenced,
    get_template_by_id,
    replace_template,
)
from app.database.accessor.breeze_buddy.widget_config import (
    create_widget_config,
    get_widget_config_by_id,
    get_widget_config_by_reseller_merchant,
    update_widget_config,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.assist_onboarding import (
    AssistOnboardingCompletePayload,
    AssistOnboardingStreamRequest,
)
from app.services.breeze_buddy.assist_template import (
    build_assist_response_metadata,
    build_assist_template,
)
from app.services.redis.locks import LockAcquireError, RedisLock

router = APIRouter()
_PUBLIC_KEY_NBYTES = 32
_ONBOARDING_LOCK_TTL_SECONDS = 120
_MAX_CONCURRENT_ONBOARDING_STREAMS = 8
_onboarding_stream_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ONBOARDING_STREAMS)


@router.post("/assist/onboard/stream")
async def onboard_assist_stream(
    body: AssistOnboardingStreamRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> StreamingResponse:
    """Provision Assist template + widget_config and stream progress events."""

    async def event_stream() -> AsyncIterator[str]:
        template = None
        created_template = False
        try:
            yield _sse("progress", {"step": "started", "status": "done"})
            yield _sse("progress", {"step": "validating_request", "status": "running"})
            validate_template_access(
                current_user,
                body.reseller_id,
                body.merchant_id,
                operation="onboard assist",
            )
            yield _sse("progress", {"step": "validating_request", "status": "done"})

            lock = RedisLock(
                _onboarding_lock_key(body.reseller_id, body.merchant_id),
                ttl_seconds=_ONBOARDING_LOCK_TTL_SECONDS,
            )
            async with _onboarding_stream_semaphore:
                async with lock:
                    yield _sse(
                        "progress",
                        {"step": "personalizing_prompt", "status": "running"},
                    )
                    generated = await build_assist_template(
                        {
                            "merchant_id": body.merchant_id,
                            "shop_url": body.shop_url,
                            "is_shopify": body.is_shopify,
                            "allowed_origins": body.allowed_origins,
                            "brand_name": body.brand_name,
                            "header_title": body.header_title,
                        }
                    )
                    metadata = build_assist_response_metadata(generated)
                    yield _sse(
                        "progress",
                        {
                            "step": "prompt_ready",
                            "status": "done",
                            "personalized": metadata["personalized"],
                            "personalizationStatus": metadata["personalization_status"],
                            "promptHash": metadata["prompt_hash"],
                        },
                    )

                    yield _sse(
                        "progress",
                        {"step": "building_template", "status": "running"},
                    )
                    template, created_template = await _create_or_update_template(
                        body, generated
                    )
                    yield _sse(
                        "progress",
                        {
                            "step": "template_ready",
                            "status": "done",
                            "templateId": template.id,
                            "templateName": generated.template_name,
                        },
                    )

                    yield _sse(
                        "progress",
                        {"step": "ensuring_widget_config", "status": "running"},
                    )
                    try:
                        widget_config = await _create_or_update_widget_config(
                            body, template.id
                        )
                    except Exception:
                        if created_template and await _cleanup_created_template(
                            template.id
                        ):
                            template = None
                        raise
                    yield _sse(
                        "progress",
                        {
                            "step": "widget_config_ready",
                            "status": "done",
                            "widgetConfigId": widget_config.id,
                            "publicWidgetKey": widget_config.public_widget_key,
                        },
                    )

                    complete = AssistOnboardingCompletePayload(
                        templateId=template.id,
                        templateName=generated.template_name,
                        widgetConfigId=widget_config.id,
                        publicWidgetKey=widget_config.public_widget_key,
                        allowedOrigins=widget_config.allowed_origins,
                        max_sessions_per_ip_hour=(
                            widget_config.max_sessions_per_ip_hour
                        ),
                        max_messages_per_ip_hour=(
                            widget_config.max_messages_per_ip_hour
                        ),
                        max_concurrent_per_ip=widget_config.max_concurrent_per_ip,
                        max_voice_sessions_per_ip_hour=(
                            widget_config.max_voice_sessions_per_ip_hour
                        ),
                        active=widget_config.active,
                        personalized=bool(metadata["personalized"]),
                        personalizationStatus=str(metadata["personalization_status"]),
                        personalizationFailureReason=metadata[
                            "personalization_failure_reason"
                        ],
                        brandProfile=metadata["brand_profile"],
                        brandProfileSource=str(metadata["brand_profile_source"]),
                        promptHash=str(metadata["prompt_hash"]),
                    )
                    yield _sse(
                        "complete",
                        complete.model_dump(
                            by_alias=True, mode="json", exclude_none=True
                        ),
                    )
        except LockAcquireError:
            yield _sse(
                "error",
                {
                    "success": False,
                    "error": "ASSIST_ONBOARDING_IN_PROGRESS",
                    "message": "Assist onboarding is already running for this merchant",
                    "statusCode": status.HTTP_409_CONFLICT,
                },
            )
        except HTTPException as exc:
            payload = {
                "success": False,
                "error": "ASSIST_ONBOARDING_FAILED",
                "message": str(exc.detail),
                "statusCode": exc.status_code,
            }
            if template:
                payload["templateId"] = template.id
            yield _sse("error", payload)
        except Exception as exc:
            logger.exception("Assist onboarding stream failed")
            payload = {
                "success": False,
                "error": "ASSIST_ONBOARDING_FAILED",
                "message": str(exc),
            }
            if template:
                payload["templateId"] = template.id
            yield _sse("error", payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _create_or_update_template(
    body: AssistOnboardingStreamRequest, generated
) -> Tuple[Any, bool]:
    configurations = generated.configurations.model_dump(
        exclude_none=True,
        mode="json",
        context={"reveal_secrets": True},
    )
    now = datetime.now(timezone.utc)

    existing = await get_template_by_id(body.template_id) if body.template_id else None
    if body.template_id and not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assist template not found",
        )
    if existing:
        _validate_template_scope(existing, body)
        template = await replace_template(
            template_id=existing.id,
            reseller_id=body.reseller_id,
            name=generated.template_name,
            flow=generated.flow,
            expected_payload_schema=generated.expected_payload_schema,
            expected_callback_response_schema=(
                generated.expected_callback_response_schema
            ),
            configurations=configurations,
            secrets=existing.secrets,
            outbound_number_id=existing.outbound_number_id,
            is_active=body.is_active,
            merchant_id=body.merchant_id,
            supported_channels=generated.supported_channels,
            now=now,
        )
        if not template:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update Assist template",
            )
        try:
            await invalidate_template(existing.id)
        except Exception:
            logger.warning("Failed to invalidate Assist template cache", exc_info=True)
        return template, False

    template = await create_template(
        template_id=str(uuid4()),
        reseller_id=body.reseller_id,
        merchant_id=body.merchant_id,
        name=generated.template_name,
        flow=generated.flow,
        expected_payload_schema=generated.expected_payload_schema,
        expected_callback_response_schema=generated.expected_callback_response_schema,
        configurations=configurations,
        secrets=None,
        outbound_number_id=None,
        is_active=body.is_active,
        supported_channels=generated.supported_channels,
        now=now,
    )
    if not template:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create Assist template",
        )
    return template, True


async def _create_or_update_widget_config(
    body: AssistOnboardingStreamRequest, template_id: str
):
    widget_config = None
    if body.widget_config_id:
        widget_config = await get_widget_config_by_id(body.widget_config_id)
        if not widget_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assist widget_config not found",
            )
        if (
            widget_config.reseller_id != body.reseller_id
            or widget_config.merchant_id != body.merchant_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="widget_config does not belong to this reseller/merchant",
            )
    if not widget_config:
        widget_config = await get_widget_config_by_reseller_merchant(
            body.reseller_id, body.merchant_id
        )

    if widget_config:
        updated = await update_widget_config(
            widget_config.id,
            template_id=template_id,
            allowed_origins=body.allowed_origins or widget_config.allowed_origins,
            active=True,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update Assist widget_config",
            )
        return updated

    created = await create_widget_config(
        reseller_id=body.reseller_id,
        merchant_id=body.merchant_id,
        public_widget_key=secrets.token_urlsafe(_PUBLIC_KEY_NBYTES),
        template_id=template_id,
        allowed_origins=body.allowed_origins,
        max_sessions_per_ip_hour=60,
        max_messages_per_ip_hour=600,
        max_concurrent_per_ip=4,
        max_voice_sessions_per_ip_hour=10,
        active=True,
    )
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create Assist widget_config",
        )
    return created


def _validate_template_scope(
    template: Any, body: AssistOnboardingStreamRequest
) -> None:
    if template.reseller_id != body.reseller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template does not belong to the specified reseller",
        )
    if template.merchant_id and template.merchant_id != body.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template does not belong to the specified merchant",
        )


async def _cleanup_created_template(template_id: str) -> bool:
    deleted = await delete_template_if_not_referenced(template_id)
    if deleted:
        return True
    logger.warning(
        "Failed to clean up Assist template after widget_config failure",
        template_id=template_id,
    )
    return False


def _onboarding_lock_key(reseller_id: str, merchant_id: str) -> str:
    return f"breeze_buddy:assist_onboarding:{reseller_id}:{merchant_id}:lock"


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


__all__ = ["router"]
