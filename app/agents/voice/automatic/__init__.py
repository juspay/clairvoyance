import asyncio
import argparse
import time
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo

from opentelemetry import trace
from langfuse import get_client

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.filters.noisereduce_filter import NoisereduceFilter
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.rtvi import GoogleRTVIObserver
from pipecat.transcriptions.language import Language
from pipecat.services.google import GoogleSTTService
from pipecat.frames.frames import TTSSpeakFrame, BotSpeakingFrame, LLMFullResponseEndFrame
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIProcessor

from app.core import config
from app.core.logger import logger, configure_session_logger
from app.utils.session_context import create_session_context
from app.agents.voice.automatic.services.llm_wrapper import LLMServiceWrapper
from app.agents.voice.automatic.services.mcp.automatic_client import MCPClient
from app.agents.voice.automatic.analytics.tracing_setup import setup_tracing
from .processors import LLMSpyProcessor, UserMessageCaptureProcessor
from .prompts import get_system_prompt
from .tools import initialize_tools
from .services.mock_stt import TestQuestionProcessor, DEFAULT_TEST_QUESTIONS
from .tts import get_tts_service
from .types import (
    TTSProvider,
    Mode,
    decode_tts_provider,
    decode_voice_name,
    decode_mode,
)

load_dotenv(override=True)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", type=str, required=True, help="URL of the Daily room")
    parser.add_argument("-t", "--token", type=str, required=True, help="Daily token")
    parser.add_argument("--mode", type=str, help="Mode (TEST or LIVE)")
    parser.add_argument("--session-id", type=str, required=True, help="Session ID for logging")
    parser.add_argument("--euler-token", type=str, help="Euler token for live mode")
    parser.add_argument("--breeze-token", type=str, help="Breeze token for live mode")
    parser.add_argument("--shop-url", type=str, help="Shop URL for live mode")
    parser.add_argument("--shop-id", type=str, help="Shop ID for live mode")
    parser.add_argument("--shop-type", type=str, help="Shop type for live mode")
    parser.add_argument("--user-name", type=str, help="User's name")
    parser.add_argument("--tts-provider", type=str, help="TTS provider to use")
    parser.add_argument("--voice-name", type=str, help="Voice name to use")
    parser.add_argument("--merchant-id", type=str, help="Merchant Id of the Shop")
    parser.add_argument("--platform-integrations",type=str, nargs="+", help="Platform Integrations that are supported by the shop (string array)")
    args = parser.parse_args()

    # Configure logger with session ID for all logs in this subprocess
    configure_session_logger(args.session_id)
    logger.info(f"Voice agent started with session ID: {args.session_id}")
    
    # Create session context for passing to components
    session_context = create_session_context(args.session_id)

    # Decode TTS parameters
    tts_provider = decode_tts_provider(args.tts_provider)
    voice_name = decode_voice_name(args.voice_name)
    mode = decode_mode(args.mode)

    # Initialize tools based on the mode and provided tokens
    # Only pass tokens if in live mode
    
    use_automatic_mcp_server = config.AUTOMATIC_MCP_TOOL_SERVER_USAGE or \
        (args.shop_id and args.shop_id in config.SHOPS_FOR_AUTOMATIC_MCP_SERVER)

    # Personalize the system prompt if a user name is provided
    system_prompt = get_system_prompt(args.user_name, tts_provider)

    daily_params = DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(
                confidence=config.VAD_CONFIDENCE,
                start_secs=0.30,
                stop_secs=1.00,
                min_volume=config.VAD_MIN_VOLUME,
            )
        ),
    )

    if config.ENABLE_NOISE_REDUCE_FILTER:
        logger.info("Noise reduction filter enabled.")
        daily_params.audio_in_filter = NoisereduceFilter()
    else:
        logger.info("Noise reduction filter disabled.")

    transport = DailyTransport(
        args.url,
        args.token,
        "Breeze Automatic Voice Agent",
        daily_params,
    )

    stt = GoogleSTTService(
        params=GoogleSTTService.InputParams(languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False),
        credentials=config.GOOGLE_CREDENTIALS_JSON
    )

    llm = LLMServiceWrapper(AzureLLMService(
        api_key=config.AZURE_OPENAI_API_KEY,
        endpoint=config.AZURE_OPENAI_ENDPOINT,
        model=config.AZURE_OPENAI_MODEL,
        timeout=30.0,  # Add 30 second timeout for Azure OpenAI requests
        max_retries=2,  # Retry failed requests 2 times
    ))

    if not use_automatic_mcp_server:
        if mode == Mode.LIVE:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                breeze_token=args.breeze_token,
                euler_token=args.euler_token,
                shop_url=args.shop_url,
                shop_id=args.shop_id,
                shop_type=args.shop_type,
                merchant_id=args.merchant_id,
            )
        else:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                merchant_id=args.merchant_id,
            )
            
        for name, function in tool_functions.items():
            logger.info("Initializing the default function tools")
            llm.register_function(name, function)
    else:
        logger.info(f"Initializing tools from remote MCP server")
        
        mcp_context = {
            "sessionId": args.session_id,
            "juspayToken": args.euler_token,
            "shopUrl": args.shop_url,
            "shopId": args.shop_id,
            "shopType": args.shop_type,
            "userId": args.user_name,
            "enableDemoMode": mode != Mode.LIVE,
            "merchantId": args.merchant_id,
            "platformIntegrations": args.platform_integrations
        }
        mcp_client = MCPClient(
            server_url=config.AUTOMATIC_TOOL_MCP_SERVER_URL,
            auth_token=args.breeze_token,
            context=mcp_context,
            session_context=session_context
        )
        tools = await mcp_client.register_tools(llm)


    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    tts = get_tts_service(
        tts_provider=tts_provider.value, 
        voice_name=voice_name.value,
        session_id=args.session_id
    )
    # Simplified event handler for TTS feedback
    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        # Only play the "checking" message if using Google TTS
        if tts_provider == TTSProvider.GOOGLE:
            for function_call in function_calls:
                if function_call.function_name != "get_current_time":
                    await tts.queue_frame(TTSSpeakFrame("Let me check on that."))
                    break

    # Check if this is a reconnection and restore conversation context
    conversation_manager = None
    try:
        from .conversation_manager import get_conversation_manager
        conversation_manager = get_conversation_manager()
        existing_conversation = conversation_manager.get_conversation(args.session_id)
        
        # If not found in memory, try loading from database
        if not existing_conversation:
            try:
                existing_conversation = await conversation_manager.load_conversation_from_db(args.session_id, args.user_name, args.merchant_id)
                if existing_conversation:
                    logger.info(f"Loaded conversation from database for session {args.session_id}")
            except Exception as e:
                logger.warning(f"Failed to load conversation from database: {e}")
        
        if existing_conversation and existing_conversation.turns:
            logger.info(f"Restoring conversation context for session {args.session_id} with {len(existing_conversation.turns)} turns")
            
            # Modify system prompt for reconnection - remove the intro greeting instruction
            reconnection_system_prompt = system_prompt.replace(
                'Begin every session with:\n    "Hey, whatsup? How can I help you today?"',
                'This is a RECONNECTION to an existing session. Do NOT say any greeting. Continue the conversation naturally from where it left off.'
            )
            
            # Log to verify the replacement worked
            if 'RECONNECTION' in reconnection_system_prompt:
                logger.info(f"Successfully modified system prompt for reconnection in session {args.session_id}")
            else:
                logger.warning(f"Failed to modify system prompt for reconnection in session {args.session_id}")
            
            messages = [{"role": "system", "content": reconnection_system_prompt}]
            
            for turn in existing_conversation.turns:
                if turn.user_message:
                    messages.append({
                        "role": "user", 
                        "content": turn.user_message.content
                    })
                if turn.assistant_response:
                    messages.append({
                        "role": "assistant",
                        "content": turn.assistant_response.content
                    })
                    # Add tool calls and results if present
                    for tool_call in turn.tool_calls:
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": tool_call.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function_name,
                                    "arguments": tool_call.arguments
                                }
                            }]
                        })
                    for tool_result in turn.tool_results:
                        messages.append({
                            "role": "tool",
                            "content": tool_result.result,
                            "tool_call_id": tool_result.tool_call_id
                        })
        else:
            logger.info(f"No existing conversation found for session {args.session_id}, starting fresh")
            messages = [{"role": "system", "content": system_prompt}]
    except Exception as e:
        logger.warning(f"Could not restore conversation context: {e}, starting fresh")
        messages = [{"role": "system", "content": system_prompt}]

    context = llm.create_summarizing_context(
        messages,
        tools,
    )

    context_aggregator = llm.create_context_aggregator(context)

    # Add processors for conversation tracking
    user_message_capture = UserMessageCaptureProcessor(args.session_id)
    tool_call_processor = LLMSpyProcessor(rtvi, args.session_id, args.user_name, args.merchant_id)

    # Build pipeline components
    pipeline_components = [
        transport.input(),
        stt,
    ]
    
    if config.ENVIRONMENT.lower() in ["development", "dev"]:
        test_processor = TestQuestionProcessor(questions=DEFAULT_TEST_QUESTIONS)
        pipeline_components.append(test_processor)
        logger.info("Test Question Processor enabled (development mode)")
    
    pipeline_components.extend([
        user_message_capture,  # Capture user messages before context aggregator
        context_aggregator.user(),
        llm,
        tool_call_processor,
        rtvi,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])
    
    pipeline = Pipeline(pipeline_components)

    user_name = args.user_name or "guest"
    shopId = "euler" if args.euler_token and not args.shop_id else args.shop_id or "dummy"
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y-%m-%d_%H-%M-%S")
    conversation_id=f"{user_name}-{shopId}-{timestamp}"

    # Custom task class to intercept idle timeout before cancellation
    class IdleTimeoutNotifyingTask(PipelineTask):
        def __init__(self, pipeline, rtvi_instance, session_id, **kwargs):
            super().__init__(pipeline, **kwargs)
            self._rtvi = rtvi_instance
            self._session_id = session_id
        
        async def _idle_timeout_detected(self, frame_buffer):
            # Send notification BEFORE cancellation
            try:
                from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
                await self._rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "session-disconnected",
                            "payload": {
                                "reason": "idle_timeout",
                                "message": "Session disconnected due to inactivity",
                                "timestamp": int(time.time() * 1000),
                                "session_id": self._session_id
                            }
                        }
                    )
                )
                logger.info(f"Sent idle timeout notification to frontend for session {self._session_id}")
                # Brief delay to ensure message is sent before connection closes
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"Failed to notify frontend about idle timeout: {e}")
            
            # Now proceed with normal idle timeout handling
            return await super()._idle_timeout_detected(frame_buffer)

    task_params = {
        "idle_timeout_secs": 180.0,
        "idle_timeout_frames": (BotSpeakingFrame, LLMFullResponseEndFrame),
        "params": PipelineParams(allow_interruptions=True),
        "cancel_on_idle_timeout": True,
        "observers": [GoogleRTVIObserver(rtvi)],
    }

    if config.ENABLE_TRACING:
        setup_tracing("breeze-voice-agent")
        task_params["conversation_id"] = conversation_id
        task_params["enable_tracing"] = True

    task = IdleTimeoutNotifyingTask(pipeline, rtvi, args.session_id, **task_params)

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        # Send session-start event with session ID
        try:
            from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
            await rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "session-start",
                        "payload": {
                            "session_id": args.session_id,
                            "timestamp": int(time.time() * 1000),
                            "user_name": args.user_name or "guest",
                            "mode": mode.value if mode else "unknown"
                        }
                    }
                )
            )
            logger.info(f"Sent session-start event to frontend for session {args.session_id}")
        except Exception as e:
            logger.warning(f"Failed to send session-start event: {e}")
        
        await rtvi.set_bot_ready()

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"First participant joined: {participant['id']}")
        
        # Check if this is a reconnection with existing conversation
        # If so, don't automatically queue context to prevent re-processing previous questions
        if existing_conversation and existing_conversation.turns:
            logger.info(f"Reconnection detected for session {args.session_id} - skipping automatic context trigger")
            # For reconnections, we've already restored the context in the LLM
            # The system should wait for actual new user input before responding
            return
        
        # Only trigger context for fresh sessions (no existing conversation)
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info(f"Participant left: {participant['id']}")
        await task.cancel()

    @task.event_handler("on_pipeline_cancelled")
    async def on_pipeline_cancelled(task, frame):
        logger.info("Pipeline task cancelled. Cancelling main task.")
        main_task = asyncio.current_task()
        main_task.cancel()

    runner = PipelineRunner()

    async def run_pipeline():
        try:
            await runner.run(task)
        except asyncio.CancelledError:
            logger.info("Main task cancelled. Exiting gracefully.")
        except Exception as e:
            logger.error(f"Pipeline runner error: {e}")

    if config.ENABLE_TRACING:
        langfuse_client = get_client()
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(conversation_id) as root_span:
            logger.info(f"Starting current span with conversation ID: {conversation_id}")
            root_span.set_attribute("conversation.id", conversation_id)
            root_span.set_attribute("conversation.type", "voice")
            root_span.set_attribute("user.name", user_name)
            root_span.set_attribute("service.name", "breeze-voice-agent")
            langfuse_client.update_current_trace(user_id=user_name)
            langfuse_client.update_current_trace(session_id=args.session_id)
            langfuse_client.update_current_trace(tags=[voice_name])
            await run_pipeline()
    else:
        await run_pipeline()
