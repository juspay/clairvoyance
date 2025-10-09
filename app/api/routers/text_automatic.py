"""
API Router for the Text Automatic Agent
"""

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from app.agents.text.automatic import pipeline_manager
from app.agents.text.automatic.utils import get_available_tools
from app.core.logger import logger
from app.schemas import AutomaticTextUserConnectRequest

router = APIRouter()


@router.post("/")
async def chat(request: AutomaticTextUserConnectRequest):
    """Chat endpoint with streaming response."""
    try:
        session_id = request.sessionId or str(uuid.uuid4())

        logger.info(f"Text Pipeline: Chat endpoint called for session [{session_id}]")
        logger.info(
            f"Text Pipeline: Request [{request.model_dump_json(exclude_none=True)}]"
        )

        # Validate message
        if not request.message or not request.message.strip():
            logger.error("Text Pipeline: No message provided")
            return JSONResponse({"error": "No message provided"}, status_code=400)

        # Validate message length (prevent extremely long messages)
        if len(request.message.strip()) > 10000:  # 10KB limit
            logger.error(
                f"Text Pipeline: Message too long [{len(request.message)} characters]"
            )
            return JSONResponse(
                {"error": "Message too long (max 10,000 characters)"}, status_code=400
            )

        # Validate session ID format if provided
        if request.sessionId and (
            len(request.sessionId) > 100
            or not request.sessionId.replace("-", "").replace("_", "").isalnum()
        ):
            logger.error(
                f"Text Pipeline: Invalid session ID format [{request.sessionId}]"
            )
            return JSONResponse({"error": "Invalid session ID format"}, status_code=400)

        # Build config from request parameters with proper defaults and validation
        config = {
            "mode": (
                (request.mode or "TEST").upper()
                if request.mode in ["TEST", "LIVE", None]
                else "TEST"
            ),
            "user_name": (
                request.userName.strip()
                if request.userName and request.userName.strip()
                else None
            ),
            "user_email": (
                request.email.strip()
                if request.email and request.email.strip()
                else None
            ),
            "euler_token": (
                request.eulerToken.strip()
                if request.eulerToken and request.eulerToken.strip()
                else None
            ),
            "breeze_token": (
                request.breezeToken.strip()
                if request.breezeToken and request.breezeToken.strip()
                else None
            ),
            "shop_url": (
                request.shopUrl.strip()
                if request.shopUrl and request.shopUrl.strip()
                else None
            ),
            "shop_id": (
                request.shopId.strip()
                if request.shopId and request.shopId.strip()
                else None
            ),
            "shop_type": (
                request.shopType.strip()
                if request.shopType and request.shopType.strip()
                else None
            ),
            "merchant_id": (
                request.merchantId.strip()
                if request.merchantId and request.merchantId.strip()
                else None
            ),
            "platform_integrations": (
                request.platformIntegrations
                if isinstance(request.platformIntegrations, list)
                else None
            ),
            "reseller_id": (
                request.resellerId.strip()
                if request.resellerId and request.resellerId.strip()
                else None
            ),
            "conversation_id": (
                request.conversationId.strip()
                if request.conversationId and request.conversationId.strip()
                else None
            ),
        }

        logger.info(
            f"Text Pipeline: Calling pipeline_manager.process_message for session [{session_id}]"
        )
        # Process message through pipeline manager
        response_generator = await pipeline_manager.process_message(
            session_id, request.message, config
        )
        logger.info(
            "Text Pipeline: Got response_generator, returning StreamingResponse"
        )

        return StreamingResponse(
            response_generator,
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    except Exception as e:
        logger.error(f"Text Pipeline: Error in chat [{str(e)}]")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/status")
async def get_status():
    """Get text agent status with detailed cache statistics."""
    try:
        cache_stats = pipeline_manager.get_cache_stats()
        active_sessions = pipeline_manager.get_active_sessions()

        return JSONResponse(
            {
                "status": "healthy",
                "active_sessions": active_sessions,
                "cache_stats": cache_stats,
                "service": "text-automatic-agent",
            }
        )
    except Exception as e:
        logger.error(f"Text Pipeline: Error getting status [{str(e)}]")
        return JSONResponse(
            {
                "status": "degraded",
                "error": "Failed to retrieve cache statistics",
                "service": "text-automatic-agent",
            },
            status_code=503,
        )


@router.get("/tools")
async def get_tools(mode: str = "TEST", shop_id: str = None, user_email: str = None):
    """Get list of available tools."""
    try:
        tools_info = get_available_tools(mode, shop_id, user_email)
        return JSONResponse(tools_info)
    except Exception as e:
        logger.error(f"Text Pipeline: Error getting tools [{str(e)}]")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/history")
async def clear_conversation_history(session_id: str):
    """Clear conversation history for a specific session."""
    try:
        await pipeline_manager.cache_manager.clear_conversation_history(session_id)
        return JSONResponse(
            {"message": f"Conversation history cleared for session [{session_id}]"}
        )
    except Exception as e:
        logger.error(f"Text Pipeline: Error clearing conversation history [{str(e)}]")
        return JSONResponse({"error": str(e)}, status_code=500)
