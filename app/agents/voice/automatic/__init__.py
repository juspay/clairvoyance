import argparse
import asyncio
import json
import os
import random
import sys
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from langfuse import get_client
from opentelemetry import trace
from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.audio.filters.noisereduce_filter import NoisereduceFilter
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotSpeakingFrame,
    EmulateUserStartedSpeakingFrame,
    EmulateUserStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIProcessor
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.rtvi import GoogleRTVIObserver
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.utils.tracing.conversation_context_provider import (
    ConversationContextProvider,
)

from app.agents.voice.automatic.features.llm_wrapper import LLMServiceWrapper
from app.agents.voice.automatic.processors.llm_spy import handle_confirmation_response
from app.agents.voice.automatic.services.fal import FalSmartTurnService
from app.agents.voice.automatic.services.mcp import init_breeze_mcp_tools
from app.agents.voice.automatic.services.mem0.memory import ImprovedMem0MemoryService
from app.agents.voice.automatic.tools.charts import (
    tool_functions as chart_tool_functions,
)
from app.agents.voice.automatic.types import (
    Mode,
    TTSProvider,
    decode_mode,
    decode_tts_provider,
    decode_voice_name,
)
from app.agents.voice.automatic.utils.session_context import (
    create_session_context,
    set_current_session_id,
)
from app.core import config
from app.core.logger import configure_session_logger, logger

from .processors import LLMSpyProcessor
from .processors.ptt_vad_filter import PTTVADFilter
from .prompts import get_system_prompt
from .stt import get_stt_service
from .tools import initialize_tools
from .tts import get_tts_service

# Load tool call sound
tool_call_sound = None
if config.ENABLE_TOOL_CALL_SOUND and os.path.exists(config.TOOL_CALL_SOUND_FILE):
    with wave.open(config.TOOL_CALL_SOUND_FILE) as audio_file:
        tool_call_sound = OutputAudioRawFrame(
            audio_file.readframes(-1),
            audio_file.getframerate(),
            audio_file.getnchannels(),
        )


# import setup_tracing from tracing_setup.py file
from app.agents.voice.automatic.analytics.tracing_setup import setup_tracing
from app.agents.voice.automatic.analytics.utils import (
    generate_open_observer_url_for_session_id,
)

