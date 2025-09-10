import httpx
import json
import base64
from typing import Dict, Any, Optional, Callable
from app.agents.voice.automatic.utils.session_context import SessionContext


from app.core.config import MCP_CLIENT_TIMEOUT, is_neurolink_shop
from app.core.logger import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.schemas.function_schema import FunctionSchema
from app.agents.voice.automatic.types.models import (
    JSONRPCResponse,
    ToolCallResult,
    MCPTool
)

from app.agents.voice.automatic.services.charts.mcp.utils import _store_ui_components_from_mcp

class StreamableHTTPTransport:
    """Handles JSON-RPC 2.0 over HTTP with support for both standard MCP and neurolink servers."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any], shop_id: str = None):
        logger.debug(f"StreamableHTTPTransport initialized with server_url: '{server_url}'")
        if not server_url or not isinstance(server_url, str):
            raise ValueError("MCP server URL must be a non-empty string.")

        self._server_url = server_url.strip()
        self._auth_token = auth_token
        self._context = context
        self._context_b64 = base64.b64encode(json.dumps(context).encode()).decode()
        self._client = httpx.AsyncClient(timeout=MCP_CLIENT_TIMEOUT)
        self._demo_mode = context.get("enableDemoMode", False)
        self._shop_id = shop_id or context.get("shopId", "")
        
        # Determine if this is a neurolink server using the new config
        self._is_neurolink = is_neurolink_shop(self._shop_id)
        
        logger.info(f"MCP Client initialized: shopId='{self._shop_id}', isNeurolink={self._is_neurolink}, serverUrl='{server_url}'")
        
        if self._is_neurolink:
            logger.info(f"Using Neurolink server for shop_id: {self._shop_id}")
            # Log minimal sanitized context at debug level
            safe_ctx = dict(context)
            for k in ("juspayToken", "breezeToken", "authToken", "x-auth-token", "userEmail"):
                if k in safe_ctx:
                    safe_ctx[k] = "***"
            logger.debug(f"MCP context (sanitized): {json.dumps(safe_ctx, indent=2)}")
            # Avoid logging raw base64 of x-context which contains sensitive data
            
            # For verification purposes, log only non-sensitive decoded fields at debug level
            try:
                decoded_context = json.loads(base64.b64decode(self._context_b64).decode())
                safe_decoded = {k: v for k, v in decoded_context.items() 
                               if k not in ("juspayToken", "breezeToken", "authToken", "x-auth-token", "userEmail")}
                logger.debug(f"Decoded context (sanitized): {json.dumps(safe_decoded, indent=2)}")
            except Exception as e:
                logger.error(f"Failed to decode context for verification: {e}")
        else:
            logger.info(f"Using standard MCP server for shop_id: {self._shop_id}")
        
        # Log only sanitized context at debug level
        safe_context = dict(context)
        for k in ("juspayToken", "breezeToken", "authToken", "x-auth-token", "userEmail"):
            if k in safe_context:
                safe_context[k] = "***"
        logger.debug(f"Context being sent to MCP server (sanitized): {safe_context}")
        # Remove logging of raw base64 context as it contains sensitive data

    async def post(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Performs a JSON-RPC POST request and handles response."""
        if self._is_neurolink:
            return await self._post_neurolink(method, params)
        else:
            return await self._post_standard_mcp(method, params)
    
    async def _post_standard_mcp(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Standard MCP server communication (non-streaming JSON-RPC)"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-context": self._context_b64,
        }
        if self._auth_token:
            headers["x-auth-token"] = self._auth_token
        
        json_rpc_payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        
        try:
            logger.debug(f"POST standard MCP: {self._server_url} method={method}")
            logger.debug(f"JSON-RPC payload: {json_rpc_payload}")
            safe_headers = dict(headers)
            if "x-auth-token" in safe_headers:
                safe_headers["x-auth-token"] = "***"
            logger.debug(f"Headers: {safe_headers}")
            
            response = await self._client.post(self._server_url, headers=headers, json=json_rpc_payload)
            
            logger.info(f"Response status: {response.status_code}")
            
            if response.is_error:
                response.raise_for_status()
            
            response_data = response.json()
            logger.debug(f"Standard MCP response: {response_data}")
            
            return response_data
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error on method {method}: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"HTTP Error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"Network request error on method {method}: {e}")
            raise RuntimeError(f"Network Error: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected transport error occurred on method {method}: {e}")
            raise
    
    async def _post_neurolink(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Neurolink server communication (streaming SSE format)"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-context": self._context_b64,
        }
        if self._auth_token:
            headers["x-auth-token"] = self._auth_token
        json_rpc_payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        
        query_params = {}
        if self._demo_mode:
            query_params["demoMode"] = "true"

        try:
            logger.debug(f"POST neurolink: {self._server_url} method={method}")
            logger.debug(f"JSON-RPC payload: {json_rpc_payload}")
            safe_headers = dict(headers)
            if "x-auth-token" in safe_headers:
                safe_headers["x-auth-token"] = "***"
            logger.debug(f"Headers: {safe_headers}")
            async with self._client.stream("POST", self._server_url, headers=headers, json=json_rpc_payload, params=query_params) as response:
                logger.debug(f"Response status: {response.status_code}, content-type: {response.headers.get('content-type', 'Not specified')}")
                
                if response.is_error:
                    await response.aread()
                    response.raise_for_status()

                async for line in response.aiter_lines():
                    logger.debug(f"Received line from server: {line}")
                    if line.startswith("data:"):
                        json_str = line[len("data:"):].strip()
                        logger.info(f"Extracted JSON string from stream: {json_str}")
                        try:
                            # First, try to parse as raw JSON to see what we're getting
                            raw_data = json.loads(json_str)
                            logger.info(f"Raw response data: {raw_data}")
                            logger.info(f"Raw response keys: {list(raw_data.keys()) if isinstance(raw_data, dict) else 'Not a dict'}")
                            
                            # Check if this is a neurolink-specific event format
                            if isinstance(raw_data, dict) and "event" in raw_data:
                                event_type = raw_data.get("event")
                                logger.info(f"Received neurolink event: {event_type}")
                                
                                # Skip status/progress events and wait for actual data
                                if event_type in ["request_received", "processing", "progress"]:
                                    logger.debug(f"Skipping status event: {event_type}")
                                    continue
                                
                                # Check if this contains tools data anywhere in the response
                                tools_array = self._find_tools_in_response(raw_data)
                                if tools_array:
                                    logger.info(f"Found {len(tools_array)} tools in neurolink response")
                                    logger.debug(f"First few tools: {tools_array[:3] if len(tools_array) > 3 else tools_array}")
                                    # Convert neurolink format to JSON-RPC format
                                    converted_response = {
                                        "jsonrpc": "2.0",
                                        "id": 1,
                                        "result": {"tools": tools_array}
                                    }
                                    logger.info(f"Converted to JSON-RPC format with {len(tools_array)} tools")
                                    return converted_response
                                
                                # Check if this is a call response
                                if event_type == "tool_response" or "result" in raw_data:
                                    logger.info("Found tool call result in neurolink response")
                                    # Convert neurolink format to JSON-RPC format
                                    converted_response = {
                                        "jsonrpc": "2.0", 
                                        "id": 1,
                                        "result": {
                                            "content": [{"type": "text", "text": raw_data.get("result", raw_data)}]
                                        }
                                    }
                                    logger.info(f"Converted tool result to JSON-RPC format: {converted_response}")
                                    return converted_response
                                
                                # If it's an error event
                                if event_type == "error" or "error" in raw_data:
                                    logger.error(f"Received error from neurolink: {raw_data}")
                                    converted_response = {
                                        "jsonrpc": "2.0",
                                        "id": 1,
                                        "error": {
                                            "code": -1,
                                            "message": str(raw_data.get("error", raw_data.get("message", "Unknown error")))
                                        }
                                    }
                                    return converted_response
                            
                            # If it's already in JSON-RPC format, validate it
                            elif "jsonrpc" in raw_data:
                                validated_response = JSONRPCResponse.model_validate_json(json_str)
                                response_dict = validated_response.model_dump(by_alias=True, exclude_none=True)

                                if isinstance(validated_response.result, ToolCallResult):
                                    for i, item in enumerate(validated_response.result.content):
                                        response_dict["result"]["content"][i]["text"] = item.text

                                return response_dict
                            
                            # If it's not an event format but might contain tools data, search for tools
                            else:
                                tools_array = self._find_tools_in_response(raw_data)
                                if tools_array:
                                    logger.info(f"Found {len(tools_array)} tools in non-event response")
                                    logger.debug(f"First few tools: {tools_array[:3] if len(tools_array) > 3 else tools_array}")
                                    # Convert to JSON-RPC format
                                    converted_response = {
                                        "jsonrpc": "2.0",
                                        "id": 1,
                                        "result": {"tools": tools_array}
                                    }
                                    logger.info(f"Converted non-event response to JSON-RPC format with {len(tools_array)} tools")
                                    return converted_response
                                
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode JSON from stream: {json_str}")
                            logger.error(f"JSON decode error: {e}")
                            raise ValueError("Received malformed JSON from server.")
                        except Exception as e: # Catches Pydantic's ValidationError
                            logger.error(f"Response validation failed: {e}")
                            logger.error(f"Trying to validate JSON: {json_str}")
                            # Don't raise immediately, continue processing other events
                            logger.warning(f"Continuing to process other events after validation error: {e}")
                            continue
                    else:
                        logger.debug(f"Skipping non-data line: {line}")

                logger.warning("Neurolink server stream ended without sending expected data.")
                # Return empty tools response for now to allow graceful fallback
                return {
                    "jsonrpc": "2.0",
                    "id": 1, 
                    "result": {"tools": []}
                }

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error on neurolink method {method}: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"HTTP Error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"Network request error on neurolink method {method}: {e}")
            raise RuntimeError(f"Network Error: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected transport error occurred on neurolink method {method}: {e}")
            raise

    def _find_tools_in_response(self, data):
        """Recursively search for tools array in the response data"""
        if isinstance(data, dict):
            # Check direct tools key
            if "tools" in data and isinstance(data["tools"], list):
                return data["tools"]
            # Check in result key
            if "result" in data and isinstance(data["result"], dict):
                if "tools" in data["result"] and isinstance(data["result"]["tools"], list):
                    return data["result"]["tools"]
            # Recursively search in all values
            for key, value in data.items():
                tools = self._find_tools_in_response(value)
                if tools:
                    return tools
        elif isinstance(data, list):
            # Check if this is already a tools array
            if all(isinstance(item, dict) and "name" in item for item in data):
                return data
            # Recursively search in list items
            for item in data:
                tools = self._find_tools_in_response(item)
                if tools:
                    return tools
        return None

    async def close(self):
        await self._client.aclose()

