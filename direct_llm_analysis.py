#!/usr/bin/env python3
"""
Direct LLM analysis - call Azure OpenAI directly to see what tools it calls
for each question without any pipecat pipeline complexity.
"""
import asyncio
import json
import base64
import httpx
from typing import Dict, List, Any
from dotenv import load_dotenv
import openai
from openai import AsyncAzureOpenAI
from datetime import datetime

# Load environment variables first
load_dotenv(override=True)

# Import required components
from app.agents.voice.automatic.services.mock_stt import DEFAULT_TEST_QUESTIONS, get_question_metadata
from app.agents.voice.automatic.tools import initialize_tools
from app.agents.voice.automatic.prompts import get_system_prompt
from app.agents.voice.automatic.types import Mode, TTSProvider
from app.agents.voice.automatic.services.mcp.automatic_client import MCPClient
from app.core import config

class SimpleMCPClient:
    """Simplified MCP client for direct LLM analysis"""
    
    def __init__(self, server_url: str, auth_token: str = None, context: Dict[str, Any] = None):
        self.server_url = server_url
        self.auth_token = auth_token
        self.context = context or {}
        self.context_b64 = base64.b64encode(json.dumps(self.context).encode()).decode()
        self.client = httpx.AsyncClient(timeout=15)
        
    async def fetch_tools(self) -> List[Dict]:
        """Fetch tools from MCP server and convert to OpenAI format"""
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "x-context": self.context_b64,
            }
            if self.auth_token:
                headers["x-auth-token"] = self.auth_token
                
            json_rpc_payload = {
                "jsonrpc": "2.0", 
                "id": 1, 
                "method": "tools/list", 
                "params": {}
            }
            
            query_params = {}
            if self.context.get("enableDemoMode"):
                query_params["demoMode"] = "true"
            
            print(f"🔗 Fetching tools from MCP server: {self.server_url}")
            
            # Use streaming like the original MCP client
            async with self.client.stream(
                "POST", 
                self.server_url, 
                headers=headers, 
                json=json_rpc_payload,
                params=query_params
            ) as response:
                response.raise_for_status()
                
                # Parse streaming response
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        json_str = line[len("data:"):].strip()
                        try:
                            response_json = json.loads(json_str)
                            break
                        except json.JSONDecodeError:
                            print(f"Failed to decode JSON from stream: {json_str}")
                            continue
                else:
                    print("No data received from streaming response")
                    return []
            
            if response_json.get("error"):
                print(f"❌ MCP error: {response_json['error']}")
                return []
                
            if not response_json.get("result") or not response_json["result"].get("tools"):
                print("⚠️  No tools returned from MCP server")
                return []
                
            raw_tools = response_json["result"]["tools"]
            print(f"📦 Received {len(raw_tools)} tools from MCP server")
            
            # Convert MCP tools to OpenAI format
            openai_tools = []
            for tool_data in raw_tools:
                try:
                    openai_tool = self._convert_mcp_to_openai(tool_data)
                    openai_tools.append(openai_tool)
                    print(f"  ✅ Converted: {tool_data['name']}")
                except Exception as e:
                    print(f"  ❌ Failed to convert {tool_data.get('name', 'unknown')}: {e}")
                    
            return openai_tools
            
        except Exception as e:
            print(f"❌ Failed to fetch MCP tools: {e}")
            return []
    
    def _convert_mcp_to_openai(self, mcp_tool: Dict) -> Dict:
        """Convert MCP tool schema to OpenAI function calling format"""
        return {
            "type": "function",
            "function": {
                "name": mcp_tool["name"],
                "description": mcp_tool.get("description", ""),
                "parameters": mcp_tool.get("inputSchema", {})
            }
        }
        
    async def execute_tool(self, function_name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool via MCP server"""
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", 
                "x-context": self.context_b64,
            }
            if self.auth_token:
                headers["x-auth-token"] = self.auth_token
                
            json_rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1, 
                "method": "tools/call",
                "params": {
                    "name": function_name,
                    "arguments": arguments
                }
            }
            
            query_params = {}
            if self.context.get("enableDemoMode"):
                query_params["demoMode"] = "true"
            
            async with self.client.stream(
                "POST",
                self.server_url,
                headers=headers,
                json=json_rpc_payload,
                params=query_params
            ) as response:
                response.raise_for_status()
                
                # Parse streaming response
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        json_str = line[len("data:"):].strip()
                        try:
                            response_json = json.loads(json_str)
                            break
                        except json.JSONDecodeError:
                            continue
                else:
                    return "No response from MCP server"
            
            if response_json.get("error"):
                return f"Error: {response_json['error']}"
                
            result_content = response_json.get("result", {}).get("content", [])
            text_response = " ".join(
                item.get("text", "") for item in result_content 
                if item.get("type") == "text"
            )
            
            return text_response or "Tool executed successfully"
            
        except Exception as e:
            return f"Error executing tool: {e}"
    
    async def close(self):
        await self.client.aclose()

async def call_llm_with_tools(client: AsyncAzureOpenAI, messages: List[Dict], tools: List[Dict], mcp_client: SimpleMCPClient = None) -> Dict:
    """Call the LLM with optional second turn if get_current_time is called"""
    
    try:
        # First LLM call
        response = await client.chat.completions.create(
            model=config.AZURE_OPENAI_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=1000
        )
        
        message = response.choices[0].message
        all_function_calls = []
        
        # Extract first round function calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                all_function_calls.append({
                    'name': tool_call.function.name,
                    'arguments': json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                })
        
        # Check if time-related tool was called (triggers second turn)
        time_called = any(fc['name'] in ['get_current_time', 'utility-server_getCurrentTime', 'utility-server_generateTimestamp'] for fc in all_function_calls)
        
        if time_called and message.tool_calls:
            # Prepare messages for second turn
            current_messages = messages.copy()
            
            # Add assistant message with tool calls
            current_messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function", 
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls
                ]
            })
            
            # Add tool responses
            for tool_call in message.tool_calls:
                if tool_call.function.name in ["get_current_time", "utility-server_getCurrentTime"]:
                    mock_response = "2024-08-14T13:00:00+05:30"  # Fixed IST time
                elif tool_call.function.name == "utility-server_generateTimestamp":
                    # Parse arguments to generate appropriate timestamp
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                    if args.get("date") == 13:  # Yesterday
                        mock_response = "2024-08-13T00:00:00+05:30"
                    elif args.get("date") == 7:  # A week ago
                        mock_response = "2024-08-07T00:00:00+05:30"
                    else:
                        mock_response = "2024-08-13T00:00:00+05:30"  # Default to yesterday
                elif mcp_client:
                    # Execute via MCP server
                    try:
                        arguments = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        mock_response = await mcp_client.execute_tool(tool_call.function.name, arguments)
                    except Exception as e:
                        mock_response = f"MCP tool execution failed: {e}"
                else:
                    mock_response = f"Mock data retrieved for {tool_call.function.name}."
                
                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": mock_response
                })
            
            # Continue multi-turn conversation until no more tool calls or max turns
            max_turns = 5  # Prevent infinite loops
            turn_count = 1
            
            while turn_count < max_turns:
                try:
                    response_next = await client.chat.completions.create(
                        model=config.AZURE_OPENAI_MODEL,
                        messages=current_messages,
                        tools=tools,
                        tool_choice="auto",
                        temperature=0.1,
                        max_tokens=1000
                    )
                    
                    next_message = response_next.choices[0].message
                    
                    # Extract function calls from this turn
                    if next_message.tool_calls:
                        # Add assistant message with tool calls
                        current_messages.append({
                            "role": "assistant",
                            "content": next_message.content,
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function", 
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                                } for tc in next_message.tool_calls
                            ]
                        })
                        
                        # Add tool responses
                        for tool_call in next_message.tool_calls:
                            all_function_calls.append({
                                'name': tool_call.function.name,
                                'arguments': json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                            })
                            
                            # Generate appropriate response
                            if tool_call.function.name in ["get_current_time", "utility-server_getCurrentTime"]:
                                mock_response = "2024-08-14T13:00:00+05:30"
                            elif tool_call.function.name == "utility-server_generateTimestamp":
                                # Parse arguments to generate appropriate timestamp
                                args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                                if args.get("date") == 13:  # Yesterday
                                    mock_response = "2024-08-13T00:00:00+05:30"
                                elif args.get("date") == 7:  # A week ago
                                    mock_response = "2024-08-07T00:00:00+05:30"
                                else:
                                    mock_response = "2024-08-13T00:00:00+05:30"  # Default to yesterday
                            elif mcp_client:
                                try:
                                    arguments = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                                    mock_response = await mcp_client.execute_tool(tool_call.function.name, arguments)
                                except Exception as e:
                                    mock_response = f"MCP tool execution failed: {e}"
                            else:
                                mock_response = f"Mock data retrieved for {tool_call.function.name}."
                            
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.function.name,
                                "content": mock_response
                            })
                        
                        turn_count += 1
                    else:
                        # No more tool calls, conversation is complete
                        break
                        
                except Exception as e:
                    print(f"    ⚠️  Turn {turn_count + 1} failed: {e}")
                    break
        
        return {
            'function_calls': all_function_calls,
            'response_text': message.content,
            'success': True
        }
        
    except Exception as e:
        return {
            'function_calls': [],
            'error': str(e),
            'success': False
        }

async def analyze_all_questions():
    """Analyze all questions using direct LLM calls"""
    
    print("🔍 Setting up direct LLM analysis...")
    
    # Check if MCP is enabled and initialize accordingly
    mcp_client = None
    openai_tools = []
    tool_names = []
    
    if getattr(config, 'AUTOMATIC_MCP_TOOL_SERVER_USAGE', False):
        print("🔗 MCP is enabled - using dynamic tools from MCP server")
        
        # Create MCP context (similar to voice agent)
        mcp_context = {
            "sessionId": "test_session",
            "juspayToken": None,  # Could be provided as parameter
            "shopUrl": None,
            "shopId": None, 
            "shopType": None,
            "userId": "test_user",
            "enableDemoMode": True,  # Always demo mode for testing
            "merchantId": "test_merchant",
            "platformIntegrations": []
        }
        
        # Initialize MCP client
        mcp_client = SimpleMCPClient(
            server_url=config.AUTOMATIC_TOOL_MCP_SERVER_URL,
            auth_token=None,  # Could be provided as parameter
            context=mcp_context
        )
        
        # Fetch tools from MCP server
        openai_tools = await mcp_client.fetch_tools()
        tool_names = [tool["function"]["name"] for tool in openai_tools]
        
        if not openai_tools:
            print("❌ No tools retrieved from MCP server")
            return {}, []
            
        print(f"✅ Loaded {len(openai_tools)} tools from MCP server")
        
    else:
        print("🔧 MCP disabled - using static tools")
        
        # Initialize static tools
        tools_schema, tool_functions = initialize_tools(
            mode=Mode.TEST.value,
            merchant_id="test_merchant"
        )
        
        # Extract tools in OpenAI format
        tools = tools_schema.standard_tools
        print(f"📋 Loaded {len(tools)} static tools")
        
        # Print available tools (handle different tool formats)
        for tool in tools:
            if hasattr(tool, 'name'):
                tool_names.append(tool.name)
            elif hasattr(tool, 'function') and hasattr(tool.function, 'name'):
                tool_names.append(tool.function.name)
            elif isinstance(tool, dict):
                tool_names.append(tool.get("function", {}).get("name", "unknown"))
            else:
                tool_names.append(str(type(tool)))
        
        print(f"🔧 Available static tools: {tool_names}")
        
        # Convert tools to proper OpenAI format
        for tool in tools:
            try:
                if hasattr(tool, 'model_dump'):  # Pydantic model
                    tool_dict = tool.model_dump()
                elif hasattr(tool, 'dict'):  # Pydantic model (older version)
                    tool_dict = tool.dict()
                elif isinstance(tool, dict):
                    tool_dict = tool
                else:
                    # Handle FunctionSchema objects
                    tool_dict = {
                        "type": "function",
                        "function": {
                            "name": tool.name if hasattr(tool, 'name') else str(tool),
                            "description": tool.description if hasattr(tool, 'description') else "",
                            "parameters": tool.parameters.model_dump() if hasattr(tool, 'parameters') and hasattr(tool.parameters, 'model_dump') else (
                                tool.parameters.dict() if hasattr(tool, 'parameters') and hasattr(tool.parameters, 'dict') else (
                                    tool.parameters if hasattr(tool, 'parameters') else {}
                                )
                            )
                        }
                    }
                
                openai_tools.append(tool_dict)
                
            except Exception as e:
                print(f"⚠️  Error converting tool {type(tool)}: {e}")
                # Try to extract basic info
                try:
                    tool_dict = {
                        "type": "function", 
                        "function": {
                            "name": getattr(tool, 'name', 'unknown_tool'),
                            "description": getattr(tool, 'description', 'No description'),
                            "parameters": getattr(tool, 'parameters', {})
                        }
                    }
                    openai_tools.append(tool_dict)
                except:
                    print(f"❌ Failed to convert tool completely: {tool}")
        
        print(f"🔄 Converted {len(openai_tools)} static tools to OpenAI format")
        
        # Print first few tools to verify conversion
        if openai_tools:
            print(f"✅ Sample converted tool: {openai_tools[0]['function']['name']}")
            print(f"   Description: {openai_tools[0]['function']['description'][:100]}...")
        else:
            print("❌ No tools converted! This will cause hallucinated tool calls.")
    
    # Print final tool summary  
    print(f"🔧 Final available tools: {tool_names}")
    
    # Create Azure OpenAI client
    client = AsyncAzureOpenAI(
        api_key=config.AZURE_OPENAI_API_KEY,
        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
        api_version="2024-09-01-preview"
    )
    
    # Get system prompt
    system_prompt = get_system_prompt(user_name="test_user", tts_provider=TTSProvider.GOOGLE)
    
    # Analyze each question
    results = {}
    
    print(f"📝 Analyzing {len(DEFAULT_TEST_QUESTIONS)} questions...")
    
    for i, question in enumerate(DEFAULT_TEST_QUESTIONS[:5]):
        print(f"  {i+1:3d}/172: {question[:5]}...")
        
        # Create messages
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question}
        ]
        
        # Call LLM (with MCP client if available)
        result = await call_llm_with_tools(client, messages, openai_tools, mcp_client)
        
        # Get question metadata (category and queryType)
        metadata = get_question_metadata(question)
        
        # Store results with metadata
        results[question] = {
            'question_index': i + 1,
            'success': result['success'],
            'function_calls': result['function_calls'],
            'num_tools_called': len(result['function_calls']),
            'tools_called': [fc['name'] for fc in result['function_calls']],
            'response_text': result.get('response_text', ''),
            'error': result.get('error', None),
            'queryType': metadata['queryType'] if metadata else 'unknown',
            'category': metadata['category'] if metadata else 'unknown'
        }
        
        # Show results
        if result['success'] and result['function_calls']:
            called_tools = [fc['name'] for fc in result['function_calls']]
            print(f"    ✅ {len(result['function_calls'])} tools called: {called_tools}")
        elif result['success']:
            print(f"    ⚪ No tools called")
        else:
            print(f"    ❌ Error: {result.get('error', 'Unknown error')}")
    
    # Cleanup MCP client if used
    if mcp_client:
        await mcp_client.close()
        print("🔌 MCP client closed")
    
    return results, tool_names

def analyze_results(results: Dict[str, Any]):
    """Analyze the tool call results with category and queryType insights"""
    
    print("\n📊 Tool Usage Analysis:")
    print("=" * 60)
    
    # Count tool usage
    tool_counts = {}
    questions_with_tools = 0
    questions_without_tools = 0
    questions_with_errors = 0
    
    # Category and queryType analysis
    category_stats = {}
    querytype_stats = {}
    
    for question, data in results.items():
        category = data.get('category', 'unknown')
        querytype = data.get('queryType', 'unknown')
        
        # Initialize category stats
        if category not in category_stats:
            category_stats[category] = {'total': 0, 'with_tools': 0, 'without_tools': 0, 'errors': 0}
        
        # Initialize queryType stats  
        if querytype not in querytype_stats:
            querytype_stats[querytype] = {'total': 0, 'with_tools': 0, 'without_tools': 0, 'errors': 0}
        
        category_stats[category]['total'] += 1
        querytype_stats[querytype]['total'] += 1
        
        if not data['success']:
            questions_with_errors += 1
            category_stats[category]['errors'] += 1
            querytype_stats[querytype]['errors'] += 1
        elif data['num_tools_called'] > 0:
            questions_with_tools += 1
            category_stats[category]['with_tools'] += 1
            querytype_stats[querytype]['with_tools'] += 1
            for tool_name in data['tools_called']:
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        else:
            questions_without_tools += 1
            category_stats[category]['without_tools'] += 1
            querytype_stats[querytype]['without_tools'] += 1
    
    print(f"Total questions: {len(results)}")
    print(f"Questions with tool calls: {questions_with_tools}")
    print(f"Questions without tool calls: {questions_without_tools}")
    print(f"Questions with errors: {questions_with_errors}")
    
    # Category analysis
    print(f"\n🏷️  Analysis by CATEGORY:")
    for category, stats in sorted(category_stats.items()):
        tool_rate = (stats['with_tools'] / stats['total']) * 100 if stats['total'] > 0 else 0
        print(f"  {category:10} {stats['total']:3d} total | {stats['with_tools']:3d} with tools ({tool_rate:4.1f}%) | {stats['without_tools']:3d} no tools")
    
    # QueryType analysis
    print(f"\n📝 Analysis by QUERY TYPE:")
    for querytype, stats in sorted(querytype_stats.items()):
        tool_rate = (stats['with_tools'] / stats['total']) * 100 if stats['total'] > 0 else 0
        print(f"  {querytype:10} {stats['total']:3d} total | {stats['with_tools']:3d} with tools ({tool_rate:4.1f}%) | {stats['without_tools']:3d} no tools")
    
    if tool_counts:
        print(f"\n🔧 Tool call frequency:")
        for tool_name, count in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / len(results)) * 100
            print(f"  {tool_name:40} {count:3d} calls ({percentage:5.1f}%)")
        
        # Show examples for top tools
        print(f"\n📋 Example questions by most used tools:")
        top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        for tool_name, count in top_tools:
            examples = []
            for question, data in results.items():
                if tool_name in data['tools_called']:
                    examples.append(f"[{data['category']}/{data['queryType']}] {question}")
                    if len(examples) >= 2:
                        break
            
            print(f"\n  {tool_name} ({count} calls):")
            for example in examples:
                print(f"    • {example[:100]}...")

def save_results(results: Dict[str, Any], available_tools: List[str] = None, filename: str = "direct_llm_tool_analysis.json"):
    """Save results to JSON file with metadata"""
    
    # Create enhanced output with metadata
    output_data = {
        "metadata": {
            "analysis_timestamp": json.loads(json.dumps(datetime.now().isoformat())),
            "total_questions_analyzed": len(results),
            "total_available_tools": len(available_tools) if available_tools else 0,
            "available_tools": available_tools or [],
            "mcp_enabled": getattr(config, 'AUTOMATIC_MCP_TOOL_SERVER_USAGE', False),
            "analysis_method": "direct_llm_calls_with_time_mocking"
        },
        "questions": results
    }
    
    with open(filename, 'w') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Results saved to {filename}")

async def main():
    try:
        results, available_tools = await analyze_all_questions()
        analyze_results(results)
        save_results(results, available_tools)
        
        # Mark task as complete
        print("\n✅ Analysis completed successfully!")
        
    except Exception as e:
        print(f"❌ Error in analysis: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())