# Simple environment loading - subprocess inherits from parent
load_dotenv(override=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", type=str, help="URL of the Daily room")
    parser.add_argument("-t", "--token", type=str, help="Daily token")
    parser.add_argument("--mode", type=str, help="Mode (TEST or LIVE)")
    parser.add_argument("--session-id", type=str, help="Session ID for logging")
    parser.add_argument("--client-sid", type=str, help="Client session ID for logging")
    parser.add_argument("--euler-token", type=str, help="Euler token for live mode")
    parser.add_argument("--breeze-token", type=str, help="Breeze token for live mode")
    parser.add_argument("--shop-url", type=str, help="Shop URL for live mode")
    parser.add_argument("--shop-id", type=str, help="Shop ID for live mode")
    parser.add_argument("--shop-type", type=str, help="Shop type for live mode")
    parser.add_argument("--user-name", type=str, help="User's name")
    parser.add_argument("--user-email", type=str, help="User's email address")
    parser.add_argument("--tts-provider", type=str, help="TTS provider to use")
    parser.add_argument("--voice-name", type=str, help="Voice name to use")
    parser.add_argument("--merchant-id", type=str, help="Merchant Id of the Shop")
    parser.add_argument(
        "--platform-integrations",
        type=str,
        nargs="+",
        help="Platform Integrations that are supported by the shop (string array)",
    )
    parser.add_argument("--reseller-id", type=str, help="Reseller ID")

    # Pool mode arguments
    parser.add_argument("--pool-mode", action="store_true", help="Run in pool mode")
    parser.add_argument("--process-id", type=str, help="Process ID for pool mode")

    args = parser.parse_args()

    # Validate arguments
    if args.pool_mode and (not args.process_id or not args.process_id.strip()):
        parser.error("--process-id is required when --pool-mode is used.")

    if args.pool_mode:
        await run_pool_mode(args)
    else:
        await run_normal_mode(args)


async def run_pool_mode(args):
    """Run in pool mode - wait for session assignments"""
    logger.info(f"Voice agent process {args.process_id} starting in pool mode")

    try:
        await pre_initialize_services()
        print("READY", flush=True)

        # Wait for session assignments
        while True:
            try:
                # Run the blocking readline call in a separate thread
                line = await asyncio.to_thread(sys.stdin.readline)

                # An empty string from readline indicates EOF
                if line == "":
                    logger.info("Pool process received EOF, shutting down")
                    break

                if line.strip():
                    try:
                        session_config = json.loads(line.strip())
                        await handle_session(session_config)
                    except json.JSONDecodeError as json_err:
                        logger.error(f"Failed to decode session config: {json_err}")
                        # Don't break the worker, just log and continue
                        continue

            except Exception as e:
                logger.error(
                    f"An unexpected error occurred in pool mode: {e}", exc_info=True
                )
                break

    except Exception as e:
        logger.error(f"Failed to initialize pool mode: {e}")
        print(f"ERROR: {e}", flush=True)

    logger.info("Pool process shutting down")


async def pre_initialize_services():
    """Pre-load heavy services for faster session startup"""
    logger.info("Pre-initializing services for pool mode")

    try:
        # Pre-initialize Silero VAD model
        await _pre_init_silero_vad()

        logger.info("Services pre-initialized successfully")

    except Exception as e:
        logger.error(f"Error during service pre-initialization: {e}")
        # Don't fail the process - fallback to normal initialization
        logger.info("Continuing with normal initialization fallback")


async def _pre_init_silero_vad():
    """Pre-initialize Silero VAD model"""
    try:
        # Pre-load the Silero VAD model
        vad_params = VADParams(
            confidence=config.VAD_CONFIDENCE,
            start_secs=config.VAD_START_SECS,
            stop_secs=config.VAD_STOP_SECS,
            min_volume=config.VAD_MIN_VOLUME,
        )

        # Store in global cache for reuse
        global _silero_vad_cache
        _silero_vad_cache = {"sample_rate": config.SAMPLE_RATE, "params": vad_params}

        logger.info("Silero VAD model pre-loaded")

    except Exception as e:
        logger.debug(f"Silero VAD pre-init failed (will fallback): {e}")


# Global caches for pre-initialized services
_silero_vad_cache = None


async def handle_session(session_config):
    """Handle a session with the given configuration"""
    session_id = session_config.get("session_id")

    # Simple args object from session config
    class SessionArgs:
        def __init__(self, config):
            for key, value in config.items():
                setattr(self, key.replace("-", "_"), value)
            # Map room_url to url for compatibility
            self.url = config.get("room_url")

    session_args = SessionArgs(session_config)

    try:
        await run_normal_mode(session_args)
    except Exception as e:
        logger.error(f"Session {session_id} ended with error: {e}")
    finally:
        logger.info(f"Session {session_id} completed, process ready for next session")
        print("SESSION_ENDED", flush=True)


async def run_normal_mode(args):
    """Run the normal voice agent mode"""
    # Validate required arguments for normal mode
    if not args.url or not args.token or not args.session_id:
        logger.error("Missing required arguments for normal mode")
        return

    # Configure logger with session ID and client session ID for all logs in this subprocess
    configure_session_logger(args.session_id, args.client_sid)
    logger.info(
        f"Voice agent started with session ID: {args.session_id}, client session ID: {args.client_sid}"
    )

    # Create session context for passing to components
    session_context = create_session_context(args.session_id)

    # Set global session ID for chart tools
    set_current_session_id(args.session_id)

    # Decode TTS parameters
    tts_provider = decode_tts_provider(args.tts_provider)
    voice_name = decode_voice_name(args.voice_name)
    mode = decode_mode(args.mode)

    # Initialize tools based on the mode and provided tokens
    # Only pass tokens if in live mode

    use_breeze_mcp_server = config.ENABLE_BREEZE_MCP and (
        not config.SHOPS_FOR_BREEZE_MCP  # Empty list = all shops
        or args.shop_id in config.SHOPS_FOR_BREEZE_MCP  # Specific shops only
    )

    # Personalize the system prompt if a user name is provided
    system_prompt = get_system_prompt(args.user_name, tts_provider)

    # Configure VAD - use pre-initialized model if available
    global _silero_vad_cache
    if _silero_vad_cache:
        logger.info("Using pre-initialized Silero VAD model")
        vad_analyzer = SileroVADAnalyzer(
            sample_rate=_silero_vad_cache["sample_rate"],
            params=_silero_vad_cache["params"],
        )
    else:
        # Fallback to normal initialization
        logger.info("Using fallback Silero VAD initialization")
        vad_params = VADParams(
            confidence=config.VAD_CONFIDENCE,
            start_secs=config.VAD_START_SECS,
            stop_secs=config.VAD_STOP_SECS,  # Use normal timeout - Smart Turn will intercept and decide
            min_volume=config.VAD_MIN_VOLUME,
        )

        vad_analyzer = SileroVADAnalyzer(
            sample_rate=config.SAMPLE_RATE,
            params=vad_params,
        )

    # Initialize Fal.ai Smart Turn service
    smart_turn_analyzer = None
    fal_session = None
    fal_smart_turn_service = None

    if config.ENABLE_FAL_SMART_TURN:
        if config.FAL_SMART_TURN_API_KEY:
            fal_smart_turn_service = FalSmartTurnService()
            smart_turn_analyzer, fal_session = (
                await fal_smart_turn_service.create_analyzer()
            )
        else:
            logger.warning(
                "SMART_TURN: Fal.ai Smart Turn is enabled but FAL_SMART_TURN_API_KEY is missing; skipping."
            )

    daily_params = DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=None if config.DISABLE_SILERO_VAD else vad_analyzer,
        turn_analyzer=smart_turn_analyzer,
    )

    # Audio filter configuration
    if config.ENABLE_AIC_FILTER and config.AICOUSTICS_LICENSE_KEY:
        try:
            aic_filter = AICFilter(
                license_key=config.AICOUSTICS_LICENSE_KEY,
                enhancement_level=config.AIC_ENHANCEMENT_LEVEL,
                voice_gain=config.AIC_VOICE_GAIN,
                noise_gate_enable=config.AIC_NOISE_GATE_ENABLE,
            )
            daily_params.audio_in_filter = aic_filter
            logger.info(
                f"AIC Filter: ENABLED (enhancement_level={config.AIC_ENHANCEMENT_LEVEL}, voice_gain={config.AIC_VOICE_GAIN}, noise_gate={config.AIC_NOISE_GATE_ENABLE})"
            )

        except Exception as e:
            logger.error(f"AIC Filter failed: {e}")
    elif config.ENABLE_NOISE_REDUCE_FILTER:
        daily_params.audio_in_filter = NoisereduceFilter()
        logger.info("Audio Filter: NoiseReduce Enabled")
    else:
        logger.info("No Audio Filter enabled")

    transport = DailyTransport(
        args.url,
        args.token,
        "Breeze Automatic Voice Agent",
        daily_params,
    )

    stt = get_stt_service(voice_name=voice_name.value)

    tts = get_tts_service(
        tts_provider=tts_provider.value,
        voice_name=voice_name.value,
        session_id=args.session_id,
        enable_chart_text_filter=config.ENABLE_CHARTS,
    )

    llm = LLMServiceWrapper(
        AzureLLMService(
            api_key=config.AZURE_OPENAI_API_KEY,
            endpoint=config.AZURE_OPENAI_ENDPOINT,
            model=config.AZURE_OPENAI_MODEL,
        )
    )

    if not use_breeze_mcp_server:
        # Initialize tools normally
        if mode == Mode.LIVE:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                breeze_token=args.breeze_token,
                euler_token=args.euler_token,
                shop_url=args.shop_url,
                shop_id=args.shop_id,
                shop_type=args.shop_type,
                merchant_id=args.merchant_id,
                session_id=args.client_sid,  # Pass client_sid instead of session_id
                user_id=args.user_name,
                user_email=args.user_email,
                reseller_id=args.reseller_id,
            )
        else:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                merchant_id=args.merchant_id,
                session_id=args.client_sid,  # Pass client_sid instead of session_id
                reseller_id=args.reseller_id,
            )

        for name, function in tool_functions.items():
            logger.info("Initializing the default function tools")
            llm.register_function(name, function)
    else:
        logger.info(f"Initializing tools from remote MCP server")

        mcp_context = {
            "sessionId": args.client_sid,  # Pass client_sid instead of session_id
            "juspayToken": args.euler_token,
            "shopUrl": args.shop_url,
            "shopId": args.shop_id,
            "shopType": args.shop_type,
            "userId": args.user_name,
            "userEmail": args.user_email,
            "enableDemoMode": mode != Mode.LIVE,
            "merchantId": args.merchant_id,
            "platformIntegrations": args.platform_integrations,
        }

        tools = await init_breeze_mcp_tools(
            llm=llm,
            mcp_context=mcp_context,
            breeze_token=args.breeze_token,
            reseller_id=args.reseller_id,
            mode=mode,
            args=args,
        )

    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    # Simplified event handler for TTS feedback
    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        # Only play the "checking" message if using Google TTS
        if tts_provider == TTSProvider.GOOGLE:
            for function_call in function_calls:
                # Skip "checking" message for instant functions and chart tools
                instant_functions = [
                    "get_current_time",
                    "utility__getCurrentTime",  # NeuroLink equivalent
                    "utility__generateTimestamp",  # NeuroLink timestamp tool
                    "generate_bar_chart",
                    "generate_line_chart",
                    "generate_donut_chart",
                    "generate_single_stat_card",
                ]
                if function_call.function_name not in instant_functions:
                    # Play tool call sound if enabled, otherwise use phrases
                    if tool_call_sound:
                        await transport.send_audio(tool_call_sound)
                    else:
                        phrases = [
                            "Let me check on that.",
                            "Give me a moment to do that.",
                            "I'll get right on that.",
                            "Working on that for you.",
                            "One moment — I'm on it",
                            "One second, boss.",
                            "On it, boss!",
                            "Just a second, captain.",
                        ]
                        await tts.queue_frame(TTSSpeakFrame(random.choice(phrases)))
                    break

    messages = [
        {"role": "system", "content": system_prompt},
    ]

    context = llm.create_summarizing_context(
        messages,
        tools,
    )

    context_aggregator = llm.create_context_aggregator(context)

    # Add custom LLMSpyProcessor for streaming function call events (RTVI and TTS created earlier)
    tool_call_processor = LLMSpyProcessor(
        rtvi, args.session_id, config.ENABLE_CHARTS, "LLMSpyProcessor"
    )

    # Build pipeline components list
    pipeline_components = [
        transport.input(),
        stt,
    ]

    # Add PTT VAD filter only if it's enabled
    if config.DISABLE_VAD_FOR_PTT:
        ptt_vad_filter = PTTVADFilter("PTTVADFilter")
        pipeline_components.append(ptt_vad_filter)  # Filter VAD frames after STT

    pipeline_components.extend([rtvi, context_aggregator.user()])
    if (
        config.MEM0_ENABLED
        and args.user_email
        and args.user_email.strip()
        and config.MEM0_API_KEY
        and config.MEM0_API_KEY.strip()
    ):
        try:
            logger.info("Initializing Mem0 memory service")
            memory_params = ImprovedMem0MemoryService.InputParams()
            memory = ImprovedMem0MemoryService(
                api_key=config.MEM0_API_KEY,
                user_id=args.user_email,
                params=memory_params,
            )
            pipeline_components.append(memory)
            logger.info("Mem0 memory service initialized successfully")
        except (ValueError, Exception) as e:
            logger.error(f"Failed to initialize Mem0 memory service: {e}")
            logger.warning(
                "Continuing without memory service - conversation will work normally"
            )
    elif config.MEM0_ENABLED:
        if not args.user_email:
            logger.info(
                "Skipping Mem0 memory service - no user email provided (guest flow)"
            )
        elif not config.MEM0_API_KEY or not config.MEM0_API_KEY.strip():
            logger.warning("MEM0_API_KEY is not provided - skipping memory service")
    else:
        logger.debug("Mem0 memory service disabled via config")

    # Add remaining components
    pipeline_components.extend(
        [
            llm,
            tool_call_processor,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    pipeline = Pipeline(pipeline_components)

    user_name = args.user_name or "guest"
    shopId = (
        "euler" if args.euler_token and not args.shop_id else args.shop_id or "dummy"
    )
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y-%m-%d_%H-%M-%S")
    conversation_id = f"{user_name}-{shopId}-{timestamp}"

    task_params = {
        "idle_timeout_secs": config.AUTOMATIC_SESSION_INACTIVITY_TIMEOUT,
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

    @rtvi.event_handler("on_client_message")
    async def on_client_message(rtvi, message):
        """Handle incoming messages from RTVI client, including function confirmation responses and PTT events"""
        try:
            if isinstance(message, dict):
                message_type = message.get("type")

                if message_type == "function-confirmation-response":
                    confirmation_id = message.get("confirmationId")
                    approved = message.get("approved", False)
                    reason = message.get("reason", "")

                    if confirmation_id:
                        response = {"approved": approved, "reason": reason}
                        handle_confirmation_response(confirmation_id, response)
                        logger.info(
                            f"Processed function confirmation response: {confirmation_id} -> {approved}"
                        )
                    else:
                        logger.warning(
                            "Received function confirmation response without confirmationId"
                        )

                elif message_type == "ptt-start":
                    # Handle PTT start event
                    logger.debug("PTT started - activating VAD filter")
                    ptt_vad_filter.set_ptt_active(True)
                    # Send emulated user started speaking frame
                    await task.queue_frames([EmulateUserStartedSpeakingFrame()])

                elif message_type == "ptt-end":
                    # Handle PTT end event
                    logger.debug(
                        "PTT ended - deactivating VAD filter and sending stop frame"
                    )
                    ptt_vad_filter.set_ptt_active(False)
                    # Send emulated user stopped speaking frame
                    await task.queue_frames([EmulateUserStoppedSpeakingFrame()])

                elif message_type == "ptt-sync":
                    # Handle PTT state synchronization from client
                    client_ptt_state = message.get("data", {}).get("ptt_active", False)
                    current_state = ptt_vad_filter._ptt_active

                    if client_ptt_state != current_state:
                        logger.warning(
                            f"PTT state mismatch! client: {client_ptt_state}, server: {current_state}"
                        )
                        # Sync to client state (client is authoritative)
                        ptt_vad_filter.set_ptt_active(client_ptt_state)
                        logger.info(f"PTT state synchronized to: {client_ptt_state}")

                        # Send appropriate frames for state change
                        if client_ptt_state:
                            await task.queue_frames([EmulateUserStartedSpeakingFrame()])
                        else:
                            await task.queue_frames([EmulateUserStoppedSpeakingFrame()])
                    else:
                        logger.debug(
                            f"PTT state sync: states match (current_state: {current_state})"
                        )

        except Exception as e:
            logger.error(f"Error handling RTVI client message: {e}")

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"First participant joined: {participant['id']}")
        if config.ENABLE_AUTOMATIC_DAILY_RECORDING:
            await transport.start_recording()

        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info(f"Participant left: {participant['id']}")
        if config.ENABLE_AUTOMATIC_DAILY_RECORDING:
            await transport.stop_recording()
        await task.cancel()

    # Route Daily transport messages to RTVI for function confirmations
    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        """Route function confirmation messages from Daily transport to RTVI"""

        # Check if this is a function confirmation message or PTT message and route to RTVI
        if isinstance(message, dict):
            message_type = message.get("type")
            if message_type == "function-confirmation-response" or (
                config.DISABLE_VAD_FOR_PTT
                and message_type in ["ptt-start", "ptt-end", "ptt-sync"]
            ):
                # Manually trigger the RTVI handler since it might not be getting the message
                try:
                    await on_client_message(rtvi, message)
                except Exception as e:
                    logger.error(f"Error manually routing message to RTVI: {e}")

    @task.event_handler("on_pipeline_cancelled")
    async def on_pipeline_cancelled(task, frame):
        logger.info("Pipeline task cancelled. Cancelling main task.")
        # Clean up Fal.ai Smart Turn session
        if fal_smart_turn_service:
            await fal_smart_turn_service.cleanup(fal_session)
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
            logger.info(
                f"Starting current span with conversation ID: {conversation_id}"
            )
            root_span.set_attribute("conversation_id", conversation_id)
            root_span.set_attribute("conversation_type", "voice")
            root_span.set_attribute("user_name", user_name)
            root_span.set_attribute("shop_id", args.shop_id)
            root_span.set_attribute("shop_type", args.shop_type)
            root_span.set_attribute("shop_url", args.shop_url)
            root_span.set_attribute("merchant_id", args.merchant_id)
            root_span.set_attribute("service.name", "breeze-voice-agent")
            root_span.set_attribute("client_sid", args.client_sid)
            root_span.set_attribute(
                "application_logs",
                generate_open_observer_url_for_session_id(args.client_sid),
            )
            langfuse_client.update_current_trace(
                user_id=args.user_email,
                session_id=args.session_id,
                tags=[
                    (
                        voice_name.value
                        if hasattr(voice_name, "value")
                        else str(voice_name)
                    )
                ],
            )

            # Set Pipecat conversation context for proper tool call nesting
            provider = ConversationContextProvider.get_instance()
            provider.set_current_conversation_context(
                root_span.get_span_context(), conversation_id
            )
            logger.info(
                f"Set Pipecat conversation context with span ID: {root_span.get_span_context().span_id}"
            )

            await run_pipeline()
    else:
        await run_pipeline()
