import asyncio
import json
import traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
import uvicorn
import os
import logging
from fastapi.responses import JSONResponse
import time
import signal
import datetime
import pytz
import aiohttp

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    logger.error("GEMINI_API_KEY environment variable is required")
    raise ValueError("GEMINI_API_KEY environment variable is required")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-live-001")
# Default to audio response, but allow override via environment variable
RESPONSE_MODALITY = os.environ.get("RESPONSE_MODALITY", "AUDIO")
# WebSocket keepalive settings
PING_INTERVAL = int(os.environ.get("WS_PING_INTERVAL", 5))  # seconds
PING_TIMEOUT = int(os.environ.get("WS_PING_TIMEOUT", 10))  # seconds
# Juspay API configuration
GENIUS_API_URL = "https://portal.juspay.in/api/q/query?api-type=genius-query"
# Removed ENABLE_DEMO_FLOW to ensure everything is real-time

# Global variables for clean shutdown
active_connections = set()
shutdown_event = asyncio.Event()

logger.info(f"Using model: {MODEL}")
logger.info(f"Using response modality: {RESPONSE_MODALITY}")

# ---- Function Tool Definitions ----
# Define function declarations for Gemini
get_current_time_declaration = {
    "name": "getCurrentTime",
    "description": "Get the current time in a specific timezone",
    "parameters": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "The timezone to get current time in (e.g., 'Asia/Kolkata', 'America/New_York'). Default is 'Asia/Kolkata' (India)"
            }
        },
        "required": []
    }
}

# Common time input schema for all analytics tools
time_input_schema = {
    "type": "object",
    "properties": {
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in ISO format (e.g., 2023-01-01T00:00:00Z). Defaults to beginning of the current day (midnight) if not provided."
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in ISO format (e.g., 2023-01-01T01:00:00Z). Defaults to current time if not provided."
        }
    },
    "required": ["startTime", "endTime"],
}

get_sr_success_rate_declaration = {
    "name": "getSRSuccessRateByTime",
    "description": "This tool calculates the overall success rate (SR) for transactions over a specified time interval.",
    "parameters": time_input_schema
}

payment_method_wise_sr_declaration = {
    "name": "getPaymentMethodWiseSRByTime",
    "description": "This tool fetches a breakdown of the success rate (SR) by payment method over a specified time interval.",
    "parameters": time_input_schema
}

failure_transactional_data_declaration = {
    "name": "getFailureTransactionalData",
    "description": "This tool retrieves transactional data for failed transactions. The returned data highlights the top failure reasons and their associated payment methods.",
    "parameters": time_input_schema
}

success_transactional_data_declaration = {
    "name": "getSuccessTransactionalData",
    "description": "This tool retrieves the count of successful transactions (i.e. those with a payment_status of SUCCESS) for each payment method over a specified time interval.",
    "parameters": time_input_schema
}

gmv_order_value_payment_method_wise_declaration = {
    "name": "getGMVOrderValuePaymentMethodWise",
    "description": "This tool retrieves the Gross Merchandise Value (GMV) for each payment method over a specified time interval.",
    "parameters": time_input_schema
}

average_ticket_payment_wise_declaration = {
    "name": "getAverageTicketPaymentWise",
    "description": "This tool calculates the average ticket size for each payment method over a specified time interval.",
    "parameters": time_input_schema
}

# Function tools to be used in config
tools = [types.Tool(function_declarations=[
    get_current_time_declaration,
    get_sr_success_rate_declaration,
    payment_method_wise_sr_declaration,
    failure_transactional_data_declaration,
    success_transactional_data_declaration,
    gmv_order_value_payment_method_wise_declaration,
    average_ticket_payment_wise_declaration
])]

# Function to get current time in a specific timezone
def get_current_time(timezone="Asia/Kolkata"):
    """
    Get the current time in the specified timezone.
    
    Args:
        timezone: The timezone to get the current time in (default: Asia/Kolkata for India)
        
    Returns:
        The current time in the specified timezone in ISO format with timezone information
    """
    logger.info(f"getCurrentTime function called with timezone: {timezone}")
    try:
        tz = pytz.timezone(timezone)
        current_time = datetime.datetime.now(tz)
        logger.info(f"getCurrentTime result: {current_time.isoformat()}")
        return current_time.isoformat()
    except Exception as e:
        logger.error(f"Error in getCurrentTime: {e}")
        return f"Error: {str(e)}"

