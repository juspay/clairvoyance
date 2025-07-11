import httpx
import asyncio
import json
import base64
from typing import List, Dict, Any, Optional, Callable

from loguru import logger
# Using the correct import paths from the project
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.schemas.function_schema import FunctionSchema

class StreamableHTTPTransport:
    """Handles JSON-RPC 2.0 over streaming HTTP with custom headers."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any]):
        logger.debug(f"StreamableHTTPTransport initialized with server_url: '{server_url}'")
        if not server_url or not isinstance(server_url, str):
            raise ValueError("MCP server URL must be a non-empty string.")

        self._server_url = server_url.strip()
        self._auth_token = auth_token
        self._context_b64 = base64.b64encode(json.dumps(context).encode()).decode()
        self._client = httpx.AsyncClient(timeout=60)

    async def post(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Performs a JSON-RPC POST request."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-auth-token": self._auth_token,
            "x-context": self._context_b64,
        }
        json_rpc_payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}

        try:
            logger.info(f"Attempting to POST to full URL: '{self._server_url} and {json_rpc_payload}'")
            async with self._client.stream("POST", self._server_url, headers=headers, json=json_rpc_payload) as response:
                logger.debug(f"Received response object: status_code={response.status_code}, headers={response.headers}")
                response.raise_for_status()
                raw_data = await response.aread()
                
                logger.debug(f"Received raw response data (bytes): {raw_data!r}")
                
                # Decode the full response
                full_response_str = raw_data.decode('utf-8', errors='ignore').strip()
                logger.debug(f"Decoded and stripped string: {full_response_str!r}")

                # Find the line that starts with "data:" and extract the JSON
                json_str = ""
                for line in full_response_str.splitlines():
                    if line.startswith("data:"):
                        json_str = line[5:].strip()
                        break
                
                logger.debug(f"Final string for JSON parsing: {json_str!r}")

                if not json_str:
                    raise ValueError("Server returned an empty or non-JSON response body.")
                    
                return json.loads(json_str)
        except Exception as e:
            logger.error(f"Transport error on method {method}: {e}")
            raise

    async def close(self):
        await self._client.aclose()

class CustomMCPClient:
    """A service to list, register, and call tools from a remote MCP server."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any]):
        self._transport = StreamableHTTPTransport(server_url, auth_token, context)
        self._llm = None

    async def register_tools(self, llm) -> ToolsSchema:
        """Lists tools and registers them with the given LLM processor."""
        self._llm = llm
        logger.info("Registering tools from custom MCP client...")
        
        response = await self._transport.post(method="tools/list")
        if "error" in response:
            raise RuntimeError(f"JSON-RPC Error listing tools: {response['error']}")

        raw_tools = response.get("result", {}).get("tools", [])
        
        converted_tools = []
        for tool_data in raw_tools:
            tool_name = tool_data.get("name")
            logger.debug(f"Registering remote tool: {tool_name}")
            
            # 1. Convert schema to PipeCat's format
            function_schema = self._convert_schema(tool_data)
            converted_tools.append(function_schema)
            
            # 2. Register a handler function with the LLM
            llm.register_function(tool_name, self._mcp_tool_wrapper)
            
        logger.info(f"Successfully registered {len(converted_tools)} remote tools.")
        return ToolsSchema(standard_tools=converted_tools)

    def _convert_schema(self, tool_data: Dict[str, Any]) -> FunctionSchema:
        """Converts a raw MCP tool dict to a PipeCat FunctionSchema."""
        return FunctionSchema(
            name=tool_data.get("name"),
            description=tool_data.get("description"),
            properties=tool_data.get("inputSchema", {}).get("properties", {}),
            required=tool_data.get("inputSchema", {}).get("required", []),
        )

    async def _mcp_tool_wrapper(
        self, function_name: str, tool_call_id: str, arguments: Dict[str, Any],
        llm: Any, context: Any, result_callback: Callable
    ):
        """This wrapper is called by the LLM. It then calls the remote tool."""
        logger.debug(f"LLM called tool: {function_name} with args: {arguments}")
        await self._call_tool(function_name, arguments, result_callback)

    async def _call_tool(
        self, function_name: str, arguments: Dict[str, Any], result_callback: Callable
    ):
        """Sends the 'tools/call' request to the remote server."""
        try:
            params = {"name": function_name, "arguments": arguments}
            response = await self._transport.post(method="tools/call", params=params)
            logger.info(f"Calling the MCP tool {function_name} and {params}")

            if "error" in response:
                raise RuntimeError(f"JSON-RPC Error calling tool: {response['error']}")

            result_content = response.get("result", {}).get("content", [])
            
            # Extract text from the response content
            text_response = " ".join(
                item.get("text", "") for item in result_content if item.get("type") == "text"
            )

            if not text_response:
                text_response = "Tool executed successfully but returned no text."

            logger.debug(f"Tool '{function_name}' returned: {text_response}")
            await result_callback(text_response)

        except Exception as e:
            logger.error(f"Failed to call tool '{function_name}': {e}")
            await result_callback(f"Error: Could not execute tool {function_name}.")

    async def close(self):
        await self._transport.close()

# Example of how this would be integrated into a pipeline (conceptual)
async def main():
    # This is a placeholder for a real LLM processor like OpenAILLMContext
    class MockLLM:
        def __init__(self):
            self._functions = {}
        def register_function(self, name, handler):
            print(f"[MockLLM] Registered '{name}'")
            self._functions[name] = handler
        async def call_registered_function(self, name, args):
            print(f"[MockLLM] Simulating LLM call to '{name}'")
            async def callback(result):
                print(f"[MockLLM] Received result: {result}")
            await self._functions[name](
                function_name=name, tool_call_id="123", arguments=args,
                llm=self, context={}, result_callback=callback
            )

    # -- Client Setup --
    server_url = "http://localhost:5173"
    auth_token = "aB3cDeF4gHiJkL5mNoPqRsT6uVwXyZ7aB8c"
    context = {"sessionId": "abc123"}
    
    client = CustomMCPClient(server_url, auth_token, context)
    mock_llm = MockLLM()

    try:
        # -- Register tools --
        tools_schema = await client.register_tools(mock_llm)
        print("\n--- Schema of Registered Tools ---")
        print(tools_schema.model_dump_json(indent=2))

        # -- Simulate a tool call --
        if "get_payment_details" in mock_llm._functions:
            print("\n--- Simulating Tool Call ---")
            await mock_llm.call_registered_function(
                "get_payment_details", {"payment_id": "xyz-789"}
            )
    finally:
        await client.close()

if __name__ == "__main__":
    # Note: This main block is for demonstration.
    # A real server needs to be running at the specified URL for it to work.
    asyncio.run(main())
