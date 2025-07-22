import asyncio
import sys
import argparse
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.logger import logger, configure_session_logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.filters.noisereduce_filter import NoisereduceFilter
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from app.agents.voice.automatic.services.llm_wrapper import LLMServiceWrapper
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.transcriptions.language import Language
from pipecat.frames.frames import TTSSpeakFrame, BotSpeakingFrame, LLMFullResponseEndFrame
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIProcessor
from pipecat.services.google.rtvi import GoogleRTVIObserver

from app.core import config
from app.agents.voice.automatic.services.mcp.automatic_client import MCPClient
from .processors import LLMSpyProcessor
from .prompts import get_system_prompt
from .tools import initialize_tools
from .tts import get_tts_service
from app.agents.voice.automatic.types import (
    TTSProvider,
    Mode,
    decode_tts_provider,
    decode_voice_name,
    decode_mode,
)
from opentelemetry import trace
from langfuse import get_client

load_dotenv(override=True)

# import setup_tracing from tracing_setup.py file
from app.agents.voice.automatic.analytics.tracing_setup import setup_tracing

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

    tts = get_tts_service(tts_provider=tts_provider.value, voice_name=voice_name.value)

    llm = LLMServiceWrapper(AzureLLMService(
        api_key=config.AZURE_OPENAI_API_KEY,
        endpoint=config.AZURE_OPENAI_ENDPOINT,
        model=config.AZURE_OPENAI_MODEL,
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
            context=mcp_context
        )
        tools = await mcp_client.register_tools(llm)

    # Simplified event handler for TTS feedback
    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        # Only play the "checking" message if using Google TTS
        if tts_provider == TTSProvider.GOOGLE:
            for function_call in function_calls:
                if function_call.function_name != "get_current_time":
                    await tts.queue_frame(TTSSpeakFrame("Let me check on that."))
                    break

    messages = [
        {
            "role": "system",
            "content": system_prompt
        },
    ]

    context = llm.create_summarizing_context(
        messages,
        tools,
    )

    context_aggregator = llm.create_context_aggregator(context)

    # RTVI events for Pipecat client UI
    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    # Add custom LLMSpyProcessor for streaming function call events
    tool_call_processor = LLMSpyProcessor(rtvi)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            rtvi,
            context_aggregator.user(),
            llm,
            tool_call_processor,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    user_name = args.user_name or "guest"
    shopId = "euler" if args.euler_token and not args.shop_id else args.shop_id or "dummy"
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y-%m-%d_%H-%M-%S")
    conversation_id=f"{user_name}-{shopId}-{timestamp}"

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

    task = PipelineTask(pipeline, **task_params)

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await rtvi.set_bot_ready()

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"First participant joined: {participant['id']}")
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