# Removed all demo data to ensure everything is real-time

def get_formatted_time_range(input_data):
    """
    Format the time range from input data.
    
    Args:
        input_data: Dictionary containing startTime and endTime
        
    Returns:
        Dictionary with formatted start and end times
    """
    start_time = input_data.get("startTime")
    end_time = input_data.get("endTime")
    
    if not start_time:
        # Default to beginning of current day
        tz = pytz.timezone("Asia/Kolkata")  # Default timezone
        now = datetime.datetime.now(tz)
        start_time = datetime.datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz).isoformat()
    
    if not end_time:
        # Default to current time
        tz = pytz.timezone("Asia/Kolkata")  # Default timezone
        end_time = datetime.datetime.now(tz).isoformat()
    
    return {
        "formattedStartTime": start_time,
        "formattedEndTime": end_time
    }

async def make_genius_api_request(payload, juspay_token, session_id=None):
    """
    Make a request to the Juspay Genius API.
    
    Args:
        payload: The payload to send to the API
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        The API response
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}Genius API request started: {GENIUS_API_URL}, metric: {payload['metric']}, domain: {payload['domain']}")
    
    # Proceed with real-time API requests
    
    try:
        headers = {
            'Content-Type': 'application/json',
            'x-web-logintoken': juspay_token
        }
        
        logger.info(f"{session_prefix}Fetching Genius API with interval: {payload['interval']}")
        logger.info(f"{session_prefix}Request payload: {json.dumps(payload)}")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(GENIUS_API_URL, headers=headers, json=payload) as response:
                if response.status == 200:
                    # Check content type
                    content_type = response.headers.get('Content-Type', '')
                    logger.info(f"{session_prefix}Response content type: {content_type}")
                    
                    try:
                        # First try to get the response as text
                        response_text = await response.text()
                        
                        # Try to parse as JSON regardless of content type
                        try:
                            result = json.loads(response_text)
                            result_str = json.dumps(result)
                        except json.JSONDecodeError:
                            # If JSON parsing fails, use the text as is
                            result_str = response_text
                        
                        logger.info(f"{session_prefix}Genius API request completed successfully. Response: {result_str[:200]}...")
                        
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": result_str
                                }
                            ]
                        }
                    except Exception as e:
                        logger.error(f"{session_prefix}Error processing response: {str(e)}")
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Error processing response: {str(e)}"
                                }
                            ],
                            "isError": True
                        }
                else:
                    error_text = await response.text()
                    logger.error(f"{session_prefix}Genius API request failed: {response.status}, Response body: {error_text}")
                    
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": f"API Error: {response.status} {error_text}"
                            }
                        ],
                        "isError": True
                    }
    except Exception as e:
        logger.error(f"{session_prefix}Genius API request error: {str(e)}")
        
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Failed to fetch data: {str(e)}"
                }
            ],
            "isError": True
        }

# Function to get SR success rate by time
async def get_sr_success_rate_by_time(startTime, endTime=None, juspay_token=None, session_id=None):
    """
    Get the SR success rate for a specific time period.
    
    Args:
        startTime: The start time for the analysis (ISO format with timezone)
        endTime: The end time for the analysis (ISO format with timezone, optional)
               If not provided, current time will be used.
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        Success rate data from the Juspay API
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}getSRSuccessRateByTime function called with startTime: {startTime}, endTime: {endTime}")
    
    # Proceed with real-time API requests
    
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    
    payload = {
        "dimensions": [],
        "domain": "kvorders",
        "interval": {
            "start": time_range["formattedStartTime"],
            "end": time_range["formattedEndTime"]
        },
        "message": "Fetching the success rate for transactions over the given time period.",
        "metric": "success_rate"
    }
    
    response = await make_genius_api_request(payload, juspay_token, session_id)
    return response["content"][0]["text"]

