"""
MCP Client wrapper for Breeze Buddy.

This module provides a wrapper around PipecatMCPClient that handles
connection lifecycle and tool registration for template-based MCP integration.
Uses Storefront MCP (HTTP Streamable transport) for tool integration.
"""

import asyncio
from typing import Any, Dict, List, Optional

import httpx
from mcp import ClientSession
from mcp.client.session_group import StreamableHttpParameters
from mcp.client.streamable_http import streamable_http_client
from pipecat.services.mcp_service import MCPClient as PipecatMCPClient
from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.mcp.config import DEFAULT_MCP_TIMEOUT
from app.core.config.dynamic import BB_MCP_MAX_TIMEOUT
from app.core.logger import logger


class BreezeBuddyMCPClient:
    """
    MCP Client for Breeze Buddy template-based tool integration.

    Uses Pipecat's public MCPClient API for tool registration and the
    MCP SDK directly for tool execution to avoid private API coupling.

    Usage:
        client = BreezeBuddyMCPClient()
        await client.connect("https://your-store.myshopify.com/api/mcp")
        tools = await client.register_tools(llm)
        # ... use tools ...
        await client.disconnect()
    """

    def __init__(self):
        self._pipecat_client: Optional[PipecatMCPClient] = None

        self._connected: bool = False
        self._tools: Optional[Any] = None

        self._server_url: Optional[str] = None
        self._headers: Optional[Dict[str, str]] = None

        self._http_client: Optional[httpx.AsyncClient] = None

        self._max_timeout: int = 300

    @property
    def is_connected(self) -> bool:
        """Check if client is connected to MCP server."""
        return self._connected

    async def _fetch_max_timeout(self) -> int:
        """Fetch max timeout from dynamic config.

        Returns:
            Max timeout in seconds (from dynamic config, fallback 300)
        """

        return await BB_MCP_MAX_TIMEOUT()

    def _get_bounded_timeout(self, timeout: Optional[int]) -> int:
        """Get timeout value bounded to prevent indefinite hangs.

        Args:
            timeout: Requested timeout in seconds

        Returns:
            Bounded timeout between 1 and _max_timeout seconds
        """
        timeout_seconds = timeout or DEFAULT_MCP_TIMEOUT
        return max(1, min(timeout_seconds, self._max_timeout))

    async def connect(
        self,
        server_url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> bool:
        """
        Connect to Storefront MCP server via HTTP.

        Args:
            server_url: The Storefront MCP URL
                (e.g., https://shop.myshopify.com/api/mcp)
            headers: Optional headers for authentication (API keys, bearer tokens)
            timeout: Optional timeout override (used for connection validation)

        Returns:
            True if connection successful, False otherwise
        """
        if self._connected:
            logger.warning("MCP client already connected, disconnecting first")
            await self.disconnect()

        self._max_timeout = await self._fetch_max_timeout()

        timeout_seconds = self._get_bounded_timeout(timeout)

        try:
            logger.info(f"Connecting to Storefront MCP server: {server_url}")

            self._server_url = server_url
            self._headers = headers or {}

            self._http_client = httpx.AsyncClient(headers=self._headers)

            server_params = StreamableHttpParameters(
                url=server_url,
                headers=self._headers,
            )
            self._pipecat_client = PipecatMCPClient(server_params=server_params)

            logger.debug(f"Validating MCP connection with {timeout_seconds}s timeout")
            await asyncio.wait_for(
                self._pipecat_client.get_tools_schema(), timeout=timeout_seconds
            )

            self._connected = True
            logger.info("Successfully connected to Storefront MCP server")
            return True

        except asyncio.TimeoutError:
            logger.error(f"Connection to MCP server timed out after {timeout_seconds}s")
            await self._cleanup_connection()
            return False
        except Exception as e:
            logger.error(
                f"Failed to connect to MCP server: {type(e).__name__}: {str(e)}"
            )
            await self._cleanup_connection()
            return False

    async def _cleanup_connection(self) -> None:
        """Internal cleanup helper to reset connection state."""
        self._connected = False
        self._pipecat_client = None
        self._server_url = None
        self._headers = None
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

    async def register_tools(
        self, llm: Any, timeout: Optional[int] = None
    ) -> Optional[Any]:
        """
        Register MCP tools with the LLM using Pipecat's public API.

        Args:
            llm: The LLM service to register tools with
            timeout: Optional timeout for tool registration

        Returns:
            ToolsSchema object with registered tools, or None on failure
        """
        if not self._connected or self._pipecat_client is None:
            logger.error("Cannot register tools: MCP client not connected")
            return None

        timeout_seconds = self._get_bounded_timeout(timeout)

        try:
            logger.info("Registering MCP tools with LLM (via Pipecat)")

            logger.debug("Fetching tools schema from MCP server via Pipecat...")
            tools_schema = await asyncio.wait_for(
                self._pipecat_client.get_tools_schema(), timeout=timeout_seconds
            )

            if not tools_schema:
                logger.error("MCP server returned empty tools schema")
                return None

            logger.info(
                f"Retrieved {len(tools_schema.standard_tools)} tools from MCP server"
            )
            for tool in tools_schema.standard_tools:
                logger.debug(f"  - Tool: {tool.name}")

            logger.debug("Registering tools schema with LLM...")
            await asyncio.wait_for(
                self._pipecat_client.register_tools_schema(tools_schema, llm),
                timeout=timeout_seconds,
            )

            self._tools = tools_schema
            logger.info("Successfully registered MCP tools with LLM")
            return self._tools

        except asyncio.TimeoutError:
            logger.error(f"Timeout registering MCP tools after {timeout_seconds}s")
            return None
        except Exception as e:
            logger.error(
                f"Failed to register MCP tools: {type(e).__name__}: {str(e)}",
                exc_info=True,
            )
            return None

    async def disconnect(self) -> None:
        """
        Disconnect from MCP server and cleanup resources.

        Ensures all resources are released even if cleanup fails.
        Logs all errors but does not re-raise to prevent blocking other cleanup.
        """
        if not self._connected:
            return

        logger.info("Disconnecting from MCP server")

        if self._pipecat_client:
            try:
                await self._pipecat_client.cleanup()
            except Exception as e:
                logger.warning(f"Error during Pipecat client cleanup: {e}")

        await self._cleanup_connection()

        logger.info("Successfully disconnected from MCP server")

    def get_tools(self) -> Optional[Any]:
        """
        Get the registered tools.

        Returns:
            ToolsSchema with registered MCP tools, or None if not registered
        """
        return self._tools

    def get_flow_global_functions(self) -> Optional[List[FlowsFunctionSchema]]:
        """Convert MCP tools to FlowsFunctionSchema for use as global functions.

        This allows MCP tools to be passed to FlowManager's global_functions parameter,
        ensuring they persist across node transitions instead of being cleared.

        Returns:
            List of FlowsFunctionSchema objects, or None if no tools registered
        """
        if not self._tools or not self._pipecat_client:
            return None

        functions = []
        for tool in self._tools.standard_tools:
            handler = self._create_mcp_tool_handler(tool.name)

            functions.append(
                FlowsFunctionSchema(
                    name=tool.name,
                    description=tool.description,
                    properties=tool.properties,
                    required=tool.required,
                    handler=handler,
                )
            )

        logger.info(f"Converted {len(functions)} MCP tools to FlowsFunctionSchema")
        return functions

    async def execute_tool(
        self, tool_name: str, args: Dict[str, Any], timeout: Optional[int] = None
    ) -> str:
        """Execute an MCP tool by name with the given arguments.

        Uses the shared HTTP client for connection pooling. No private Pipecat
        APIs are accessed - only the MCP SDK's public streamable_http_client.

        Args:
            tool_name: Name of the MCP tool to execute
            args: Tool arguments dictionary
            timeout: Optional timeout in seconds (defaults to DEFAULT_MCP_TIMEOUT,
                     bounded to _MAX_TIMEOUT)

        Returns:
            Tool response as string

        Raises:
            RuntimeError: If MCP client is not connected
            asyncio.TimeoutError: If tool execution exceeds timeout
            Exception: Propagates tool execution errors
        """
        if not self._connected or self._http_client is None:
            raise RuntimeError("MCP client is not connected")

        if self._server_url is None:
            raise RuntimeError("MCP server URL is not configured")

        timeout_seconds = self._get_bounded_timeout(timeout)

        logger.debug(f"Calling MCP tool '{tool_name}' with {timeout_seconds}s timeout")

        try:
            async with streamable_http_client(
                self._server_url, http_client=self._http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    results = await asyncio.wait_for(
                        session.call_tool(name=tool_name, arguments=args),
                        timeout=timeout_seconds,
                    )

                    response = ""
                    if results:
                        if hasattr(results, "content") and results.content:
                            for content in results.content:
                                if hasattr(content, "text") and content.text:
                                    response += content.text

                    return response

        except asyncio.TimeoutError:
            logger.error(f"MCP tool '{tool_name}' timed out after {timeout_seconds}s")
            raise
        except Exception as e:
            logger.error(
                f"MCP tool execution failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    def _create_mcp_tool_handler(self, tool_name: str):
        """Create a handler function for an MCP tool.

        Args:
            tool_name: Name of the MCP tool

        Returns:
            Async handler function compatible with FlowsFunctionSchema
        """

        async def handler(args: Dict[str, Any], flow_manager: Any) -> Any:
            """Handler that executes the MCP tool via the underlying client.

            Returns the tool result as a string for the LLM.
            """
            logger.info(f"Executing MCP tool '{tool_name}'")
            logger.debug(f"MCP tool '{tool_name}' args: {args}")

            try:
                response = await self.execute_tool(tool_name, args)

                if response:
                    logger.info(f"MCP tool '{tool_name}' completed successfully")
                    logger.debug(f"MCP tool '{tool_name}' result: {response[:200]}...")
                    return {"result": response}
                else:
                    logger.warning(f"MCP tool '{tool_name}' returned empty response")
                    return {"result": "Sorry, the tool returned no results."}

            except RuntimeError:
                return {"result": "Sorry, this tool is temporarily unavailable."}
            except Exception:
                logger.error(f"Error executing MCP tool '{tool_name}'", exc_info=True)
                return {"result": "Sorry, there was an error while calling this tool."}

        return handler
