"""Breeze Buddy custom processors for pipeline control."""

from app.ai.voice.agents.breeze_buddy.processors.knowledge_retrieval import (
    KnowledgeRetrievalProcessor,
)
from app.ai.voice.agents.breeze_buddy.processors.speaker_diarization_gate import (
    SpeakerDiarizationGateProcessor,
)
from app.ai.voice.agents.breeze_buddy.processors.transcript_collector import (
    TranscriptCollectorProcessor,
)
from app.ai.voice.agents.breeze_buddy.processors.transcription_gate import (
    TranscriptionGateProcessor,
)
from app.ai.voice.agents.breeze_buddy.processors.user_idle import (
    UserIdleCallbackHandler,
)
from app.ai.voice.agents.breeze_buddy.processors.voice_ui_stream import (
    VoiceUiStreamProcessor,
)

__all__ = [
    "KnowledgeRetrievalProcessor",
    "SpeakerDiarizationGateProcessor",
    "TranscriptCollectorProcessor",
    "TranscriptionGateProcessor",
    "UserIdleCallbackHandler",
    "VoiceUiStreamProcessor",
]
