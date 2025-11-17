"""
Request handler for conversation history management.
Handles sending and retrieving conversation context to/from Lighthouse APIs.
"""

import asyncio
from typing import Optional

import aiohttp

from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.utils.common import get_breeze_portal_url


async def save_conversation_context(
    session_id: str,
    llm_context,
    accumulated_text: str,
    tool_calls: list,
    tool_results: list,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    shop_id: Optional[str] = None,
    breeze_token: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """
    Save conversation context by extracting data from LLM context and sending to Lighthouse.

    Args:
        session_id: Backend session ID
        llm_context: LLM context aggregator to extract user messages from
        accumulated_text: Accumulated assistant response text
        tool_calls: List of tool calls made during this turn
        tool_results: List of tool results received during this turn
        reseller_id: Reseller ID to select portal URL
        merchant_id: Merchant ID (for merchant token authentication)
        shop_id: Shop ID (for merchant token authentication)
        breeze_token: JWT token for authentication
        timeout: Request timeout in seconds (default 10)

    Returns:
        True if saved successfully
    """
    if not llm_context:
        return False

    try:
        # Get accumulated assistant message
        assistant_message = accumulated_text.strip() if accumulated_text else None
        if not assistant_message:
            return False

        # Extract last user message from context_aggregator (filter for user role only)
        all_messages = llm_context._user._context.get_messages()
        user_messages = [msg for msg in all_messages if msg.get("role") == "user"]
        user_message = user_messages[-1]["content"].strip() if user_messages else None

        # Skip if no user message (welcome message scenario)
        if not user_message:
            logger.debug(
                f"[{session_id}] Skipping conversation save - no user message yet"
            )
            return False

        logger.info(
            f"[{session_id}] Saving conversation: "
            f"user({len(user_message)} chars), assistant({len(assistant_message)} chars), "
            f"tool_calls({len(tool_calls)}), tool_results({len(tool_results)})"
        )

        # Build URL
        base_url = get_breeze_portal_url(reseller_id)
        url = f"{base_url.rstrip('/')}/conversation-history"

        # Add query parameters
        query_params = []
        if merchant_id:
            query_params.append(f"merchantId={merchant_id}")
        if shop_id:
            query_params.append(f"shopId={shop_id}")
        if query_params:
            url += "?" + "&".join(query_params)

        # Prepare payload
        payload = {
            "sessionId": session_id,
            "userMessage": user_message,
            "assistantMessage": assistant_message,
            "toolCalls": tool_calls or [],
            "toolResults": tool_results or [],
        }

        # Setup headers
        headers = {"Content-Type": "application/json"}
        if breeze_token:
            headers["Authorization"] = f"Bearer {breeze_token}"

        # Send POST request
        async with create_aiohttp_session() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 200:
                    logger.debug(f"[{session_id}] Conversation saved successfully")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(
                        f"[{session_id}] Failed to save conversation: status={response.status}, error={error_text}"
                    )
                    return False

    except asyncio.TimeoutError:
        logger.error(f"[{session_id}] Timeout saving conversation after {timeout}s")
        return False
    except Exception as e:
        logger.error(
            f"[{session_id}] Failed to save conversation: {e}",
            exc_info=True,
        )
        return False


async def restore_conversation_context(
    session_id: str,
    context_aggregator,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    shop_id: Optional[str] = None,
    breeze_token: Optional[str] = None,
    timeout: int = 10,
) -> int:
    """
    Restore previous conversation context from Lighthouse to LLM context.

    Args:
        session_id: Backend session ID
        context_aggregator: LLM context aggregator to add messages to
        reseller_id: Reseller ID to select portal URL
        merchant_id: Merchant ID (for merchant token authentication)
        shop_id: Shop ID (for merchant token authentication)
        breeze_token: JWT token for authentication
        timeout: Request timeout in seconds (default 10)

    Returns:
        Number of messages restored
    """
    try:
        logger.info(f"[{session_id}] Restoring conversation from Lighthouse...")

        # Build URL
        base_url = get_breeze_portal_url(reseller_id)
        url = f"{base_url.rstrip('/')}/conversation-history/{session_id}"

        # Add query parameters
        query_params = []
        if merchant_id:
            query_params.append(f"merchantId={merchant_id}")
        if shop_id:
            query_params.append(f"shopId={shop_id}")
        if query_params:
            url += "?" + "&".join(query_params)

        # Setup headers
        headers = {"Content-Type": "application/json"}
        if breeze_token:
            headers["Authorization"] = f"Bearer {breeze_token}"

        # Send GET request
        async with create_aiohttp_session() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 404:
                    logger.info(f"[{session_id}] No previous conversation found")
                    return 0
                elif response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"[{session_id}] Failed to restore conversation: status={response.status}, error={error_text}"
                    )
                    return 0

                # Parse response
                result = await response.json()
                if result.get("status") == "success" and result.get("data"):
                    messages = result["data"].get("messages", [])
                else:
                    logger.warning(
                        f"[{session_id}] Unexpected response format: {result}"
                    )
                    return 0

        # Validate messages
        if not messages:
            logger.warning(f"[{session_id}] No messages returned from Lighthouse")
            return 0

        # Restore messages to LLM context
        restored_count = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            content = msg.get("content", "").strip()

            if not content:
                continue

            try:
                if role == "user":
                    context_aggregator._user._context.add_message(
                        {"role": role, "content": content}
                    )
                    restored_count += 1
                elif role == "assistant":
                    context_aggregator._assistant._context.add_message(
                        {"role": role, "content": content}
                    )
                    restored_count += 1
                else:
                    # Skip unknown roles (tool_call, tool_result, etc.)
                    logger.debug(f"[{session_id}] Skipping message with role: {role}")
            except Exception as e:
                logger.warning(f"[{session_id}] Failed to restore message {i+1}: {e}")

        logger.info(f"[{session_id}] Restored {restored_count} messages to LLM context")
        return restored_count

    except asyncio.TimeoutError:
        logger.error(f"[{session_id}] Timeout restoring conversation after {timeout}s")
        return 0
    except Exception as e:
        logger.error(
            f"[{session_id}] Failed to restore conversation: {e}",
            exc_info=True,
        )
        return 0