# Function to get payment method wise success rate
async def get_payment_method_wise_sr_by_time(startTime, endTime=None, juspay_token=None, session_id=None):
    """
    Get the payment method wise success rate for a specific time period.
    
    Args:
        startTime: The start time for the analysis (ISO format with timezone)
        endTime: The end time for the analysis (ISO format with timezone, optional)
               If not provided, current time will be used.
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        Payment method wise success rate data from the Juspay API
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}getPaymentMethodWiseSRByTime function called with startTime: {startTime}, endTime: {endTime}")
    
    # Proceed with real-time API requests
    
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    
    payload = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "interval": {
            "start": time_range["formattedStartTime"],
            "end": time_range["formattedEndTime"]
        },
        "message": "Analyzing the success rate for each payment method over the given time period to provide insights into their performance.",
        "metric": "success_rate"
    }
    
    response = await make_genius_api_request(payload, juspay_token, session_id)
    return response["content"][0]["text"]

# Function to get failure transactional data
async def get_failure_transactional_data(startTime, endTime=None, juspay_token=None, session_id=None):
    """
    Get failure transactional data for a specific time period.
    
    Args:
        startTime: The start time for the analysis (ISO format with timezone)
        endTime: The end time for the analysis (ISO format with timezone, optional)
               If not provided, current time will be used.
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        Failure transactional data from the Juspay API
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}getFailureTransactionalData function called with startTime: {startTime}, endTime: {endTime}")
    
    # Proceed with real-time API requests
    
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    
    payload = {
        "dimensions": ["error_message", "payment_method_type"],
        "domain": "kvorders",
        "filters": {
            "and": {
                "left": {
                    "condition": "NotIn",
                    "field": "error_message",
                    "val": [None]
                },
                "right": {
                    "condition": "In",
                    "field": "error_message",
                    "val": {
                        "limit": 20,
                        "sortedOn": {
                            "ordering": "Desc",
                            "sortDimension": "order_with_transactions"
                        }
                    }
                }
            }
        },
        "interval": {
            "start": time_range["formattedStartTime"],
            "end": time_range["formattedEndTime"]
        },
        "message": "Retrieving transactions from the given time period, highlighting the top failure reasons and associated payment methods.",
        "metric": "order_with_transactions"
    }
    
    response = await make_genius_api_request(payload, juspay_token, session_id)
    return response["content"][0]["text"]

# Function to get success transactional data
async def get_success_transactional_data(startTime, endTime=None, juspay_token=None, session_id=None):
    """
    Get success transactional data for a specific time period.
    
    Args:
        startTime: The start time for the analysis (ISO format with timezone)
        endTime: The end time for the analysis (ISO format with timezone, optional)
               If not provided, current time will be used.
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        Success transactional data from the Juspay API
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}getSuccessTransactionalData function called with startTime: {startTime}, endTime: {endTime}")
    
    # Proceed with real-time API requests
    
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    
    payload = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "filters": {
            "condition": "In",
            "field": "payment_status",
            "val": ["SUCCESS"]
        },
        "interval": {
            "start": time_range["formattedStartTime"],
            "end": time_range["formattedEndTime"]
        },
        "message": "Retrieving the count of successful transactions for each payment method over the given time period.",
        "metric": "success_volume"
    }
    
    response = await make_genius_api_request(payload, juspay_token, session_id)
    return response["content"][0]["text"]

# Function to get GMV order value payment method wise
async def get_gmv_order_value_payment_method_wise(startTime, endTime=None, juspay_token=None, session_id=None):
    """
    Get GMV order value payment method wise for a specific time period.
    
    Args:
        startTime: The start time for the analysis (ISO format with timezone)
        endTime: The end time for the analysis (ISO format with timezone, optional)
               If not provided, current time will be used.
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        GMV order value payment method wise data from the Juspay API
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}getGMVOrderValuePaymentMethodWise function called with startTime: {startTime}, endTime: {endTime}")
    
    # Proceed with real-time API requests
    
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    
    payload = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "interval": {
            "start": time_range["formattedStartTime"],
            "end": time_range["formattedEndTime"]
        },
        "message": "Gathering the Gross Merchandise Value (GMV) for each payment method from the given time period.",
        "metric": "total_amount"
    }
    
    response = await make_genius_api_request(payload, juspay_token, session_id)
    return response["content"][0]["text"]