class MCPClient:
    """A service to list, register, and call tools from a remote MCP server."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any], session_context: SessionContext, enable_chart: bool):
        shop_id = context.get("shopId", "")
        self._transport = StreamableHTTPTransport(server_url, auth_token, context, shop_id)
        self._session_context = session_context
        self._llm = None
        self._enable_chart = enable_chart

    async def register_tools(self, llm, selective_functions) -> ToolsSchema:
        """Lists tools and registers them with the given LLM processor."""
        self._llm = llm
        logger.info("Registering tools from custom MCP client...")
        selective_functions_set = set(selective_functions)
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
            logger.info(f"Received {len(raw_tools)} tools from MCP server")
            
            tools_to_process = raw_tools
            if len(selective_functions) > 0:
                # If selective functions are specified, use only those
                selective_tools_to_register = []
                for tool_data in raw_tools:
                    tool_name = tool_data["name"]
                    if tool_name in selective_functions_set:
                        selective_tools_to_register.append(tool_data)
                logger.info(f"Found {len(selective_tools_to_register)} tools matching selective functions")
                tools_to_process = selective_tools_to_register
            
            # OpenAI has a hard limit of 128 tools - enforce this limit
            if len(tools_to_process) > 128:
                logger.warning(f"Too many tools ({len(tools_to_process)}) for OpenAI limit (128). Truncating to first 128 tools.")
                tools_to_process = tools_to_process[:128]
            
            # Log which tools we're actually registering
            tool_names_to_register = [tool["name"] for tool in tools_to_process]
            logger.info(f"Final tools to register ({len(tool_names_to_register)}): {tool_names_to_register}")
            
            converted_tools = []
            for tool_data in tools_to_process:
                tool_name = tool_data["name"]
                logger.debug(f"Registering remote tool: {tool_name}")
                
                # Debug schema for specific problematic tools
                if "getBusinessAnalyticsCounts" in tool_name:
                    logger.info(f"DEBUG: Schema for {tool_name}:")
                    logger.info(f"  Raw tool data: {json.dumps(tool_data, indent=2)}")
                
                function_schema = self._convert_schema(tool_data)
                converted_tools.append(function_schema)
                
                # Log the converted schema for problematic tools
                if "getBusinessAnalyticsCounts" in tool_name:
                    logger.info(f"  Converted function schema: name={function_schema.name}")
                    logger.info(f"  Properties: {function_schema.properties}")
                    logger.info(f"  Required: {function_schema.required}")
                
                # Register using the potentially shortened name from the schema
                llm.register_function(function_schema.name, self._mcp_tool_wrapper)
                
            logger.info(f"Successfully registered {len(converted_tools)} remote tools.")
            return ToolsSchema(standard_tools=converted_tools)
        except Exception as e:
            logger.error(f"Failed to register tools from remote server: {e}")
            return ToolsSchema(standard_tools=[])

    def _convert_schema(self, tool_data: Dict[str, Any]) -> FunctionSchema:
        """Converts a raw MCP tool dict to a PipeCat FunctionSchema."""
        tool = MCPTool.model_validate(tool_data)
        
        # OpenAI has a 64-character limit on function names
        original_name = tool.name
        if len(original_name) > 64:
            # Create a shorter name by truncating and adding a hash suffix
            import hashlib
            hash_suffix = hashlib.md5(original_name.encode()).hexdigest()[:8]
            truncated_name = original_name[:50] + "_" + hash_suffix  # 50 + 1 + 8 = 59 chars
            
            logger.info(f"Tool name too long ({len(original_name)} chars): '{original_name}' -> '{truncated_name}'")
            # Store the mapping for tool calls
            self._name_mapping = getattr(self, '_name_mapping', {})
            self._name_mapping[truncated_name] = original_name
            function_name = truncated_name
        else:
            function_name = original_name
        
        return FunctionSchema(
            name=function_name,
            description=tool.description,
            properties=tool.input_schema.properties,
            required=tool.input_schema.required or [],
        )

    async def _mcp_tool_wrapper(self, params) -> None:
        """This wrapper is called by the LLM. It then calls the remote tool."""
        # Extract parameters from the params object
        function_name = getattr(params, 'function_name', 'unknown')
        arguments = getattr(params, 'arguments', {})
        result_callback = getattr(params, 'result_callback', None)
        
        # Check if this is a truncated name that needs to be mapped back
        name_mapping = getattr(self, '_name_mapping', {})
        actual_function_name = name_mapping.get(function_name, function_name)
        
        if function_name != actual_function_name:
            logger.debug(f"Mapped truncated name '{function_name}' back to original '{actual_function_name}'")
        
        logger.debug(f"LLM called MCP tool: {actual_function_name} with args: {arguments}")
        
        if not result_callback:
            logger.error(f"No result_callback found for MCP function {function_name}")
            return
            
        await self._call_tool(actual_function_name, arguments, result_callback)

    async def _call_tool(
        self, function_name: str, arguments: Dict[str, Any], result_callback: Callable
    ) -> None:
        """Sends the 'tools/call' request to the remote server."""
        try:
            # Debug for problematic tools
            if "getBusinessAnalyticsCounts" in function_name:
                logger.info(f"DEBUG: Tool call for {function_name}")
                logger.info(f"  LLM sent arguments: {json.dumps(arguments, indent=2)}")
                logger.info(f"  Context merchantId: {self._transport._context.get('merchantId')}")
                
                # If this is a timeframe-based call, we need to convert it to date range
                if 'timeframe' in arguments and 'startDate' not in arguments:
                    logger.warning(f"Tool {function_name} received timeframe '{arguments['timeframe']}' but server expects startDate/endDate")
                    
                    # Convert timeframe to actual dates
                    from datetime import datetime, timedelta
                    now = datetime.now()
                    
                    timeframe = arguments['timeframe']
                    if timeframe == 'this_week':
                        # Start of this week (Monday)
                        days_since_monday = now.weekday()
                        start_date = now - timedelta(days=days_since_monday)
                        end_date = now
                    elif timeframe == 'last_week':
                        # Start and end of last week
                        days_since_monday = now.weekday()
                        this_monday = now - timedelta(days=days_since_monday)
                        start_date = this_monday - timedelta(days=7)
                        end_date = this_monday - timedelta(days=1)
                    elif timeframe == 'this_month':
                        # Start of this month
                        start_date = now.replace(day=1)
                        end_date = now
                    elif timeframe == 'last_month':
                        # Start and end of last month
                        first_day_this_month = now.replace(day=1)
                        end_date = first_day_this_month - timedelta(days=1)
                        start_date = end_date.replace(day=1)
                    else:
                        # Default to this week
                        days_since_monday = now.weekday()
                        start_date = now - timedelta(days=days_since_monday)
                        end_date = now
                    
                    # Remove timeframe and add startDate/endDate
                    del arguments['timeframe']
                    arguments['startDate'] = start_date.strftime('%Y-%m-%d')
                    arguments['endDate'] = end_date.strftime('%Y-%m-%d')
                    logger.info(f"  Converted timeframe '{timeframe}' to startDate: {arguments['startDate']}, endDate: {arguments['endDate']}")
                    
                # If merchantIds is missing but we have merchantId in context, add it
                if 'merchantIds' not in arguments and self._transport._context.get('merchantId'):
                    arguments['merchantIds'] = [self._transport._context['merchantId']]
                    logger.info(f"  Added merchantIds from context: {arguments['merchantIds']}")
            
            params = {"name": function_name, "arguments": arguments}
            logger.info(f"Calling MCP tool '{function_name}' with arguments: {arguments}")
            logger.debug(f"Tool call params: {params}")
            
            # The transport.post method will automatically include x-context and x-auth-token headers
            response_dict = await self._transport.post(method="tools/call", params=params)
            
            logger.debug(f"Tool call response received for '{function_name}': {response_dict}")

            if response_dict.get("error"):
                error_details = response_dict['error']
                logger.error(f"Tool call error for '{function_name}': {error_details}")
                raise RuntimeError(f"JSON-RPC Error calling tool: {error_details}")

            result_content = response_dict.get("result", {}).get("content", [])
            text_response = " ".join(
                json.dumps(item.get("text")) for item in result_content if item.get("type") == "text"
            )

            if self._enable_chart:
                ui_components = []
                for item in result_content:
                    if item.get("type") == "text" and isinstance(item.get("text"), dict) and item["text"].get("uiComponent") is True:
                        ui_components.append(item["text"])

                if ui_components:
                    await _store_ui_components_from_mcp(self, ui_components)
            
            
                if not text_response and ui_components:
                    # Extract cleanVoiceDescription from UI components metadata
                    ui_text_parts = []
                    for ui_component in ui_components:
                        metadata = ui_component.get("metadata", {})
                        clean_voice_description = metadata.get("cleanVoiceDescription")
                        if clean_voice_description and str(clean_voice_description).strip():
                            ui_text_parts.append(str(clean_voice_description).strip())
                
                    text_response = " ".join(ui_text_parts) if ui_text_parts else "Tool executed successfully but returned no text."
                
                if ui_components:
                    logger.debug(f"Tool '{function_name}' also returned {len(ui_components)} UI components")
            
            if not text_response:
                text_response = "Tool executed successfully but returned no text."

            logger.debug(f"Tool '{function_name}' returned: {text_response}")
                
            await result_callback(text_response)

        except Exception as e:
            logger.error(f"Failed to call tool '{function_name}': {e}")
            await result_callback(f"Error: Could not execute tool {function_name}.")

    async def close(self):
        await self._transport.close()
