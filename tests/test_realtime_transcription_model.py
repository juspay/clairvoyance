from pipecat.services.openai.realtime.events import SessionProperties

from app.ai.voice.llm.realtime.azure_realtime import (
    AzureRealtimeConfig,
    build_azure_realtime_llm,
)
from app.ai.voice.llm.realtime.openai_realtime import (
    OpenAIRealtimeConfig,
    build_openai_realtime_llm,
)


def test_realtime_transcription_model_is_named():
    openai = build_openai_realtime_llm(OpenAIRealtimeConfig(api_key="test"))
    azure = build_azure_realtime_llm(
        AzureRealtimeConfig(api_key="test", base_url="wss://example.test/realtime")
    )

    for service in (openai, azure):
        properties = service._settings.session_properties
        assert isinstance(properties, SessionProperties)
        audio_input = properties.model_dump()["audio"]["input"]
        assert audio_input["transcription"]["model"] == "gpt-4o-transcribe"