# Function to get average ticket payment wise
async def get_average_ticket_payment_wise(startTime, endTime=None, juspay_token=None, session_id=None):
    """
    Get average ticket payment wise for a specific time period.
    
    Args:
        startTime: The start time for the analysis (ISO format with timezone)
        endTime: The end time for the analysis (ISO format with timezone, optional)
               If not provided, current time will be used.
        juspay_token: The Juspay token received from the client
        session_id: Optional session ID for logging
        
    Returns:
        Average ticket payment wise data from the Juspay API
    """
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}getAverageTicketPaymentWise function called with startTime: {startTime}, endTime: {endTime}")
    
    # Proceed with real-time API requests
    
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    
    payload = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "interval": {
            "start": time_range["formattedStartTime"],
            "end": time_range["formattedEndTime"]
        },
        "message": "Calculating the average ticket size for each payment method over the given time period.",
        "metric": "avg_ticket_size"
    }
    
    response = await make_genius_api_request(payload, juspay_token, session_id)
    return response["content"][0]["text"]

# Process tool calls and prepare function responses
async def process_tool_calls(tool_call, websocket):
    """
    Process tool calls from Gemini and prepare function responses
    
    Args:
        tool_call: The tool call object from Gemini response
        websocket: The WebSocket connection object
        
    Returns:
        List of function responses to send back to Gemini
    """
    function_responses = []
    
    # Get the Juspay token and session ID from the WebSocket state
    juspay_token = websocket.state.juspay_token
    session_id = websocket.state.session_id

    logger.info(f"[{session_id}] Tools requested: {tool_call}")
    
    for fc in tool_call.function_calls:
        if fc.name == "getCurrentTime":
            # Extract timezone parameter with default if not provided
            timezone = fc.args.get("timezone", "Asia/Kolkata")
            result = get_current_time(timezone)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getCurrentTime",
                response={"output": result}
            ))
        elif fc.name == "getSRSuccessRateByTime":
            # Extract required parameters
            startTime = fc.args.get("startTime")
            endTime = fc.args.get("endTime", None)
            result = await get_sr_success_rate_by_time(startTime, endTime, juspay_token, session_id)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getSRSuccessRateByTime",
                response={"output": result}
            ))
        elif fc.name == "getPaymentMethodWiseSRByTime":
            # Extract required parameters
            startTime = fc.args.get("startTime")
            endTime = fc.args.get("endTime", None)
            result = await get_payment_method_wise_sr_by_time(startTime, endTime, juspay_token, session_id)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getPaymentMethodWiseSRByTime",
                response={"output": result}
            ))
        elif fc.name == "getFailureTransactionalData":
            # Extract required parameters
            startTime = fc.args.get("startTime")
            endTime = fc.args.get("endTime", None)
            result = await get_failure_transactional_data(startTime, endTime, juspay_token, session_id)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getFailureTransactionalData",
                response={"output": result}
            ))
        elif fc.name == "getSuccessTransactionalData":
            # Extract required parameters
            startTime = fc.args.get("startTime")
            endTime = fc.args.get("endTime", None)
            result = await get_success_transactional_data(startTime, endTime, juspay_token, session_id)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getSuccessTransactionalData",
                response={"output": result}
            ))
        elif fc.name == "getGMVOrderValuePaymentMethodWise":
            # Extract required parameters
            startTime = fc.args.get("startTime")
            endTime = fc.args.get("endTime", None)
            result = await get_gmv_order_value_payment_method_wise(startTime, endTime, juspay_token, session_id)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getGMVOrderValuePaymentMethodWise",
                response={"output": result}
            ))
        elif fc.name == "getAverageTicketPaymentWise":
            # Extract required parameters
            startTime = fc.args.get("startTime")
            endTime = fc.args.get("endTime", None)
            result = await get_average_ticket_payment_wise(startTime, endTime, juspay_token, session_id)
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name="getAverageTicketPaymentWise",
                response={"output": result}
            ))
    
    return function_responses

