import asyncio
import base64
import json
from datetime import timedelta

from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient as PipecatMCPClient

from app.agents.voice.automatic.tools import initialize_tools
from app.agents.voice.automatic.tools.charts import (
    tool_functions as chart_tool_functions,
)
from app.agents.voice.automatic.types import Mode
from app.core import config
from app.core.logger import logger
from app.utils.common import get_breeze_portal_url


async def init_breeze_mcp_tools(
    llm,
    mcp_context,
    breeze_token,
    reseller_id,
    mode,
    args,
):
    logger.info(f"Initializing tools from remote MCP server")

    # Use pure Pipecat MCP client
    logger.info(f"Using pure Pipecat MCP client for shop_id: {args.shop_id}")

    try:
        # Use Pipecat MCP client directly (standard MCP protocol only)
        server_params = StreamableHttpParameters(
            url=f"{get_breeze_portal_url(reseller_id)}{config.BREEZE_MCP_ENDPOINT_PATH}",
            headers={
                "x-auth-token": breeze_token,
                "x-context": base64.b64encode(
                    json.dumps(mcp_context).encode()
                ).decode(),
            },
            timeout=timedelta(seconds=config.MCP_CLIENT_TIMEOUT),
            sse_read_timeout=timedelta(seconds=config.MCP_CLIENT_TIMEOUT),
            terminate_on_close=True,
        )

        mcp_client = PipecatMCPClient(server_params=server_params)
        tools = await asyncio.wait_for(
            mcp_client.register_tools(llm), timeout=config.MCP_CLIENT_TIMEOUT
        )

        # register chart tools if enabled
        if config.ENABLE_CHARTS:
            for name, function in chart_tool_functions.items():
                logger.info(f"Registering essential chart tool: {name}")
                llm.register_function(name, function)

        # Log registration success
        if tools:
            logger.info(
                f"Successfully registered MCP tools via pure Pipecat MCP client"
            )
        else:
            logger.warning(
                f"MCP client returned None or empty tools object - but essential tools are still registered"
            )

        return tools

    except Exception as e:
        logger.error(f"Failed to register MCP tools: {type(e).__name__}: {str(e)}")
        logger.exception("Full exception traceback:")

        # Fallback to traditional tools
        if mode == Mode.LIVE:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                breeze_token=breeze_token,
                euler_token=args.euler_token,
                shop_url=args.shop_url,
                shop_id=args.shop_id,
                shop_type=args.shop_type,
                merchant_id=args.merchant_id,
                session_id=args.client_sid,
                user_id=args.user_name,
                user_email=args.user_email,
                reseller_id=args.reseller_id,
            )
        else:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                merchant_id=args.merchant_id,
                session_id=args.client_sid,
                reseller_id=args.reseller_id,
            )

        # Register all fallback tools
        for name, function in tool_functions.items():
            logger.info(f"Initializing fallback function tool: {name}")
            llm.register_function(name, function)

        return tools
