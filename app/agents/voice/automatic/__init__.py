import asyncio
import sys
import argparse

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.transcriptions.language import Language
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIProcessor
from pipecat.services.google.rtvi import GoogleRTVIObserver

from app.core import config
from .processors import LLMSpyProcessor
from .prompts import SYSTEM_PROMPT
from .tools import tools, all_tool_functions as tool_functions

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", type=str, required=True, help="URL of the Daily room")
    parser.add_argument("-t", "--token", type=str, required=True, help="Daily token")
    args = parser.parse_args()

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
            "content": SYSTEM_PROMPT
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

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
        observers=[GoogleRTVIObserver(rtvi)],
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

    runner = PipelineRunner()
    await runner.run(task)