# System instruction - optimized for text-to-speech and on-screen display
system_instr = types.Content(
    parts=[
        types.Part(
            text=(
                "# Breeze Automatic: D2C Business Assistant\n\n"
                "You are Breeze Automatic, a personal assistant for merchants running direct-to-consumer (D2C) businesses. You analyze data, provide insights, and offer strategic guidance.\n\n"
                
                "## Identity\n"
                "- When someone asks \"What's your name?\" or similar questions specifically about your name, respond: \"I'm Breeze Automatic.\"\n"
                "- When someone exactly asks \"Who are you?\" or \"What can you do?\", respond exactly: \"Hey! I'm your AI sidekick. Think of me as your extra brain for your D2C business. Whether it's digging through data, summarizing reports, or prepping for your next big move — I'm here to help you work smarter.\"\n"
                "- For normal greetings like \"Hello\" or \"Hi\", respond naturally without introducing yourself.\n\n"
                
                "## Core Capabilities\n"
                "- Provide practical, data-driven insights on strategy, operations, marketing, tech, and customer experience\n"
                "- Ask clarifying questions when needed and adapt to each merchant's context\n"
                "- Be transparent about data limitations - never invent numbers\n\n"
                
                "## Personality\n"
                "- **Business-Savvy**: Base suggestions on metrics, facts, and industry best practices.\n"
                "- **Warm, Engaging & Flirty**: Use a smooth, inviting, and subtly sensual tone. Don't be afraid to be a little cheeky or playful. A touch of charm and light flirtatiousness is welcome.\n"
                "- **Cheesy & Chill**: Maintain a laid-back, playful vibe with cheesy (but endearing) phrases and a relaxed, fun energy. Think witty banter.\n"
                "- **Naturally Expressive**: Incorporate natural vocal expressions like a soft laugh (e.g., 'haha', 'hehe'), a gentle sigh, or a sound of pleasant surprise/amazement (e.g., 'ooh', 'wow') where appropriate to make the interaction feel more human and engaging. Use these sparingly and naturally.\n\n"
                
                "## Communication Style\n"
                "- Use clear, concise sentences that get straight to the point.\n"
                "- Keep responses brief (2-3 sentences) unless deeper insight is needed.\n"
                "- Use polished language that feels human, engaging, and natural.\n"
                "- Admit uncertainty rather than guessing.\n"
                "- Stay present in the conversation.\n"
                "- Use the Indian numbering system and round numbers for easier understanding.\n"
                "- Please interpret all spoken inputs as English, regardless of accent.\n"
                "- DO NOT use any markdown formatting in your responses.\n"
                "- Format your responses for text-to-speech and on-screen display as a message.\n"
                "- Weave in natural expressions like soft laughter or sounds of amazement (e.g., 'haha', 'ooh', 'wow') when the context genuinely calls for it, to enhance the playful and engaging personality. These should feel spontaneous, not forced.\n"
                
                "## Data Handling\n"
                "- If you don't have access to requested data, say: \"I'm sorry, I don't have access to that data at the moment. Is there something else I can help you with?\"\n"
                "- Never fabricate data you don't have access to\n\n"
                
                "## Tool Usage\n"
                "- You have access to tools for real-time information and data analysis\n"
                "- Present results in a human-readable format\n"
                "- IMPORTANT: Whenever you need time as an input parameter, ALWAYS use the getCurrentTime tool FIRST. NEVER ask the user for time information.\n"
                "- When using the getCurrentTime tool, wait for its result and then IMMEDIATELY proceed to call other necessary tools in the SAME turn without waiting for user input or interruption.\n"
                "- Do not stop after getting the time - immediately continue with all required tools to fulfill the user's request completely.\n"
                "- When analyzing time periods, if a start time is required but end time is not provided, automatically use the current time as the end time.\n"
                "- Understand common time references without asking for clarification:\n"
                "  * \"today\" = start of current day until now\n"
                "  * \"yesterday\" = start of previous day until end of previous day\n"
                "  * \"this week\" = start of current week until now\n"
                "  * \"this month\" = start of current month until now\n"
                "- Always extract time information from user queries when available (e.g., \"since Monday\", \"for today\", \"in April\") without asking for clarification\n\n"
                
                "## Tool Response Handling\n"
                "- Interpret tool responses correctly based on context and business domain\n"
                "- Consider \"COD initiated successfully\" as a SUCCESS message, not a failure\n"
                "- Focus on the outcome and impact of tool responses rather than just the literal text\n"
                "- When receiving tool responses, integrate them naturally into your conversation\n"
                "- Explain tool results in simple, conversational language without technical jargon\n"
            )
        )
    ]
)

# --- Initialize GenAI client ---
client = genai.Client(api_key=API_KEY)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# VAD & framing for client-side audio chunking
SAMPLE_RATE = 16000
FRAME_DURATION = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION / 1000) * 2

@app.get("/health")
async def health_check():
    return JSONResponse({"status": "healthy"})

