import asyncio
import sys
import argparse
from dotenv import load_dotenv
from loguru import logger
from datetime import datetime

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.transcriptions.language import Language
from pipecat.frames.frames import TTSSpeakFrame, BotSpeakingFrame, LLMFullResponseEndFrame
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIProcessor
from pipecat.services.google.rtvi import GoogleRTVIObserver

from app.core import config
from .processors import LLMSpyProcessor
from .prompts import SYSTEM_PROMPT
from .tools import initialize_tools
from opentelemetry import trace

load_dotenv(override=True)

# import setup_tracing from tracing_setup.py file
from app.agents.voice.automatic.analytics.tracing_setup import setup_tracing

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", type=str, required=True, help="URL of the Daily room")
    parser.add_argument("-t", "--token", type=str, required=True, help="Daily token")
    parser.add_argument("--mode", type=str, choices=["test", "live"], default="test", help="Mode (test or live)")
    parser.add_argument("--euler-token", type=str, help="Euler token for live mode")
    parser.add_argument("--breeze-token", type=str, help="Breeze token for live mode")
    parser.add_argument("--shop-url", type=str, help="Shop URL for live mode")
    parser.add_argument("--shop-id", type=str, help="Shop ID for live mode")
    parser.add_argument("--shop-type", type=str, help="Shop type for live mode")
    parser.add_argument("--user-name", type=str, help="User's name")
    args = parser.parse_args()

    # Initialize tools based on the mode and provided tokens
    tools, tool_functions = initialize_tools(
        mode=args.mode,
        breeze_token=args.breeze_token,
        euler_token=args.euler_token,
        shop_url=args.shop_url,
        shop_id=args.shop_id,
        shop_type=args.shop_type,
    )

    # Personalize the system prompt if a user name is provided
    system_prompt = SYSTEM_PROMPT
    if args.user_name:
        logger.info(f"Personalizing prompt for user: {args.user_name}")
        system_prompt = f"You are speaking with {args.user_name}. Make the conversation feel more personal by naturally using their name where appropriate. {SYSTEM_PROMPT}"
    else:
        system_prompt = SYSTEM_PROMPT

    transport = DailyTransport(
        args.url,
        args.token,
        "Breeze Automatic Voice Agent",
        DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    stt = GoogleSTTService(
        params=GoogleSTTService.InputParams(languages=[Language.EN_US, Language.EN_IN]),
        credentials=config.GOOGLE_CREDENTIALS_JSON
    )

    tts = GoogleTTSService(
        voice_id="en-IN-Chirp3-HD-Sadaltager",
        params=GoogleTTSService.InputParams(language=Language.EN_IN),
        credentials=config.GOOGLE_CREDENTIALS_JSON
    )

    llm = AzureLLMService(
        api_key=config.AZURE_OPENAI_API_KEY,
        endpoint=config.AZURE_OPENAI_ENDPOINT,
        model="gpt-4o-automatic",
    )

    for name, function in tool_functions.items():
        llm.register_function(name, function)

    # Simplified event handler for TTS feedback
    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
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

    context = OpenAILLMContext(messages, tools)
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
    shopId = args.shop_id or "dummy"
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    conversation_id=f"{user_name}-{shopId}-{timestamp}"

    setup_tracing("breeze-voice-agent")

    tracer = trace.get_tracer(__name__)
    
    with tracer.start_as_current_span(conversation_id) as root_span:
        root_span.set_attribute("conversation.id", conversation_id)
        root_span.set_attribute("conversation.type", "voice")
        root_span.set_attribute("user.name", user_name)
        root_span.set_attribute("service.name", "breeze-voice-agent")

        task = PipelineTask(
            pipeline,
            idle_timeout_secs=60.0,  
            idle_timeout_frames=(BotSpeakingFrame,
                            LLMFullResponseEndFrame),
            params=PipelineParams(allow_interruptions=True),
            cancel_on_idle_timeout=True,
            observers=[GoogleRTVIObserver(rtvi)],
            enable_tracing=True,
            conversation_id=conversation_id
        )

        @rtvi.event_handler("on_client_ready")
        async def on_client_ready(rtvi):
            await rtvi.set_bot_ready()

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            logger.debug(f"First participant joined: {participant['id']}")
            # Kick off the conversation
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
        try:
            await runner.run(task)
        except asyncio.CancelledError:
            logger.info("Main task cancelled. Exiting gracefully.")