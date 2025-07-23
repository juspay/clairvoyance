import httpx
import json
import base64
from typing import Dict, Any, Optional, Callable

from app.core.logger import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.schemas.function_schema import FunctionSchema
from app.agents.voice.automatic.types.models import (
    JSONRPCResponse,
    ToolCallResult,
    MCPTool
)

class StreamableHTTPTransport:
    """Handles JSON-RPC 2.0 over streaming HTTP with custom headers."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any]):
        logger.debug(f"StreamableHTTPTransport initialized with server_url: '{server_url}'")
        if not server_url or not isinstance(server_url, str):
            raise ValueError("MCP server URL must be a non-empty string.")

        self._server_url = server_url.strip()
        self._auth_token = auth_token
        self._context_b64 = base64.b64encode(json.dumps(context).encode()).decode()
        self._client = httpx.AsyncClient(timeout=15)
        self._demo_mode = context.get("enableDemoMode", False)

    async def post(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Performs a JSON-RPC POST request and handles streaming response."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": self._auth_token if self._auth_token is not None else "",
            "x-context": self._context_b64,
        }
        json_rpc_payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        
        query_params = {}
        if self._demo_mode:
            query_params["demoMode"] = "true"

        try:
            logger.info(f"Attempting to POST to: {self._server_url} with payload: {json_rpc_payload} and headers: {headers}")
            async with self._client.stream("POST", self._server_url, headers=headers, json=json_rpc_payload, params=query_params) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        json_str = line[len("data:"):].strip()
                        try:
                            validated_response = JSONRPCResponse.model_validate_json(json_str)
                            response_dict = validated_response.model_dump(by_alias=True, exclude_none=True)

                            if isinstance(validated_response.result, ToolCallResult):
                                for i, item in enumerate(validated_response.result.content):
                                    response_dict["result"]["content"][i]["text"] = item.text

                            return response_dict
                        except json.JSONDecodeError:
                            logger.error(f"Failed to decode JSON from stream: {json_str}")
                            raise ValueError("Received malformed JSON from server.")
                        except Exception as e: # Catches Pydantic's ValidationError
                            logger.error(f"Response validation failed: {e}")
                            raise ValueError(f"Server response did not match expected schema: {e}")

                raise ValueError("Server stream ended without sending a data message.")

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error on method {method}: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"HTTP Error: {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Network request error on method {method}: {e}")
            raise RuntimeError(f"Network Error: {e}")
        except Exception as e:
            logger.error(f"An unexpected transport error occurred on method {method}: {e}")
            raise

    async def close(self):
        await self._client.aclose()

class MCPClient:
    """A service to list, register, and call tools from a remote MCP server."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any]):
        self._transport = StreamableHTTPTransport(server_url, auth_token, context)
        self._llm = None

    async def register_tools(self, llm) -> ToolsSchema:
        """Lists tools and registers them with the given LLM processor."""
        self._llm = llm
        logger.info("Registering tools from custom MCP client...")
        
        try:
            response_dict = await self._transport.post(method="tools/list")
            
            if response_dict.get("error"):
                error_details = response_dict['error']
                logger.error(f"Received JSON-RPC error when listing tools: {error_details}")
                raise RuntimeError(f"JSON-RPC Error listing tools: {error_details}")

            if not response_dict.get("result") or not response_dict["result"].get("tools"):
                logger.warning("Tool registration response was successful but contained no tools.")
                return ToolsSchema(standard_tools=[])

            raw_tools = response_dict["result"]["tools"]
            
            converted_tools = []
            for tool_data in raw_tools:
                tool_name = tool_data["name"]
                logger.debug(f"Registering remote tool: {tool_name}")
                
                function_schema = self._convert_schema(tool_data)
                converted_tools.append(function_schema)
                
                llm.register_function(tool_name, self._mcp_tool_wrapper)
                
            logger.info(f"Successfully registered {len(converted_tools)} remote tools.")
            return ToolsSchema(standard_tools=converted_tools)
        except Exception as e:
            logger.error(f"Failed to register tools from remote server: {e}")
            return ToolsSchema(standard_tools=[])

    def _convert_schema(self, tool_data: Dict[str, Any]) -> FunctionSchema:
        """Converts a raw MCP tool dict to a PipeCat FunctionSchema."""
        tool = MCPTool.model_validate(tool_data)
        return FunctionSchema(
            name=tool.name,
            description=tool.description,
            properties=tool.input_schema.properties,
            required=tool.input_schema.required or [],
        )

    async def _mcp_tool_wrapper(
        self, function_name: str, tool_call_id: str, arguments: Dict[str, Any],
        llm: Any, context: Any, result_callback: Callable
    ) -> None:
        """This wrapper is called by the LLM. It then calls the remote tool."""
        logger.debug(f"LLM called tool: {function_name} with args: {arguments}")
        await self._call_tool(function_name, arguments, result_callback)

    async def _call_tool(
        self, function_name: str, arguments: Dict[str, Any], result_callback: Callable
    ) -> None:
        """Sends the 'tools/call' request to the remote server."""
        try:
            params = {"name": function_name, "arguments": arguments}
            response_dict = await self._transport.post(method="tools/call", params=params)
            logger.info(f"Calling the MCP tool {function_name} and {params}")

            if response_dict.get("error"):
                raise RuntimeError(f"JSON-RPC Error calling tool: {response_dict['error']}")

            result_content = response_dict.get("result", {}).get("content", [])
            
            text_response = " ".join(
                json.dumps(item.get("text")) for item in result_content if item.get("type") == "text"
            )

            if not text_response:
                text_response = "Tool executed successfully but returned no text."

            logger.debug(f"Tool '{function_name}' returned: {text_response}")
            await result_callback(text_response)

        except Exception as e:
            logger.error(f"Failed to call tool '{function_name}': {e}")
            await result_callback(f"Error: Could not execute tool {function_name}.")

    async def close(self):
        await self._client.aclose()