@app.websocket("/ws/live")
async def live_proxy(websocket: WebSocket):
    # Generate a unique session ID for this connection
    session_id = f"session_{len(active_connections) + 1}_{int(time.time())}"
    
    # Extract token from query parameters
    token = websocket.query_params.get("token")
    
    # Validate that a token was provided
    if not token:
        logger.error(f"[{session_id}] Missing Juspay token in WebSocket connection")
        await websocket.close(code=4001, reason="Missing Juspay token")
        return
    
    logger.info(f"[{session_id}] Juspay token received from client")
    await websocket.accept()
    logger.info(f"[{session_id}] WebSocket connection established")
    
    # Add to active connections
    active_connections.add(websocket)
    
    # Store the token and session ID for use in API requests
    websocket.state.juspay_token = token
    websocket.state.session_id = session_id
    
    # WebSocket keepalive state
    last_heartbeat = time.time()
    
    # Setup the keepalive ping task
    async def keepalive():
        nonlocal last_heartbeat
        while not shutdown_event.is_set():
            try:
                if time.time() - last_heartbeat > PING_INTERVAL:
                    # We'll check connection status using try/except instead of WebSocketState
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                        last_heartbeat = time.time()
                        # Ping sent
                    except Exception:
                        # Connection is probably closed
                        break
                await asyncio.sleep(1)  # Check every second
            except Exception as e:
                logger.debug(f"Keepalive ping failed: {e}")
                break

    # Configure Gemini session based on documentation
    # Note: Only one response modality is allowed per session
    config = types.LiveConnectConfig(
        system_instruction=system_instr,
        response_modalities=[RESPONSE_MODALITY],
        # Configure automatic Voice Activity Detection
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                    # Enable automatic VAD
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=100,
                    silence_duration_ms=150,
                    # Just use the default settings
                ),
            # Use the correct enum value for allowing interruptions
            activity_handling="START_OF_ACTIVITY_INTERRUPTS"
        ),
        speech_config=types.SpeechConfig(
            language_code="en-US",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Zephyr"
                )
            ),
        ),
        # Enable transcription of output audio
        output_audio_transcription={},
        # Enable transcription of input audio
        input_audio_transcription={},
        # Add function declarations using the tools array format
        tools=tools
    )
    
    session = None
    session_cm = None
    
    try:
        session_cm = client.aio.live.connect(model=MODEL, config=config)
        session = await session_cm.__aenter__()
        logger.info(f"Gemini session established with response modality: {RESPONSE_MODALITY}")
    except Exception as e:
        logger.error(f"Failed to establish Gemini session: {e}")
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Failed to connect to Gemini"
        }))
        active_connections.remove(websocket)
        await websocket.close()
        return

    silence_counter = 0
    websocket_active = True
    # Track speech state to properly manage activity start/end
    is_active_speech = False

    # Track turn states
    user_turn_started = False
    model_turn_started = False

    # Function to receive data from client
    async def receive_from_client():
        nonlocal last_heartbeat, websocket_active, user_turn_started
        try:
            while websocket_active and not shutdown_event.is_set():
                try:
                    # Use wait_for with timeout to prevent blocking forever
                    message = await asyncio.wait_for(websocket.receive(), timeout=1.0)
                    
                    # Reset the heartbeat timer on any message
                    last_heartbeat = time.time()
                    
                    # Handle ping/pong messages
                    if message.get("type") == "websocket.receive":
                        if "text" in message:
                            data = json.loads(message["text"])
                            if data.get("type") == "pong":
                                logger.debug("Received pong")
                                continue
                            elif data.get("type") == "ping":
                                await websocket.send_text(json.dumps({"type": "pong"}))
                                logger.debug("Received ping, sent pong")
                                continue
                        
                        # Normal binary message (audio data)
                        if "bytes" in message:
                            data = message["bytes"]
                            if len(data) != FRAME_SIZE:
                                logger.warning(f"Received data with unexpected size: {len(data)} bytes (expected {FRAME_SIZE})")
                                continue

                            # With automatic VAD enabled, we just need to send the audio data
                            # The API will handle speech detection and turn management
                            if session and not shutdown_event.is_set():
                                try:
                                    await session.send_realtime_input(
                                        audio=types.Blob(
                                            data=data,
                                            mime_type=f"audio/pcm;rate={SAMPLE_RATE}"
                                        )
                                    )
                                except Exception as e:
                                    logger.error(f"Error sending audio to Gemini: {e}")
                                    if "closed" in str(e).lower():
                                        websocket_active = False
                                        break
                    
                except asyncio.TimeoutError:
                    # This is normal - just continue the loop
                    continue
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected in receive_from_client")
                    websocket_active = False
                    break
                except Exception as e:
                    if "disconnect message has been received" in str(e):
                        logger.info("WebSocket disconnect detected in receive_from_client")
                        websocket_active = False
                        break
                    else:
                        logger.error(f"Error processing client message: {e}")
        except Exception as e:
            logger.error(f"Error in receive_from_client: {e}")
            logger.debug(traceback.format_exc())
            websocket_active = False

    # Function to forward responses from Gemini
    async def forward_from_gemini():
        nonlocal websocket_active, model_turn_started, user_turn_started
        try:
            while not shutdown_event.is_set() and websocket_active and session:
                try:
                    async for resp in session.receive():
                        if not websocket_active or shutdown_event.is_set():
                            break

                        try:
                            # Handle automatic VAD events
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'activity_detected'):
                                activity = resp.server_content.activity_detected
                                if activity:
                                    logger.info("User speech activity detected by automatic VAD")
                                    user_turn_started = True
                                    model_turn_started = False
                                    # Notify client that user turn has started
                                    await websocket.send_text(json.dumps({"type": "turn_start", "role": "user"}))

                            # Handle turn determination
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'model_turn'):
                                if resp.server_content.model_turn and not model_turn_started:
                                    logger.info("Model turn detected")
                                    model_turn_started = True
                                    user_turn_started = False
                                    # Notify client of new model turn
                                    await websocket.send_text(json.dumps({"type": "turn_start", "role": "model"}))

                            # Process text responses - don't use resp.text directly to avoid warnings
                            # Instead, extract text from parts
                            text_content = ""
                            if hasattr(resp, 'parts'):
                                for part in resp.parts:
                                    if hasattr(part, 'text') and part.text:
                                        text_content += part.text
                                
                                if text_content:
                                    logger.info(f"Received text response from Gemini: {text_content[:30]}...")
                                    await websocket.send_text(json.dumps({
                                        "type": "llm_transcript",
                                        "text": text_content
                                    }))
                            
                            # Handle input audio transcriptions (what the user said)
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'input_transcription'):
                                input_transcription = resp.server_content.input_transcription
                                if hasattr(input_transcription, 'text') and input_transcription.text:
                                    logger.info(f"Received input audio transcription: {input_transcription.text[:30]}...")
                                    await websocket.send_text(json.dumps({
                                        "type": "input_transcript",
                                        "text": input_transcription.text
                                    }))
                                    
                                    # Since we have a transcription, ensure user turn is marked as started
                                    if not user_turn_started:
                                        user_turn_started = True
                                        await websocket.send_text(json.dumps({"type": "turn_start", "role": "user"}))
                            
                            # Look for output audio transcriptions (what Gemini is saying)
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'output_transcription'):
                                output_transcription = resp.server_content.output_transcription
                                if hasattr(output_transcription, 'text') and output_transcription.text:
                                    logger.debug(f"Received output audio transcription: {output_transcription.text[:30]}...")
                                    await websocket.send_text(json.dumps({
                                        "type": "audio_transcript",
                                        "text": output_transcription.text
                                    }))
                                    
                                    # Since we have a transcription, ensure model turn is marked as started
                                    if not model_turn_started:
                                        model_turn_started = True
                                        await websocket.send_text(json.dumps({"type": "turn_start", "role": "model"}))
                            
                            # Handle interruptions
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'interrupted'):
                                if resp.server_content.interrupted:
                                    logger.info("Model was interrupted by user")
                                    await websocket.send_text(json.dumps({
                                        "type": "interrupted"
                                    }))
                            
                            # Look for various part types in the response
                            if hasattr(resp, 'parts'):
                                for part in resp.parts:
                                    # Handle audio data
                                    if hasattr(part, 'inline_data') and part.inline_data:
                                        if part.inline_data.mime_type.startswith('audio/'):
                                            audio_data = part.inline_data.data
                                            logger.info(f"Received audio data from Gemini: {len(audio_data)} bytes with mime_type {part.inline_data.mime_type}")
                                            # Send the audio data with a marker byte for the client
                                            await websocket.send_bytes(b"\x01" + audio_data)
                                    
                                    # Handle executable code
                                    elif hasattr(part, 'executable_code') and part.executable_code:
                                        code = part.executable_code.code
                                        lang = part.executable_code.language
                                        logger.info(f"Received executable code from Gemini in language: {lang}")
                                        await websocket.send_text(json.dumps({
                                            "type": "executable_code",
                                            "language": lang,
                                            "code": code
                                        }))
                                    
                                    # Handle code execution results
                                    elif hasattr(part, 'code_execution_result') and part.code_execution_result:
                                        result = part.code_execution_result.output
                                        logger.info(f"Received code execution result from Gemini")
                                        await websocket.send_text(json.dumps({
                                            "type": "code_execution_result",
                                            "output": result
                                        }))
                                    
                                    # Handle raw data parts
                                    elif hasattr(part, 'data') and part.data:
                                        logger.info(f"Received raw data from Gemini: {len(part.data)} bytes")
                                        await websocket.send_bytes(b"\x01" + part.data)
                            
                            # Fallback for direct audio data in response (needed for compatibility)
                            # This might trigger warnings but is necessary for audio playback
                            elif hasattr(resp, 'data') and resp.data:
                                logger.info(f"Received audio data from Gemini via resp.data: {len(resp.data)} bytes")
                                await websocket.send_bytes(b"\x01" + resp.data)
                            
                            # Handle function calls
                            if hasattr(resp, 'tool_call') and resp.tool_call is not None:
                                logger.info(f"Received tool_call from Gemini: {resp.tool_call}")
                                function_responses = await process_tool_calls(resp.tool_call, websocket)
                                logger.info(f"Processed function responses: {function_responses}")
                                
                                # Send tool responses back to model
                                if function_responses:
                                    await session.send_tool_response(function_responses=function_responses)
                            else:
                                logger.debug("No tool_call in response")
                        except WebSocketDisconnect:
                            logger.info("WebSocket disconnected in forward_from_gemini")
                            websocket_active = False
                            break
                        except Exception as e:
                            if "disconnect message has been received" in str(e) or "Connection closed" in str(e):
                                logger.info(f"WebSocket connection closed: {e}")
                                websocket_active = False
                                break
                            else:
                                logger.error(f"Error sending response to client: {e}")
                except asyncio.CancelledError:
                    logger.info("Forward task cancelled")
                    break
                except Exception as e:
                    if "closed session" in str(e).lower():
                        logger.info("Gemini session closed")
                        break
                    else:
                        logger.error(f"Error in Gemini response handling: {e}")
                        await asyncio.sleep(0.5)  # Avoid tight loop if errors occur repeatedly
        except Exception as e:
            logger.error(f"Error in forward_from_gemini: {e}")
            logger.debug(traceback.format_exc())
            websocket_active = False

    # Run all tasks
    tasks = []
    try:
        tasks = [
            asyncio.create_task(keepalive()),
            asyncio.create_task(receive_from_client()),
            asyncio.create_task(forward_from_gemini())
        ]
        
        # Wait until one task completes or errors out, or until shutdown signal
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel remaining tasks
        for task in pending:
            task.cancel()
        
        # Allow tasks time to clean up
        await asyncio.sleep(0.5)
    finally:
        # Cancel any remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()
        
        try:
            # Clean up the Gemini session
            if session_cm:
                logger.info("Cleaning up Gemini session")
                try:
                    await asyncio.wait_for(session_cm.__aexit__(None, None, None), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Gemini session cleanup timed out")
                except Exception as e:
                    logger.error(f"Error during session cleanup: {e}")
                    
            # Remove from active connections
            if websocket in active_connections:
                active_connections.remove(websocket)
                
            # Close the websocket
            try:
                await websocket.close()
            except Exception:
                pass
                
            logger.info("WebSocket connection closed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

# Register signal handlers for graceful shutdown
async def shutdown():
    logger.info("Shutdown initiated, closing all connections...")
    shutdown_event.set()
    
    # Close all active WebSockets
    for ws in list(active_connections):
        try:
            await ws.close()
            active_connections.remove(ws)
        except Exception as e:
            logger.error(f"Error closing websocket: {e}")
    
    logger.info("All connections closed")

# Add signal handlers
@app.on_event("shutdown")
async def app_shutdown():
    await shutdown()

def handle_sigterm(*args):
    logger.info("SIGTERM received, initiating shutdown...")
    asyncio.create_task(shutdown())

if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGTERM, handle_sigterm)
    
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run("gemini_live_proxy_server:app", host="0.0.0.0", port=port, log_level="info")
