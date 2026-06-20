"""
Pydantic models for the dynamic workflow engine.
"""

from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    StringConstraints,
    field_serializer,
    model_validator,
)

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import ActionUnion, Icon
from app.ai.voice.llm.types import LLMConfiguration
from app.core.deprecation import log_deprecated_fields
from app.core.logger import logger


class ActionType(str, Enum):
    TTS_SAY = "tts_say"
    END_CONVERSATION = "end_conversation"
    FUNCTION = "function"


class VadConfig(BaseModel):
    """VAD configuration for template or node-level customization."""

    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="VAD confidence threshold (0.0-1.0)"
    )
    start_secs: Optional[float] = Field(
        None, ge=0.0, description="Seconds of speech before starting transcription"
    )
    stop_secs: Optional[float] = Field(
        None, ge=0.0, description="Seconds of silence before stopping transcription"
    )
    min_volume: Optional[float] = Field(
        None, ge=0.0, description="Minimum volume threshold for VAD"
    )


# ---------------------------------------------------------------------------
# STT Configuration (normalized, provider-agnostic)
# ---------------------------------------------------------------------------


class STTProvider(str, Enum):
    """Supported STT providers."""

    SONIOX = "soniox"
    DEEPGRAM = "deepgram"
    SARVAM = "sarvam"
    OPENAI = "openai"
    GOOGLE = "google"


class SonioxSTTConfig(BaseModel):
    """Soniox-specific STT settings."""

    context: Optional[str] = Field(
        None,
        description="Soniox context JSON for domain adaptation "
        "(business terms, product names). Overrides env default if set.",
    )
    model: Optional[str] = Field(
        None, description="Soniox model (e.g. 'stt-rt-v4'). Defaults from env."
    )
    enable_language_identification: Optional[bool] = Field(
        None,
        description="Enable automatic language identification. Defaults to None.",
    )


class DeepgramSTTConfig(BaseModel):
    """Deepgram-specific STT settings.

    All params have sensible defaults — only override what you need.
    """

    model: str = Field("nova-3-general", description="Deepgram model.")
    endpointing_ms: bool | int = Field(
        25,
        description="Silence threshold before endpointing fires. "
        "Integer for exact ms (e.g. 25), True for Deepgram default (10ms), "
        "False to disable. Low values (25ms) are ideal with SmartTurn.",
    )
    utterance_end_ms: Optional[int] = Field(
        None,
        ge=1000,
        description="Max silence (ms) after last word before UtteranceEnd event. "
        "None = disabled (recommended with SmartTurn). "
        "Deepgram minimum is 1000ms when enabled.",
    )
    smart_format: bool = Field(
        True, description="Smart formatting (phone numbers, dates, currency)."
    )
    punctuate: bool = Field(True, description="Add punctuation to transcription.")
    numerals: bool = Field(
        True,
        description="Convert spoken numbers to numerals (critical for Indian lakhs/crores).",
    )
    profanity_filter: bool = Field(
        False, description="Filter profanity (disabled for business context)."
    )
    diarize: bool = Field(
        False, description="Speaker diarization (disabled for single-speaker)."
    )
    auto_detect_language: bool = Field(
        False, description="Auto-detect language (uses 'multi' parameter)."
    )


class SarvamSTTConfig(BaseModel):
    """Sarvam-specific STT settings."""

    model: Optional[str] = Field(
        None, description="Sarvam model (e.g. 'saaras:v3'). Defaults from env."
    )
    language_code: Optional[str] = Field(
        None, description="Sarvam language code. Defaults from env."
    )


class SmartTurnConfig(BaseModel):
    """SmartTurn ML turn-detection settings.

    Controls the ONNX-based Whisper model that analyzes audio prosody and
    intonation to predict when the user is done speaking.  Only used when
    ``turn_detection='smart_turn'``.
    """

    stop_secs: float = Field(
        3.0,
        ge=0.0,
        description="Max silence (seconds) before forcing turn end even if "
        "the ML model hasn't triggered. Safety timeout.",
    )
    pre_speech_ms: float = Field(
        500.0,
        ge=0.0,
        description="Milliseconds of audio to include before speech starts, "
        "giving the ML model extra context for better predictions.",
    )
    max_duration_secs: float = Field(
        8.0,
        ge=1.0,
        description="Max audio segment duration (seconds) fed to the ONNX model. "
        "Longer segments give more context but increase inference time.",
    )
    cpu_count: int = Field(
        1,
        ge=1,
        description="Number of CPUs for ONNX Runtime inference. "
        "Keep at 1 for single-CPU deployments.",
    )


class TurnDetectionMode(str, Enum):
    """How the pipeline detects when the user is done speaking.

    STT_NATIVE:  Provider handles endpoint detection natively.
                 Soniox sends <end> token; pipeline fires immediately.
                 Default — matches current production behavior.
    SMART_TURN:  SmartTurn ML model (Whisper-based, 8 MB ONNX) analyzes audio
                 prosody / intonation to predict turn completion.
                 Auto-enables Silero VAD (stop_secs=0.2) as trigger.
                 Best for Deepgram Nova-3.
    TIMEOUT:     Simple timer after last finalized transcript.  Resets on each
                 new transcript.  Configurable via user_speech_timeout.
    """

    STT_NATIVE = "stt_native"
    SMART_TURN = "smart_turn"
    TIMEOUT = "timeout"


class STTConfiguration(BaseModel):
    """Template-level STT configuration.

    Normalized, provider-agnostic model. Provider-specific settings go in the
    matching nested config (``soniox``, ``deepgram``, ``sarvam``).
    Turn detection mode is included here because it's tightly coupled to the
    provider choice (soniox → stt_native, deepgram → smart_turn).

    When not set on a template, falls back to BREEZE_BUDDY_STT_SERVICE env var
    and provider-specific env defaults.

    Examples::

        # Deepgram Nova-3 with SmartTurn
        {"provider": "deepgram", "language": "en",
         "turn_detection": "smart_turn",
         "deepgram": {"model": "nova-3-general"}}

        # Soniox with domain context (default turn detection)
        {"provider": "soniox", "language": ["en", "hi"],
         "soniox": {"context": "{...}"}}

        # Deepgram with simple timeout
        {"provider": "deepgram", "turn_detection": "timeout",
         "user_speech_timeout": 0.5}

        # Minimal — all defaults from env
        {"provider": "soniox"}
    """

    provider: STTProvider = Field(
        STTProvider.SONIOX,
        description="STT provider to use for this template.",
    )
    language: Optional[str | list[str]] = Field(
        None,
        description="Language code(s) for STT. String or list "
        "(e.g. 'en', ['en', 'hi']). Provider-specific handling.",
    )
    payload_based_language_selection: bool = Field(
        False,
        description="Use LLM to detect language from lead payload.",
    )
    turn_detection: TurnDetectionMode = Field(
        TurnDetectionMode.STT_NATIVE,
        description="Turn detection strategy. stt_native for Soniox, "
        "smart_turn for Deepgram Nova-3 (ML-based), timeout for simple timer.",
    )
    user_speech_timeout: float = Field(
        0.3,
        ge=0.0,
        description="Seconds to wait after last finalized transcript before "
        "ending turn. Only used when turn_detection='timeout'.",
    )

    # Provider-specific — only the matching one is used at runtime
    soniox: Optional[SonioxSTTConfig] = None
    deepgram: Optional[DeepgramSTTConfig] = None
    sarvam: Optional[SarvamSTTConfig] = None

    # SmartTurn ML config — only used when turn_detection='smart_turn'
    smart_turn: Optional[SmartTurnConfig] = None

    @model_validator(mode="after")
    def _normalize_user_speech_timeout(self) -> "STTConfiguration":
        """Force user_speech_timeout=0.0 for non-TIMEOUT modes.

        STT_NATIVE fires immediately on finalized transcript (0.0s).
        SMART_TURN uses its own ML-based detection (timeout irrelevant).
        Only TIMEOUT mode honors the configured user_speech_timeout.
        """
        if self.turn_detection != TurnDetectionMode.TIMEOUT:
            self.user_speech_timeout = 0.0
        return self


class NoiseFilterType(str, Enum):
    """Types of noise filters available for audio input processing."""

    AIC = "aic"  # ai-coustics noise enhancement filter


class NoiseFilterConfig(BaseModel):
    """Configuration for audio input noise filtering."""

    enable: bool = Field(False, description="Whether to enable the noise filter")
    type: NoiseFilterType = Field(
        NoiseFilterType.AIC, description="Type of noise filter to use"
    )


class TTSProvider(str, Enum):
    """Supported TTS providers."""

    ELEVENLABS = "elevenlabs"
    CARTESIA = "cartesia"
    SARVAM = "sarvam"
    GEMINI = "gemini"
    GOOGLE = "google"
    SONIOX = "soniox"


# Maps legacy tts_voice_name values to current provider strings for backward compat.
# Used by decoder migration and runtime lead payload resolution.
LEGACY_VOICE_TO_PROVIDER: Dict[str, str] = {
    "rhea": "elevenlabs",
    "mira": "cartesia",
    "sara": "sarvam",
}


class TTSConfig(BaseModel):
    """Unified TTS configuration — provider + provider-specific settings.

    Template-level values override global Redis defaults.
    Provider-specific fields (e.g. emotion for Cartesia, pitch for Sarvam)
    are silently ignored when irrelevant to the chosen provider.

    Example (Cartesia):
        {
            "provider": "cartesia",
            "voice_id": "248be419-c632-4f23-adf1-5324ed7dbf1d",
            "volume": 1.8,
            "speed": 1.2,
            "emotion": "excited",
            "language": "hi"
        }

    Example (ElevenLabs):
        {
            "provider": "elevenlabs",
            "voice_id": "fG9s0SXJb213f4UxVHyG",
            "model": "eleven_flash_v2_5",
            "speed": 1.2,
            "language": "en"
        }

    Example (Sarvam):
        {
            "provider": "sarvam",
            "voice_id": "manisha",
            "model": "bulbul:v2",
            "language": "en-IN",
            "speed": 0.9,
            "pitch": 0.0
        }

    Example (Soniox):
        {
            "provider": "soniox",
            "voice_id": "Priya",
            "model": "tts-rt-v1",
            "language": "en"
        }

    Example (Google Cloud TTS — Chirp 3 HD):
        {
            "provider": "google",
            "voice_id": "en-IN-Chirp3-HD-Despina",
            "language": "en-IN"
        }
    """

    provider: TTSProvider = Field(
        ...,
        description="TTS provider (elevenlabs, cartesia, sarvam, gemini, google, soniox)",
    )
    voice_id: Optional[str] = Field(None, description="Provider-specific voice ID")
    model: Optional[str] = Field(
        None,
        description="Provider model (e.g. 'eleven_flash_v2_5', 'sonic-3', 'bulbul:v2', 'gemini-3.1-flash-tts-preview')",
    )
    language: Optional[str] = Field(
        None, description="TTS language code (e.g. 'en', 'hi', 'en-IN')"
    )
    speed: Optional[float] = Field(None, description="Speed/pace multiplier")
    volume: Optional[float] = Field(
        None, description="Volume multiplier (Cartesia only, range 0.5-2.0)"
    )
    emotion: Optional[str] = Field(
        None,
        description="Voice emotion (Cartesia only, e.g. 'neutral', 'excited', 'happy')",
    )
    pitch: Optional[float] = Field(None, description="Pitch adjustment (Sarvam only)")
    style_prompt: Optional[str] = Field(
        None,
        description=(
            "Natural-language style instruction for Gemini TTS "
            "(e.g. 'Speak in a warm, enthusiastic tone'). Ignored by other providers."
        ),
    )
    enable_ssml_parsing: Optional[bool] = Field(
        None,
        description=(
            "ElevenLabs only. When true, the websocket parses SSML tags such as "
            '<break time="0.5s" /> so they create real pauses instead of being read '
            "aloud. Leave unset/false for plain text. Ignored by other providers."
        ),
    )


class TTSSelectionConfig(BaseModel):
    """Configuration for LLM-based TTS provider selection from payload.

    When enabled, uses Gemini to analyze the lead payload and select
    the optimal TTS provider based on rules defined in the prompt.

    Example:
        {
            "enabled": true,
            "prompt": "Based on the customer's address and region, decide the TTS provider.
                       For Hindi and English speaking regions (North India), use 'elevenlabs'.
                       For South Indian regions or if unsure, use 'cartesia'.",
            "providers": ["elevenlabs", "cartesia"]
        }
    """

    enabled: bool = False
    prompt: str = Field(
        ...,
        description="Prompt template for Gemini to decide which TTS provider to use. "
        "The payload will be appended to this prompt automatically.",
    )
    providers: List[TTSProvider] = Field(
        ...,
        min_length=1,
        description="Allowed TTS providers the LLM can choose from.",
    )


class BackgroundSoundFile(str, Enum):
    """Enum for available background sound files"""

    OFFICE_AMBIENCE = "office-ambience"


class FillerSoundtrack(str, Enum):
    """Pre-registered soundtrack files available for filler background music.

    Users pass one of these enum values instead of a raw filename.
    The audio file mapping lives in utils/audio_mixer.py.
    """

    TYPING = "typing"  # typing_music_realistic_{8k,24k}.mp3
    DIAL_TONE = "dial-tone"  # dial-tone_{8k,24k}.wav
    ON_HOLD_RINGTONE = "on-hold-ringtone"  # on-hold-ringtone_{8k,24k}.mp3


class KeywordMatchType(str, Enum):
    """Match strategy for keyword filtering."""

    EXACT = "exact"  # Transcription must equal the keyword (case-insensitive, trimmed)
    INCLUDES = "includes"  # Transcription must contain the keyword (case-insensitive)


class WakePhraseConfig(BaseModel):
    """Require a wake phrase before the bot responds.

    Wraps pipecat's WakePhraseUserTurnStartStrategy. Placed first in start
    strategies so no other strategy evaluates until the phrase is heard.

    Example::

        {"enabled": true, "phrases": ["yes", "haan"], "single_activation": true}
    """

    enabled: bool = False
    phrases: List[str] = Field(default_factory=list, min_length=1)
    timeout: float = Field(
        10.0, ge=0.0, le=300.0, description="Seconds to stay awake after phrase."
    )
    single_activation: bool = Field(
        False,
        description="If true, wake phrase required only once per session; if false, required before every turn.",
    )

    @model_validator(mode="after")
    def validate_phrases_when_enabled(self):
        if self.enabled and not self.phrases:
            raise ValueError(
                "phrases must contain at least one item when enabled is true"
            )
        return self


class KeywordFilterConfig(BaseModel):
    """Configuration for filtering out specific transcriptions during bot activity.

    When the bot is actively speaking (TTS) or the LLM is processing a response,
    user speech matching these keywords is silently dropped — it is neither forwarded
    to the LLM nor treated as an interruption.

    Example:
        {
            "enabled": true,
            "keywords": ["hello", "yes", "okay"],
            "match_type": "exact"
        }
    """

    enabled: bool = False
    keywords: List[str] = Field(
        default_factory=list,
        description="Keywords to filter out while bot is active.",
    )
    match_type: KeywordMatchType = Field(
        KeywordMatchType.EXACT,
        description="Whether the transcription must exactly equal or just contain a keyword.",
    )


class InterruptionMode(str, Enum):
    """Interruption handling modes for template or node-level control.

    ENABLED: Default. User can interrupt the bot at any time while it's speaking.
    DISABLED_DISCARD: User cannot interrupt. Any speech during bot's turn is discarded.
    """

    ENABLED = "enabled"
    DISABLED_DISCARD = "disabled_discard"


class InterruptionConfig(BaseModel):
    """Configuration for interruption handling at template or node level.

    Controls how user speech is handled while the bot is speaking.

    Examples:
        Default (interruptions on):
            {"mode": "enabled"}

        No interruptions, discard speech:
            {"mode": "disabled_discard"}

        Interruptions with minimum word threshold:
            {"mode": "enabled", "min_words": 3}
    """

    mode: InterruptionMode = Field(
        InterruptionMode.ENABLED,
        description="Interruption mode: 'enabled' (default) or 'disabled_discard'",
    )
    min_words: Optional[int] = Field(
        None,
        ge=1,
        description="Minimum words user must speak to trigger interruption. "
        "Only applies when mode='enabled'. Prevents accidental interruptions "
        "from short utterances like 'hmm' or 'ok'.",
    )


class InputCollectionConfig(BaseModel):
    """Configuration for multi-segment input collection at node level.

    When enabled on a node, increases the user_speech_timeout so that multiple
    speech segments separated by natural pauses are accumulated into a single
    user turn before triggering the LLM. This is essential for nodes where
    users dictate sequences (phone numbers, addresses, account numbers) with
    pauses between segments.

    Without this, each pause triggers a Soniox endpoint → finalized transcript
    → immediate turn end → premature LLM response ("I got 3 digits, what about
    the rest?"). With input collection, the turn stays open for
    user_speech_timeout seconds after each segment, accumulating all segments
    into one LLM message.

    Examples:
        Phone number collection (wait 3s between segments):
            {"enabled": true, "user_speech_timeout": 3.0}

        Address dictation (wait longer):
            {"enabled": true, "user_speech_timeout": 4.0}
    """

    enabled: bool = Field(
        False,
        description="Whether input collection mode is active for this node.",
    )
    user_speech_timeout: float = Field(
        0.0,
        ge=0.0,
        description="Seconds to wait after the last finalized transcript before "
        "ending the user's turn. Higher values allow more natural pauses between "
        "segments. In no-VAD mode (production), the timer resets on each new "
        "transcript, so segments within this window are accumulated into one turn. "
        "Total silence before bot responds = Soniox max_endpoint_delay_ms + this value.",
    )


class UserIdleHandlingConfig(BaseModel):
    """Configuration for user idle detection and handling."""

    enabled: bool = False
    timeout: float = Field(
        5.0,
        ge=0.0,
        description="User idle detection timeout in seconds. After this period of silence, the system will prompt the user.",
    )
    idle_message: str = Field(
        "The user has been quiet for a while. Ask if they are still there and re-engage them in the conversation.",
        description="System message to prompt LLM when user is idle.",
    )
    max_retries: int = Field(
        3,
        ge=1,
        description="Maximum number of idle timeout events before ending the call with 'busy' outcome. The user receives at most max_retries prompts. The call ends on the (max_retries+1)th event.",
    )


class PhrasingOrder(str, Enum):
    """Order in which filler phrases are selected.

    RANDOM: Pick a random phrase from the list on each invocation.
    SEQUENTIAL: Always use the first phrase in the list (stateless/fixed).
                Agent processes are stateless across calls so true round-robin
                is not possible without external state. Use RANDOM for variety.
    """

    SEQUENTIAL = "sequential"
    RANDOM = "random"


class FillerPhraseConfig(BaseModel):
    """Configuration for TTS filler phrases spoken during function call execution."""

    phrases: List[str] = Field(
        ...,
        min_length=1,
        description="List of phrases to speak. One is spoken before the handler runs.",
    )
    phrasing_order: PhrasingOrder = Field(
        PhrasingOrder.RANDOM,
        description="How to pick phrases: 'random' (default) or 'sequential'.",
    )


class FillerBackgroundMusicConfig(BaseModel):
    """Configuration for background music played during function call execution."""

    sound_file: Optional[FillerSoundtrack] = Field(
        None,
        description="Soundtrack to play. Choose from the FillerSoundtrack enum "
        "(e.g., 'typing', 'dial-tone'). None disables background music.",
    )
    volume: float = Field(
        0.4,
        ge=0.0,
        le=1.0,
        description="Mixer volume (0.0–1.0).",
    )


class FillerAudioConfig(BaseModel):
    """Filler audio played while a global function call is executing.

    Set either or both configs — they are independent and can run together:
    - filler_phrase_config: speaks a TTS phrase before the handler runs.
    - background_music_config: loops background music via SoundfileMixer
      while the handler runs, then stops when done.

    At least one config must be provided.

    Examples:
        Phrase only::

            {"filler_phrase_config": {"phrases": ["One moment...", "Let me check..."]}}

        Music only::

            {"background_music_config": {"sound_file": "typing", "volume": 0.4}}

        Both (phrase spoken first, music starts after phrase ends)::

            {"filler_phrase_config": {"phrases": ["Let me check that for you..."]},
             "background_music_config": {"sound_file": "typing", "volume": 0.3}}
    """

    background_music_config: Optional[FillerBackgroundMusicConfig] = None
    filler_phrase_config: Optional[FillerPhraseConfig] = None

    @model_validator(mode="after")
    def _validate_config(self) -> "FillerAudioConfig":
        has_music = (
            self.background_music_config is not None
            and self.background_music_config.sound_file is not None
        )
        has_phrases = self.filler_phrase_config is not None and bool(
            self.filler_phrase_config.phrases
        )
        if not has_music and not has_phrases:
            raise ValueError(
                "FillerAudioConfig requires at least one of: "
                "background_music_config (with sound_file set), filler_phrase_config (with phrases)"
            )
        return self


class QuickReplyOption(BaseModel):
    """One quick-reply chicklet shown to the user on widget open.

    ``label`` is the button text displayed in the UI and echoed as the
    user's chat bubble. ``value`` is the text sent to the backend; when
    omitted it falls back to ``label``, which lets the display text differ
    from the agent instruction (e.g. label="Track my order" while the
    backend receives a more explicit instruction).

    Set ``action`` to override the click behavior. With an ``open_url``
    action the chicklet **redirects** (opens the URL, honoring ``target``)
    instead of messaging the agent; ``value`` is then optional and the
    label fallback is not applied.
    """

    label: str = Field(..., min_length=1, description="Button text shown to the user.")
    value: Optional[str] = Field(
        None,
        description=(
            "Payload sent to the backend. Defaults to label when absent "
            "the fallback is applied server-side before the value reaches the client. "
            "Ignored when ``action`` is an ``open_url`` redirect."
        ),
    )
    action: Optional[ActionUnion] = Field(
        None,
        description=(
            "Optional click action. When omitted the chicklet sends ``value`` "
            "to the agent (default). An ``open_url`` action makes it redirect "
            "instead of messaging the agent."
        ),
    )
    icon: Optional[Icon] = Field(
        None,
        description=(
            "Optional icon shown alongside the label. Independent of "
            "``value``/``action`` — applies to both message and redirect "
            "chicklets."
        ),
    )


class HoldTransferConfig(BaseModel):
    """Configuration for hold & consultative transfer.

    When the AI agent places the inbound caller on hold and makes an outbound
    call to a third party, this config tells the handler which outbound number
    (and therefore which template) to use for the outbound conversation.

    Example:
        {
            "outbound_number_id": "uuid-of-outbound-number",
            "hold_music": "typing",
            "hold_timeout_seconds": 180,
            "summarize": true
        }
    """

    outbound_number_id: str = Field(
        ...,
        description="Outbound number ID used to make the outbound call. "
        "The template associated with this outbound_number_id "
        "will be used for the outbound conversation.",
    )
    hold_music: FillerSoundtrack = Field(
        FillerSoundtrack.ON_HOLD_RINGTONE,
        description="Hold music soundtrack played while the inbound caller waits.",
    )
    hold_timeout_seconds: int = Field(
        180,
        ge=30,
        le=600,
        description="Max seconds the inbound caller stays on hold. Default 3 min.",
    )
    phone_number: Optional[str] = Field(
        None,
        description="Phone number to call. If set, the handler uses this "
        "instead of the LLM-provided phone_number argument. "
        "Omit to let the LLM provide the number at runtime.",
    )
    summarize: bool = Field(
        False,
        description="When true, summarize the outbound transcription via LLM "
        "before sending to the inbound pod. When false, send raw transcription.",
    )
    hold_music_volume: float = Field(
        0.4,
        ge=0.0,
        le=1.0,
        description="Volume for hold music playback (0.0–1.0). Default 0.4.",
    )


class IvrConfig(BaseModel):
    """IVR-specific configuration — voice, greeting, goodbye, priority.

    When a template is part of an inbound IVR menu, these settings control
    the IVR experience. tts_configuration overrides the main tts_configuration for
    IVR audio only (greeting, goodbye, block messages).

    Example:
        {
            "tts_configuration": {"provider": "sarvam", "language": "hi"},
            "greeting": "Welcome. Press 1 for billing, press 2 for support.",
            "goodbye": "We didn't receive your input. Goodbye.",
            "priority": 1
        }
    """

    tts_configuration: Optional[TTSConfig] = Field(
        None,
        description="TTS configuration for IVR audio. Falls back to the template's main tts_configuration if not set.",
    )
    greeting: Optional[str] = Field(
        None,
        description="Full IVR audio text including greeting and menu options.",
    )
    goodbye: Optional[str] = Field(
        None,
        description="Goodbye message when no input received.",
    )
    priority: Optional[int] = Field(
        None,
        ge=1,
        description="Priority order for IVR menu (lower = earlier). Gaps allowed.",
    )


class ResponseTransform(BaseModel):
    """One in-place response-transform rule declared in a template.

    Sibling to ``expected_response_schema`` (projection): where projection
    reshapes the response into a new dict, transforms mutate specific paths
    and pass the rest through. Both are applied inside the tool handler
    closure, so voice + chat + any future channel benefit uniformly.

    Runtime registry + walker + the first built-in (``scale_by_exponent``)
    live in ``handlers/transport/utils/response_transform.py``.
    """

    path: str = Field(
        ...,
        description=(
            "Dotted path to the field(s) to transform. "
            "Use [*] to iterate array elements. "
            "Examples: 'order.total', 'products[*].price_range.min', 'items[*]'."
        ),
    )
    fn: str = Field(
        ...,
        description=(
            "Registered transform name "
            "(see handlers/transport/utils/response_transform.py)."
        ),
    )
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional arguments forwarded to the transform fn.",
    )


class ToolUiTrigger(str, Enum):
    """When the JIT UI hint should ship with this tool's response.

    * ``on_success`` — append only when the tool returns success.
    * ``on_any`` — append regardless of status (errors too).
    * ``skip_ui`` — never emit UI for this tool's result. Hint payload is
      ignored; only the ``_ui_skip`` flag is appended so the LLM knows.
    """

    ON_SUCCESS = "on_success"
    ON_ANY = "on_any"
    SKIP_UI = "skip_ui"


class ToolUiExample(BaseModel):
    """A short worked example the LLM can crib from when authoring a
    ``<ui_stream>`` block for this tool's result. Optional but recommended
    — Sidekick-style few-shot in the tool response itself."""

    scenario: str = Field(
        ..., description="Short label for the example (e.g. '3 products, INR')."
    )
    input_sketch: Optional[str] = Field(
        None,
        description=(
            "Pseudocode for the shape of the tool result this example "
            "applies to (e.g. '{products: [{id, title, price_range}…]}')."
        ),
    )
    expected_jsonl: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "List of expected SpecStream ops the LLM should emit. Each "
            "entry is a JSON dict with op/id/type/parent/props."
        ),
    )


class ToolUiHint(BaseModel):
    """Per-tool UI authoring guidance — Sidekick-pattern JIT instructions
    that ride along with the tool result instead of bloating the system
    prompt.

    At dispatch time, the matching hint's ``instructions`` (and optional
    ``examples``) are spliced into the tool result envelope under
    ``_ui_instructions`` / ``_ui_examples`` so the LLM sees them as part
    of the tool's payload. The LLM then emits a ``<ui_stream>`` block
    using SpecStream JSONL ops grounded in the data it just received.

    Commerce flavour lives in template JSON; engine + injection point are
    commerce-agnostic.
    """

    trigger: ToolUiTrigger = Field(
        ToolUiTrigger.ON_SUCCESS,
        description="When the hint fires (see ToolUiTrigger).",
    )
    instructions: str = Field(
        "",
        description=(
            "Free-text guidance shown to the LLM with the tool result. "
            "Tell it which primitives to use, what to skip, what tone to "
            "strike. Keep under ~2KB per tool."
        ),
    )
    examples: List[ToolUiExample] = Field(
        default_factory=list,
        description="Optional few-shot examples appended to the hint.",
    )


class McpServerConfig(BaseModel):
    """Configuration for a single MCP tool server.

    The ``url`` field supports ``{variable}`` placeholder substitution using
    ``template_vars`` (derived from the call payload and credentials table).
    This allows dynamic MCP URLs — for example, the Nautilus Shopify app passes
    ``shop_url`` in the lead payload so Clairvoyance can build the full URL at
    call time:

    Example template JSON (no-auth Shopify Storefront MCP via Nautilus)::

        "configurations": {
            "mcp": {
                "servers": [
                    {
                        "enabled": true,
                        "name": "shopify",
                        "url": "https://{shop_url}/api/mcp",
                        "timeout": 30,
                        "auth": {
                            "type": "none"
                        }
                    }
                ]
            }
        }

    Example with credential-based auth (api_key resolved from credentials table)::

        "configurations": {
            "mcp": {
                "servers": [
                    {
                        "enabled": true,
                        "name": "shopify-storefront",
                        "url": "https://{shop_url}/api/mcp",
                        "timeout": 30,
                        "auth": {
                            "type": "api_key",
                            "api_key_name": "X-Shopify-Storefront-Access-Token",
                            "api_key_value": "{shopify_storefront_token}"
                        }
                    }
                ]
            }
        }

    Auth types:
    - ``none``: No authentication — call the MCP server directly.
    - ``api_key``: Pass ``api_key_value`` as an HTTP header named ``api_key_name``.
      Use ``{credential_name}`` placeholders; the value is resolved from the
      credentials table (loaded into ``template_vars`` before MCP setup).
    - ``bearer``: Pass ``token`` as ``Authorization: Bearer <token>``.
    - ``basic``: Pass ``username`` / ``password`` as HTTP Basic auth.
    """

    enabled: bool = Field(True, description="Whether to enable this MCP server")
    name: Optional[str] = Field(
        None,
        description="Optional name for this server, used as tool prefix on collisions",
    )
    url: str = Field(..., description="Full MCP server URL")
    timeout: int = Field(
        30,
        ge=1,
        le=120,
        description="Timeout in seconds for MCP server connections and tool calls",
    )
    auth: Optional["HttpAuthConfig"] = Field(
        None, description="Optional auth config for this MCP server"
    )
    headers: Dict[str, str] = Field(
        default_factory=dict, description="Additional static headers to send"
    )
    tool_response_transforms: Dict[str, List["ResponseTransform"]] = Field(
        default_factory=dict,
        description=(
            "Per-tool in-place response transforms applied after the MCP tool "
            "call returns, before the result is fed to the LLM. Keys are the "
            "raw MCP tool names (e.g. 'search_catalog'); values are lists of "
            "ResponseTransform rules. Channel-agnostic — applied inside the "
            "tool handler closure, so voice + chat + any future channel "
            "benefit uniformly. Use the registered transform names from "
            "handlers/transport/utils/response_transform.py."
        ),
    )
    tool_ui_instructions: Dict[str, "ToolUiHint"] = Field(
        default_factory=dict,
        description=(
            "Per-tool JIT UI authoring guidance (Sidekick pattern). Keys "
            "are raw MCP tool names; values declare what UI the LLM "
            "should emit for that tool's result. Injected into the tool "
            "response envelope under '_ui_instructions' / '_ui_examples' "
            "/ '_ui_skip' before the LLM sees it, so per-tool guidance "
            "doesn't bloat the system prompt."
        ),
    )
    tool_context_retention: Dict[str, Literal["last_turn_only", "session"]] = Field(
        default_factory=dict,
        description=(
            "Per-tool conversation-context retention policy, applied at "
            "message-prep time before the LLM call. Keys are the registered "
            "tool names: the raw upstream MCP name, except a tool whose name "
            "collides across multiple servers is registered as "
            "``<server.name>_<name>`` and must be keyed by that prefixed name "
            "(single-server templates never collide, so keys are raw). "
            "Values are:\n"
            "  - ``last_turn_only``: the tool_result content is replaced "
            "    with a 1-line stub once it's no longer the most-recent "
            "    tool_result in history. Forces the agent to re-call the "
            "    tool for grounding rather than relying on stale memory. "
            "    Best for high-token / fast-changing data (catalog "
            "    searches, lookups).\n"
            "  - ``session``: tool_result is kept full for the whole "
            "    session. Best for state-bearing tools (cart calls — "
            "    line_items + totals shape later turns).\n"
            "Tools not listed default to ``session`` (current behavior)."
        ),
    )
    tool_context_projection: Dict[str, List[str]] = Field(
        default_factory=dict,
        description=(
            "Per-tool identity keep-list, paired with ``last_turn_only`` "
            "retention. Keys follow the same rule as ``tool_context_retention`` "
            "(raw upstream MCP tool name; ``<server.name>_<name>`` only when "
            "the name collides across servers). Values are lists of "
            "keep-paths (same grammar as tool_response_transforms: ``a.b`` "
            "descends keys, ``a[*].b`` iterates a list at ``a``). When a "
            "``last_turn_only`` tool has a keep-list, its stale tool_result is "
            "compacted to ONLY those paths (an identity projection) instead of "
            "a 1-line stub — so durable referents (product url/handle/price, "
            "variant ids) survive across turns at ~1% of the original tokens, "
            "while heavy fields (descriptions, media, full variant blobs) are "
            "dropped. The most-recent result is always kept full; only older "
            "ones are projected. Tools without a keep-list fall back to the "
            "stub. Engine-generic — the field list is the only domain-specific "
            "part and it lives here in the template."
        ),
    )
    default_args: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Static arguments deep-merged into every tool call's `arguments` "
            "object before the call is dispatched. Caller-provided values win "
            "(only_if_missing semantics). Used for protocol-level metadata "
            "that every tool on this server needs — e.g. Shopify UCP requires "
            "`meta.ucp-agent.profile` to be present on every tools/call. "
            "Channel-agnostic; applied inside the handler closure for both "
            "voice and chat."
        ),
    )
    tool_approvals: Dict[str, "ApprovalConfig"] = Field(
        default_factory=dict,
        description=(
            "Per-tool human-in-the-loop approval gates. Keys are the RAW "
            "upstream MCP tool names (e.g. 'create_checkout'); values are "
            "ApprovalConfig. When an entry exists, a call to that tool pauses "
            "for an explicit end-user decision before it executes: chat ends "
            "the turn with a function_approval_requested event and resumes on "
            "the approval endpoint; Daily voice blocks in-process on the RTVI "
            "approval channel (Pattern C). Same keying rule as the other "
            "per-tool maps — author against the raw upstream name; a tool "
            "whose name collides across servers is gated under its registered "
            "``<server.name>_<name>`` form. Telephony has no approval surface "
            "(see ApprovalConfig.on_no_channel, default 'execute'); set it to "
            "'deny' for high-stakes write tools (refunds, checkout)."
        ),
    )
    tool_schemas: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "When set, skip MCP discovery (initialize + tools/list) entirely "
            "and register the declared schemas instead. Tool calls go via a "
            "direct JSON-RPC HTTP POST to `url`, bypassing pipecat's MCPClient "
            "session. Required for Shopify UCP, which rejects `initialize` "
            "and `tools/list` without an agent profile we can only attach to "
            "individual `tools/call` requests. Each entry: "
            "`{name, description, properties, required}`."
        ),
    )


class McpConfig(BaseModel):
    """Top-level MCP configuration holding a list of MCP servers."""

    servers: List["McpServerConfig"] = Field(
        default_factory=list, description="List of MCP servers to connect to"
    )


class UiCatalogConfig(BaseModel):
    """Per-template selection of which generative-UI primitives the LLM
    may emit and the widget will render.

    Resolution at session start (see ``ui_catalog.resolve_allowlist``):
      1. Start with every primitive in ``enabled_groups``.
      2. Union in any ``enabled_primitives`` one-offs.
      3. Subtract ``disabled_primitives``.

    The resolved allowlist drives three things:
      * Server-side ``ui_op`` validation — disabled types drop with
        reason ``primitive_disabled:<type>`` (distinct from
        ``unknown_type`` so telemetry tells the difference).
      * The auto-rendered "## Available primitives" section spliced
        into the system prompt — the LLM never even sees disabled
        primitives, so it can't accidentally emit them.
      * The widget's render path (no change — widget compiles in all
        primitives; server filtering is the gate).

    Example::

        "configurations": {
            "ui_catalog": {
                "enabled_groups": ["core", "composite"],
                "enabled_primitives": [],
                "disabled_primitives": []
            }
        }

    Default behaviour (config absent): ``enabled_groups = ["core"]`` so
    pre-Tile templates keep working unchanged.
    """

    enabled_groups: List[str] = Field(
        default_factory=lambda: ["core"],
        description=(
            "Primitive groups the LLM may emit. Known groups: 'core', "
            "'composite', 'graphs', 'metrics', 'forms', 'media', 'data'. "
            "Unknown groups are silently ignored."
        ),
    )
    enabled_primitives: List[str] = Field(
        default_factory=list,
        description=(
            "Explicit one-off primitives to enable beyond what the groups "
            "provide. Useful for trying a single new primitive without "
            "enabling its whole group."
        ),
    )
    disabled_primitives: List[str] = Field(
        default_factory=list,
        description=(
            "Explicit overrides to remove from the resolved set, e.g. "
            "disable 'Table' on a voice-first template even though it's "
            "in the 'core' group."
        ),
    )


class StateReducer(BaseModel):
    """One per-tool rule that maps a tool result onto session state.

    Generic — Python runtime knows nothing about which keys mean what.
    Each rule names one tool and a set of (state_key → JMESPath) pairs;
    when that tool returns, every matching JMESPath is evaluated against
    the parsed tool payload and the result is merged into the session's
    ``data`` dict (see migration 030 / agent_session_state).

    Commerce flavour lives in the template JSON, e.g.::

        "state_reducers": [
            {"tool_name": "update_cart",
             "set_paths": {"cart_id": "cart.id",
                           "checkout_url": "cart.checkout_url"}}
        ]

    A future flavour (travel, tickets) ships different reducers — same
    engine, same schema.
    """

    tool_name: str = Field(
        ...,
        description="Raw MCP tool name (or HTTP function name) the reducer fires on.",
    )
    set_paths: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of state_key → JMESPath expression evaluated against the "
            "parsed tool payload. Missing matches / lookup failures are "
            "silently skipped so a stale rule never crashes a turn."
        ),
    )
    only_on_success: bool = Field(
        True,
        description=(
            "When true (default), skip the rule if the tool envelope's "
            "status indicates an error — keeps state out of the way of "
            "obviously-failed calls."
        ),
    )


class ToolArgInjection(BaseModel):
    """One per-tool rule that injects session state into outgoing tool args.

    The companion to :class:`StateReducer` — where the reducer captures
    server-side identifiers off tool results, this rule reads them back
    out of session state and stamps them onto the next tool's arguments.
    The LLM never has to thread the identifier through prose.

    Two sources are supported, both commerce-agnostic:

    - ``set_paths`` — read from context (state / args / session_id) via
      JMESPath. Used for things the runtime already knows (e.g. a cart
      id captured by a reducer on the previous turn).
    - ``generators`` — produce a value at call time (uuid_v4, uuid_v7,
      timestamp_iso8601, timestamp_unix_ms). Used for idempotency keys,
      request ids, client-side timestamps — values the LLM shouldn't be
      asked to invent. Any MCP server that follows the Stripe-style
      Idempotency-Key convention can re-use this without code changes.

    Default behaviour (``only_if_missing=True``) is to fill ONLY when the
    LLM didn't pass the field — explicit LLM intent always wins.

    Commerce flavour in template JSON::

        "tool_arg_injection": [
            {"tool_name": "update_cart",
             "set_paths": {"cart_id": "state.data.cart_id"},
             "generators": {"idempotency_key": "uuid_v4"}}
        ]
    """

    tool_name: str = Field(
        ...,
        description="Raw MCP tool name (or HTTP function name) the rule fires on.",
    )
    set_paths: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of arg_key → JMESPath expression evaluated against the "
            "context ``{session_id, state.data, args}``. Use "
            "`state.data.cart_id` for state lookups, `args.foo` to "
            "post-process an LLM-provided arg, or `session_id` for the "
            "raw chat session id."
        ),
    )
    generators: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of arg_key → generator name. Generator produces a value "
            "at call time. Supported: 'uuid_v4', 'uuid_v7', "
            "'timestamp_iso8601', 'timestamp_unix_ms'. Honours "
            "``only_if_missing`` like ``set_paths``."
        ),
    )
    only_if_missing: bool = Field(
        True,
        description=(
            "When true (default), skip the rule if the LLM already "
            "passed a non-null value for the arg key — the LLM's "
            "explicit intent wins. Set to false to force-override."
        ),
    )


class ClientContextConfig(BaseModel):
    """Per-template policy for client-pushed context updates.

    The storefront embed can push context mid-session via
    ``POST /widget/session/{id}/context`` (or the piggyback
    ``SendChatMessageRequest.context`` field) — no LLM turn spent. Two
    kinds, routed to the two existing sinks:

    - **state** — identifiers/flags merged into the top level of
      ``agent_session_state.data``, where the existing
      :class:`ToolArgInjection` rules thread them into outgoing tool args
      (e.g. a ``cart_id`` created client-side). Only keys in
      ``state_allowlist`` are accepted.
    - **facts** — ambient facts the LLM reasons over (offers, cart
      summary, current page). Merged into the reserved
      ``agent_session_state.data['_client_context']`` namespace and
      rendered each turn as a delimited context block. Only keys in
      ``facts_allowlist`` are accepted; the serialised namespace is
      capped at ``max_bytes``.

    Client data is **untrusted** — storefront JS is shopper-editable.
    Facts render as ``user``-role data by default
    (``facts_placement='user_tail'``): information to consider, never
    instructions. A merchant may opt specific, curated keys into
    stronger ``system``-role adherence via ``facts_placement='system'``
    + ``trusted_facts``; keys NOT in ``trusted_facts`` always render
    ``user_tail`` regardless, so shopper-supplied data can never be
    elevated to instruction status. See docs/widget/CLIENT_CONTEXT_UPDATES.md.

    Defaults are strict opt-in: with empty allowlists nothing is
    accepted, so the feature is inert until a template enables it.
    """

    state_allowlist: List[str] = Field(
        default_factory=list,
        description=(
            "Keys the client may write via `state`. Merged into the top "
            "level of agent_session_state.data (read by tool_arg_injection "
            "rules). Empty = reject all state writes."
        ),
    )
    facts_allowlist: List[str] = Field(
        default_factory=list,
        description=(
            "Top-level keys the client may write via `facts`. Non-listed "
            "keys are dropped. Empty = reject all facts."
        ),
    )
    max_bytes: int = Field(
        4096,
        ge=0,
        description=(
            "Hard cap on the serialised facts namespace so a buggy theme "
            "can't blow the context window. Over-cap writes are rejected "
            "with HTTP 413."
        ),
    )
    render: bool = Field(
        True,
        description=(
            "Whether the facts namespace is rendered into the turn at all. "
            "False = state/tool-arg injection only, no prompt block."
        ),
    )
    facts_placement: Literal["user_tail", "system"] = Field(
        "user_tail",
        description=(
            "Default landing for rendered facts. 'user_tail' (default) = a "
            "user-role data block in the volatile tail (cache-friendly, "
            "injection-safe). 'system' = an appended system-role block for "
            "instruction-strength adherence; only keys in `trusted_facts` "
            "are elevated, everything else stays user_tail. Knowingly trades "
            "prompt-prefix cache on Anthropic/Gemini."
        ),
    )
    trusted_facts: List[str] = Field(
        default_factory=list,
        description=(
            "Subset of `facts_allowlist` permitted to occupy `system` "
            "placement. Only merchant-curated keys belong here — never "
            "shopper-supplied ones. Empty = nothing may be elevated to "
            "instructions."
        ),
    )


class ConfigurationModel(BaseModel):
    # --- Agent session state (generic) ---
    state_reducers: List[StateReducer] = Field(
        default_factory=list,
        description=(
            "Declarative rules that lift identifiers from tool results "
            "into a per-session `agent_session_state.data` JSONB. "
            "Generic engine — the template decides which keys matter."
        ),
    )
    tool_arg_injection: List[ToolArgInjection] = Field(
        default_factory=list,
        description=(
            "Declarative rules that stamp session-state values onto "
            "outgoing MCP tool arguments (e.g. cart_id from prior turn). "
            "Generic engine — the template decides which args matter."
        ),
    )
    client_context: Optional["ClientContextConfig"] = Field(
        None,
        description=(
            "Optional. Policy for client-pushed context updates (the "
            "storefront /widget/session/{id}/context channel). Allowlists "
            "which state/facts keys the embed may write, caps facts size, "
            "and chooses how facts render (data vs. instructions). Absent / "
            "empty allowlists = feature inert."
        ),
    )

    # --- Generative-UI primitive selection (per-template, per-merchant) ---
    ui_catalog: Optional["UiCatalogConfig"] = Field(
        None,
        description=(
            "Optional. Selects which UI primitives the LLM may emit and "
            "the widget will render. Resolution + rendering happens at "
            "session start; the resolved allowlist drives both server "
            "validation and the system-prompt 'Available primitives' "
            "section. When absent, defaults to enabling the 'core' group."
        ),
    )

    # --- STT (provider + turn detection) ---
    stt_configuration: Optional[STTConfiguration] = Field(
        None,
        description="STT provider, language, turn detection, and provider-specific "
        "config. When set, takes priority over legacy stt_language / soniox_context fields.",
    )

    # --- Legacy STT fields (backward compat — prefer stt_configuration) ---
    stt_language: Optional[str | list[str]] = Field(
        None,
        description="DEPRECATED: Use stt_configuration.language instead.",
    )
    soniox_context: Optional[str] = Field(
        None,
        description="DEPRECATED: Use stt_configuration.soniox.context instead.",
    )
    payload_based_language_selection: bool = Field(
        False,
        description="DEPRECATED: Use stt_configuration.payload_based_language_selection instead.",
    )

    # --- TTS ---
    tts_configuration: Optional[TTSConfig] = None  # Unified TTS configuration
    tts_configuration_overrides: Optional[Dict[str, TTSConfig]] = Field(
        None,
        description="Per-provider TTS overrides keyed by provider name. "
        "Provider is auto-filled from the key — no need to repeat it. "
        'E.g. {"elevenlabs": {"voice_id": "...", "speed": 1.0}}',
    )

    @model_validator(mode="before")
    @classmethod
    def _pre_validate(cls, data: Any) -> Any:
        """Pre-validation: auto-fill override providers + migrate flat IVR fields."""
        if not isinstance(data, dict):
            return data

        # Auto-set provider from dict key in tts_configuration_overrides
        for key, cfg in (data.get("tts_configuration_overrides") or {}).items():
            if isinstance(cfg, dict):
                cfg["provider"] = key

        # Migrate deprecated flat ivr_* fields into ivr_configuration
        if not data.get("ivr_configuration"):
            ivr = {
                dst: v
                for src, dst in (
                    ("ivr_greeting", "greeting"),
                    ("ivr_goodbye", "goodbye"),
                    ("ivr_priority", "priority"),
                )
                if (v := data.get(src)) is not None
            }
            if ivr:
                data["ivr_configuration"] = ivr
                # Log deprecation warnings for each migrated field
                for old_field in ("ivr_greeting", "ivr_goodbye", "ivr_priority"):
                    if data.get(old_field) is not None:
                        new_field = old_field.replace("ivr_", "")
                        logger.warning(
                            f"[Deprecated] field '{old_field}' is set. "
                            f"Use 'ivr_configuration.{new_field}' instead."
                        )

        return data

    tts_selection_config: Optional[TTSSelectionConfig] = (
        None  # LLM-based TTS provider selection config
    )

    # --- Audio ---
    enable_background_sound: bool = False
    background_sound_file: Optional[BackgroundSoundFile] = None
    background_sound_volume: float = 2.0

    initial_greeting: Optional[str] = (
        None  # Initial greeting text template with variables (e.g., "Hi {customer_name}")
    )
    quick_replies: List[QuickReplyOption] = Field(
        default_factory=list,
        description=(
            "Quick-reply chicklet buttons shown to the user on widget open. "
            "Empty list = no chicklets, normal composer shown."
        ),
    )
    enable_text_input: bool = Field(
        True,
        description=(
            "Whether the free-text composer is shown. "
            "True (default) = composer visible; user can type freely. "
            "False = composer hidden for all turns; only quick replies or "
            "agent-driven input is possible."
        ),
    )
    ivr_configuration: Optional[IvrConfig] = None  # IVR-specific configuration
    # DEPRECATED: Use ivr_configuration.greeting / ivr_configuration.goodbye / ivr_configuration.priority
    ivr_greeting: Optional[str] = None
    ivr_goodbye: Optional[str] = None
    ivr_priority: Optional[int] = Field(None, ge=1)
    transfer_number: Optional[str] = Field(
        None, description="Phone number to transfer the call to"
    )
    enable_mpc_transfer: bool = Field(
        False,
        description=(
            "Warm-transfer transport selector (Plivo only). "
            "False (default, legacy): the customer's live leg is bridged to the "
            "agent immediately via the native call-transfer API; the bot's "
            "WebSocket closes right away, the call does not wait for the agent "
            "to answer, and nothing is returned to the LLM (transfer_to_agent is "
            "terminal). This matches templates authored before dial-first and "
            "incurs no MultiPartyCall cost. "
            "True: dial-first MultiPartyCall (MPC) — the agent is dialled first "
            "while the customer stays on the bot; if the agent does not answer "
            "the call resumes with the AI. Requires the template prompt to "
            "handle a resumed conversation, and an LLM function-call timeout "
            ">= ~40s (enforced automatically for the transfer builtin). "
            "Twilio and Exotel ignore this flag and keep their existing behavior."
        ),
    )
    vad_config: Optional[VadConfig] = Field(
        None, description="Default VAD configuration for the template"
    )
    enable_inbound: bool = False  # Whether this template can handle inbound calls
    user_idle_configuration: Optional[UserIdleHandlingConfig] = (
        None  # User idle handling config
    )
    noise_filter: Optional[NoiseFilterConfig] = Field(
        None, description="Noise filter configuration for audio input processing"
    )
    keyword_filter: Optional[KeywordFilterConfig] = Field(
        None,
        description="Keyword filter to suppress specific transcriptions while bot is active",
    )
    wake_phrase: Optional[WakePhraseConfig] = Field(
        None,
        description="Wake phrase config — bot only responds after hearing a trigger phrase",
    )
    mcp: Optional[McpConfig] = Field(
        None,
        description="MCP tool server configuration for dynamic tool discovery",
    )
    interruption: Optional[InterruptionConfig] = Field(
        None,
        description="Interruption handling configuration (mode, min_words threshold)",
    )
    llm_configurations: Optional[LLMConfiguration] = Field(
        None,
        description="LLM provider and model configuration",
    )
    evaluator_config: Optional[List[str]] = Field(
        None,
        description="List of LLM-as-judge evaluator names to run for this template. "
        "Each name is added as a tag on the Langfuse trace. "
        "If empty/None, 'ALL_EVALS' tag is added instead.",
    )
    hold_transfer: Optional[HoldTransferConfig] = Field(
        None,
        description="Hold & consultative transfer configuration. "
        "When set, enables the hold_and_consult builtin handler.",
    )

    @model_validator(mode="after")
    def _backfill_legacy_from_stt_config(self):
        """Mirror stt_configuration values to legacy fields for backward compat.

        Legacy consumers (flow.py, language_detector.py) read top-level
        stt_language / payload_based_language_selection. When stt_configuration
        is set but legacy fields are not explicitly provided, backfill so
        those consumers keep working.
        """
        if self.stt_configuration is None:
            return self
        stt = self.stt_configuration
        if "stt_language" not in self.model_fields_set and stt.language is not None:
            self.stt_language = stt.language
        if (
            "soniox_context" not in self.model_fields_set
            and stt.soniox is not None
            and stt.soniox.context is not None
        ):
            self.soniox_context = stt.soniox.context
        if (
            "payload_based_language_selection" not in self.model_fields_set
            and stt.payload_based_language_selection
        ):
            self.payload_based_language_selection = stt.payload_based_language_selection
        return self

    @model_validator(mode="after")
    def _warn_deprecated_fields(self):
        log_deprecated_fields(
            self,
            {
                "stt_language": "stt_configuration.language",
                "soniox_context": "stt_configuration.soniox.context",
                "payload_based_language_selection": "stt_configuration.payload_based_language_selection",
                "mira_voice_id": "cartesia_voice_configurations.voice_id",
            },
        )
        return self


class FlowAction(BaseModel):
    type: ActionType
    text: Optional[str] = None
    handler: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


class TaskMessage(BaseModel):
    role: str
    content: str


class FieldSource(str, Enum):
    """Source types for field value resolution.

    Used by both hooks (fire-and-forget) and global HTTP functions (wait for response).
    - STATIC: Value is a literal or contains {template_var} placeholders
    - LLM: Value comes from LLM function arguments
    - COMPUTED: Value is dynamically computed at invocation time (e.g., timestamps)
    """

    STATIC = "static"
    LLM = "llm"
    COMPUTED = "computed"


class HttpMethod(str, Enum):
    """HTTP methods supported for external API calls"""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class HttpAuthType(str, Enum):
    """Authentication types for HTTP requests"""

    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    API_KEY = "api_key"


class SseResponseMode(str, Enum):
    """How the SSE response is packaged for the LLM."""

    FULL = "full"
    """Send all SSE event data as a single newline-joined string to the LLM."""
    SELECT = "select"
    """Send a single event's data string selected by ``select_index``."""


class SseResponseHandlerConfig(BaseModel):
    """Controls how SSE events are packaged for the LLM payload.

    When ``sse_response_handler`` is ``None`` on ``GlobalHttpFunction``, the
    default behaviour is ``mode=full``: all SSE event data is joined with
    newlines and returned as one string to the LLM.

    Examples::

        # Send all event data concatenated (default when handler is None)
        {"mode": "full"}

        # Send only the last event's data
        {"mode": "select", "select_index": -1}

        # Send only the first event's data
        {"mode": "select", "select_index": 0}
    """

    model_config = ConfigDict(populate_by_name=True)

    mode: SseResponseMode = SseResponseMode.FULL
    select_index: Optional[int] = None

    @model_validator(mode="after")
    def validate_select_index(self) -> "SseResponseHandlerConfig":
        if self.mode == SseResponseMode.SELECT and self.select_index is None:
            raise ValueError("select_index is required when mode='select'")
        return self


class HttpAuthConfig(BaseModel):
    """Authentication configuration for HTTP requests"""

    type: HttpAuthType = HttpAuthType.NONE
    token: Optional[SecretStr] = None  # For bearer auth
    username: Optional[str] = None  # For basic auth
    password: Optional[SecretStr] = None  # For basic auth
    api_key_name: Optional[str] = None  # Header name for API key
    api_key_value: Optional[SecretStr] = None  # API key value

    # Templates are persisted to Postgres as JSON. Pydantic's default
    # JSON serialization for SecretStr produces the masked form
    # ("**********"), which silently corrupts the stored value — the
    # template would then send `Authorization: Bearer **********` at
    # call time. We unmask **only** when an explicit
    # ``context={"reveal_secrets": True}`` flag is passed to model_dump
    # (the create/replace handlers do this on the persistence path).
    # All other JSON serialization paths — notably FastAPI response
    # encoding for GET / PUT — see no context, fall through to the
    # masked form, and so do not leak literal tokens that an operator
    # may have embedded directly (vs. the intended
    # ``{credential_name}`` placeholder pattern).
    @field_serializer("token", "password", "api_key_value", when_used="json")
    def _reveal_secret(
        self, value: Optional[SecretStr], info: SerializationInfo
    ) -> Optional[str]:
        if value is None:
            return None
        if info.context and info.context.get("reveal_secrets"):
            return value.get_secret_value()
        return "**********"


class HttpRequestConfig(BaseModel):
    """Complete HTTP request configuration for hooks and global functions.

    The body field can be:
    - A Dict that will be serialized to JSON
    - A JSON string that will be parsed and have placeholders resolved

    Streaming is auto-detected from the response Content-Type header:
    - ``text/event-stream`` → read SSE line-by-line, forward each event
      to the frontend via RTVI. What the LLM sees is controlled by
      ``sse_response_handler`` on ``GlobalHttpFunction``.
    - Any other Content-Type → standard request/response with retry logic.
    """

    url: str
    method: HttpMethod = HttpMethod.POST
    headers: Dict[str, str] = {}
    query_params: Dict[str, str] = {}
    body: Optional[Dict[str, Any] | str] = None
    auth: Optional[HttpAuthConfig] = None
    timeout: int = 10
    max_retries: int = 3


class FieldConfig(BaseModel):
    """Configuration for a single field in hooks or global HTTP functions.

    Defines how to resolve a field value:
    - source: Where the value comes from (STATIC, LLM, or COMPUTED)
    - value: For STATIC, the literal value or {template_var} placeholder.
             For LLM, the name of the argument from LLM function call.
             For COMPUTED, the function expression (e.g., "utc_now_minus_hours:1").
    """

    source: FieldSource
    value: Optional[Any] = None


class HookConfig(BaseModel):
    """Configuration for a hook with expected fields"""

    name: str
    expected_fields: Dict[str, FieldConfig] = {}
    http_request: Optional[HttpRequestConfig] = None  # For send_http_request hook
    awaited: bool = False


class FlowFunction(BaseModel):
    name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    transition_to: Optional[str] = None
    hooks: List[HookConfig] = []


class GlobalFunctionType(str, Enum):
    """Types of global functions supported by the system."""

    HTTP = "http"
    BUILTIN = "builtin"  # Built-in handlers (e.g., warm transfer, get current time)
    CUSTOM = "custom"  # Future: custom Python function handlers


class ApprovalOnNoChannel(str, Enum):
    """Fallback behavior for approval-gated functions on channels with no
    approval surface (telephony). Daily/web voice without a working approval
    channel always denies and never consults this setting."""

    EXECUTE = "execute"
    DENY = "deny"


class ApprovalConfig(BaseModel):
    """Human-in-the-loop approval for a global function or an MCP tool.

    Presence of this config marks the call as gated: the LLM may call the
    function/tool, but execution waits for an explicit end-user decision. On a
    global function it is set inline via ``BaseGlobalFunction.approval``; on an
    MCP tool it is set via ``McpServerConfig.tool_approvals[<raw tool name>]``.
    The gate semantics below are identical for both.

    Channel behavior:
    - Chat: the turn ends at the gated call (`turn_end` carries
      ``awaiting_approval``); the decision arrives on the dedicated
      ``POST .../session/{id}/approval`` endpoint which resumes the turn.
    - Voice (Daily): the in-process handler blocks on an RTVI
      ``function-approval-decision`` client message. Whether the bot keeps
      conversing while waiting is governed by the function's existing
      ``cancel_on_interruption`` flag (True = bot waits silently and any user
      utterance cancels the request; False = async call, the bot keeps
      talking and the decision may arrive minutes later).
    - Telephony: no approval surface exists; ``on_no_channel`` applies.
    """

    prompt: Optional[str] = Field(
        None,
        description="Human-facing text shown on the approval card. Note: the "
        "post-injection arguments that will actually run are shown to the "
        "end user alongside this prompt.",
    )
    voice_announce: Optional[str] = Field(
        None,
        description="Optional phrase spoken (queue_tts_filler) when the voice "
        "gate starts waiting, e.g. 'I need your approval for this refund.' "
        "None = no spoken announcement (the visual card is the only cue).",
    )
    timeout_secs: float = Field(
        120,
        gt=0,
        description="VOICE-only: how long the in-process gate waits for a "
        "decision before resolving as timeout. Must stay below the pipeline "
        "idle timeout (300s) and the template's user-idle end-call budget.",
    )
    chat_expiry_secs: float = Field(
        3600,
        gt=0,
        description="CHAT-only: TTL for the pending approval card "
        "(expires_at). Decoupled from the voice wait — chat blocks nothing "
        "server-side, so this can be generous.",
    )
    on_no_channel: ApprovalOnNoChannel = Field(
        ApprovalOnNoChannel.EXECUTE,
        description="TELEPHONY-only fallback when no approval surface exists: "
        "'execute' (default; logs a loud warning) or 'deny'.",
    )


class BaseGlobalFunction(BaseModel):
    """Base model for all global function types.

    Subclasses should define their specific configuration fields.

    Note: func_pre_actions / func_post_actions use aliases so that existing
    JSON templates with 'pre_actions' / 'post_actions' keys on global functions
    continue to work. In Python code, always use the func_ prefixed names.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: GlobalFunctionType
    name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    filler_audio: Optional["FillerAudioConfig"] = Field(
        None,
        description="Audio to play while this function call is executing. "
        "Use 'filler_phrases' to speak a TTS phrase before the handler runs, "
        "or 'background_music' to loop background music during execution.",
    )
    func_pre_actions: List[FlowAction] = Field(default=[], alias="pre_actions")
    func_post_actions: List[FlowAction] = Field(default=[], alias="post_actions")
    cancel_on_interruption: Optional[bool] = Field(
        default=None,
        description="Pipecat 1.0 async function calls. When False, the LLM "
        "continues the turn without waiting for the function result; the "
        "result is injected as a developer message later, triggering a new "
        "LLM inference. Defaults to flows' own default (False) when unset.",
    )
    timeout_secs: Optional[float] = Field(
        default=None,
        description="Per-function timeout override in seconds. Falls back to "
        "the LLM service's function_call_timeout_secs when unset.",
    )
    approval: Optional[ApprovalConfig] = Field(
        default=None,
        description="Human-in-the-loop gate. When set, the function is "
        "approval-gated: execution waits for an explicit end-user decision "
        "(chat: dedicated approval endpoint resumes the turn; voice: RTVI "
        "decision message). Interacts with cancel_on_interruption — True "
        "means the bot blocks while waiting (and any user utterance cancels "
        "the request), False means the bot keeps conversing (async call).",
    )
    response_transforms: List["ResponseTransform"] = Field(
        default_factory=list,
        description=(
            "Optional in-place transforms applied to the tool result before "
            "the LLM sees it. Complements expected_response_schema (projection): "
            "transforms mutate specific paths and pass everything else through. "
            "Channel-agnostic — applied inside the handler, so voice + chat + "
            "any future channel benefit uniformly. Use the registered transform "
            "names from handlers/transport/utils/response_transform.py."
        ),
    )


class GlobalHttpFunction(BaseGlobalFunction):
    """
    Configuration for a global HTTP function available across all nodes.

    Global functions are registered with FlowManager and can be called by the LLM
    from any node in the conversation. Unlike hooks (fire-and-forget), global
    functions wait for the HTTP response and return data to the LLM.

    Example:
        {
            "name": "check_order_status",
            "description": "Check the status of a customer order",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "expected_fields": {
                "order_id": {"source": "llm", "value": "order_id"},
                "api_key": {"source": "static", "value": "sk-123"}
            },
            "http_request": {
                "method": "GET",
                "url": "https://api.example.com/orders/{order_id}",
                "auth": {"type": "bearer", "token": "{api_key}"}
            }
        }
    """

    type: GlobalFunctionType = GlobalFunctionType.HTTP
    expected_fields: Dict[str, FieldConfig] = {}
    http_request: HttpRequestConfig
    sse_response_handler: Optional[SseResponseHandlerConfig] = None
    # Pipecat 1.0 async function calls (PR #4217): HTTP global functions are
    # the canonical "slow API" path, so default to async — the LLM continues
    # talking while the request runs and the result is injected as a developer
    # message that re-triggers inference. Templates can override per-function.
    cancel_on_interruption: Optional[bool] = False
    expected_response_schema: Union[Dict[str, str], Literal["full"]] = {}
    """Whitelist of fields to extract from the HTTP response before feeding to the LLM,
    or ``"full"`` to pass the entire response to the LLM and store it in node traversal.

    Key   = name as it will appear in the LLM payload.
    Value = JMESPath expression (https://jmespath.org) evaluated against the
            response data. ``{placeholder}`` tokens in the expression are
            resolved from the LLM function call arguments before evaluation.

    Common patterns:
        - Simple field:        ``"status"``
        - Nested field:        ``"order.status"``
        - Array wildcard:      ``"items[*].name"``
        - Multi-field project: ``"rides[*].{rideId: rideId, area: pickup.area}"``
        - Filter with arg:     ``"coinEarnHistory[?rideId=='{ride_id}']"``

    Behaviour by value:

    - **Dict of JMESPath expressions** (e.g. ``{"driverId": "driverId"}``):
      Response is filtered to only those fields before being sent to the LLM.
      The filtered dict is also stored in node traversal. On 4xx/5xx the raw
      error body is stored instead (filtering is skipped for error responses).

    - **``"full"``**: The entire response is passed to the LLM unchanged and
      stored in node traversal. On 4xx/5xx the raw error body is stored.

    - **Empty dict ``{}`` (default)**: The full response is passed to the LLM
      unchanged, but **nothing is stored** in node traversal.
    """


class GlobalBuiltinFunction(BaseGlobalFunction):
    """
    Configuration for a built-in global function available across all nodes.

    Built-in functions are internal handlers (e.g., warm transfer, get current time)
    that can be exposed as global functions via template configuration. The `handler`
    field maps to a key in the builtin handler registry.

    Example:
        {
            "type": "builtin",
            "name": "transfer_to_agent",
            "handler": "connect_to_live_agent",
            "description": "Transfer the call to a human agent when requested"
        }
    """

    type: GlobalFunctionType = GlobalFunctionType.BUILTIN
    handler: str = Field(
        ...,
        description="Key in the builtin handler registry (e.g., 'connect_to_live_agent', 'get_current_time')",
    )
    pre_tts_message: Optional[str] = Field(
        None,
        description="TTS message to speak and wait for completion BEFORE executing the handler. "
        "Useful for handlers that terminate the pipeline (e.g., transfer) where the LLM's "
        "generated text may get cut off.",
    )
    # Pipecat-flows 1.0 flipped the FlowsFunctionSchema default to async
    # (cancel_on_interruption=False). Builtins are control-flow critical
    # (warm_transfer, end_conversation, etc.) — the LLM must NOT keep talking
    # over the handler. Force sync execution to preserve pre-1.0 behavior.
    cancel_on_interruption: Optional[bool] = True


class GlobalCustomFunction(BaseGlobalFunction):
    """
    Configuration for a custom Python global function available across all nodes.

    Custom functions allow developers to write Python code directly in the template
    that gets compiled at build time and executed when the LLM calls the function.

    The python_code string must define a top-level callable named 'handler' that
    accepts two arguments: (args, context).

    Example:
        {
            "type": "custom",
            "name": "calculate_discount",
            "description": "Calculate discount based on order count",
            "properties": {"order_count": {"type": "integer"}},
            "required": ["order_count"],
            "python_code": "def handler(args, context):\\n    n = args['order_count']\\n    if n > 50:\\n        return {'tier': 'gold'}\\n    return {'tier': 'bronze'}"
        }

    Handler contract:
        def handler(args: dict, context: dict) -> Any:
            # args: LLM-provided arguments
            # context: read-only context with lead, call_sid, lead_id
            # return: any JSON-serializable value
    """

    type: GlobalFunctionType = GlobalFunctionType.CUSTOM
    python_code: str = Field(
        ...,
        description="Python source code. Must define a top-level 'handler(args, context)' function.",
    )
    timeout_seconds: int = Field(
        5,
        ge=1,
        le=30,
        description="Wall-time limit per invocation in seconds (1-30, default 5).",
    )
    # Populated by the adapter after successful compile. Excluded from serialization.
    compiled_handler: Optional[Any] = Field(default=None, exclude=True, repr=False)

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)


class FlowNodeModel(BaseModel):
    node_name: str
    task_messages: List[TaskMessage]
    role_messages: List[TaskMessage] = []
    pre_actions: List[FlowAction] = []
    post_actions: List[FlowAction] = []
    functions: List[FlowFunction] = []
    vad_config: Optional[VadConfig] = Field(
        None, description="Node-specific VAD configuration (overrides template VAD)"
    )
    interruption: Optional[InterruptionConfig] = Field(
        None,
        description="Node-specific interruption configuration (overrides template interruption)",
    )
    input_collection: Optional[InputCollectionConfig] = Field(
        None,
        description="Node-specific input collection configuration for multi-segment "
        "input (e.g., phone numbers, addresses). Increases user_speech_timeout "
        "so natural pauses don't prematurely end the user's turn.",
    )


class FlowMode(str, Enum):
    """Top-level flow mode controlling how the agent is wired.

    FLOW (default):  Multi-node template with LLM-driven transitions, hooks,
                     pre/post-actions per node — the original Breeze Buddy
                     wiring on top of pipecat-flows FlowManager.
    DIRECT:          Single global system prompt + flat function list with
                     no node transitions. Closer to a vanilla pipecat agent;
                     internally implemented as a synthetic single node so
                     the rest of the pipeline (filler audio, hooks, OTEL,
                     evaluators, greeting, idle handling) is unchanged.
    """

    FLOW = "flow"
    DIRECT = "direct"


class DirectModeFlow(BaseModel):
    """Schema for ``flow`` JSON when ``mode == "direct"``.

    Only used for documentation and template-side validation — the builder
    reads the raw dict directly (mode-agnostic loading path).

    Direct mode is intentionally minimal: one global system prompt and a
    single flat ``functions`` array. VAD, interruption, and input-collection
    settings live at the template level (``template.configurations.*``),
    same as today — there is no per-node override because there is only
    one (synthetic) node.

    Example::

        {
            "mode": "direct",
            "system_prompt": "You are an order-confirmation agent...",
            "functions": [
                # FlowFunction-style: hook-driven side effects
                {
                    "name": "user_busy",
                    "description": "Mark the user as busy.",
                    "hooks": [{"name": "update_outcome_in_database", ...}]
                },
                # Builtin global function
                {"type": "builtin", "name": "end_conversation",
                 "handler": "end_conversation",
                 "description": "Politely end the call when the user is done."},
                # HTTP global function
                {"type": "http", "name": "check_order_status", ...},
                # Custom Python global function
                {"type": "custom", "name": "calculate_discount", ...}
            ]
        }
    """

    mode: FlowMode = Field(
        FlowMode.DIRECT,
        description="Must be 'direct' for this schema.",
    )
    system_prompt: str = Field(
        ...,
        description="Global system prompt for the LLM. Rendered with "
        "{placeholder} variables resolved from lead payload.",
    )
    functions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Functions exposed to the LLM. Heterogeneous: each entry "
        "is either a FlowFunction-style dict (with optional hooks; "
        "``transition_to`` is ignored) OR a global function with "
        "``type: 'http' | 'builtin' | 'custom'`` (built via the existing "
        "GlobalFunctionRegistry adapters).",
    )
    end_conversation_callbacks: List[str] = Field(
        default_factory=list,
        description="Same semantics as flow mode — list of callback names "
        "to invoke when the conversation ends.",
    )


def _default_supported_channels() -> List[Literal["voice", "chat"]]:
    """Default `TemplateModel.supported_channels` factory — voice-only."""
    return ["voice"]


class TemplateModel(BaseModel):
    # Read-only fields (set by server, not editable via API).
    # These are intentionally excluded from ReplaceTemplateRequest so that
    # a GET response can be sent directly to PUT — extra fields are auto-stripped.
    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

    # Editable fields (these match ReplaceTemplateRequest field names 1:1).
    name: str
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
    outbound_number_id: Optional[str] = None
    is_active: bool = True
    # Channels this template is allowed to be served on. Defaults to
    # voice-only so existing templates are unaffected. Add "chat" to
    # opt the template into the chat (text) mode flow build path
    # (see docs/CHAT_MODE.md §8). Persistence column lands with the
    # chat router task; until then the field uses the default.
    supported_channels: List[Literal["voice", "chat"]] = Field(
        default_factory=_default_supported_channels,
        min_length=1,
    )


# Request models for API
class RequestFlowFunction(BaseModel):
    function_name: str
    description: str
    properties: Dict[str, Any] = {}
    required: List[str] = []
    transition_to: Optional[str] = None
    hooks: List[HookConfig] = []


class RequestFlowNode(BaseModel):
    node_name: str
    task_messages: List[Dict[str, Any]] = []
    role_messages: List[Dict[str, Any]] = []
    pre_actions: List[Dict[str, Any]] = []
    post_actions: List[Dict[str, Any]] = []
    functions: List[RequestFlowFunction] = []


# A template with reseller_id "" is unreachable by every reseller-scoped
# lookup (RBAC filters, get-by-name resolution, config fallback), so reject
# empty / whitespace-only values at the schema boundary.
ResellerId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateTemplateRequest(BaseModel):
    # New field names
    reseller_id: ResellerId
    name: str
    merchant_id: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: bool = True
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
    supported_channels: List[Literal["voice", "chat"]] = Field(
        default_factory=_default_supported_channels,
        min_length=1,
    )


class ReplaceTemplateRequest(BaseModel):
    """Request model for updating a template via PUT.

    IMPORTANT — GET-to-PUT contract:
    This model uses extra="ignore" so that a GET /templates/{id} response can be
    sent directly to PUT /templates/{id} after editing. Read-only fields returned
    by GET (id, created_at, updated_at) are automatically stripped.
    When adding new read-only fields to TemplateModel, do NOT add them here —
    they will be safely ignored. When adding new editable fields, add them to
    BOTH TemplateModel and this model with the SAME field name.

    Non-nullable fields (name, flow, is_active) must be provided - throws 400 if not.
    Nullable fields (merchant_id, outbound_number_id, expected_payload_schema,
    expected_callback_response_schema, configurations) - if not provided, set to NULL.

    ``reseller_id`` is optional (``None`` default) for backward compatibility:
    pre-existing PUT clients never sent it (it used to be silently ignored), so
    omitting it preserves the persisted value. When provided it must be
    non-empty and the template is moved to that reseller — handler re-validates
    RBAC against the destination and checks for name collisions there.

    ``supported_channels`` is intentionally optional (``None`` default) rather
    than defaulting to ``['voice']``: pre-chat-feature PUT clients have no
    knowledge of this field, and silently overwriting a chat-enabled template
    back to voice-only on every unrelated edit would be a behaviour change.
    Handler keeps the persisted value when this field is omitted.
    """

    # extra="ignore" allows clients to pass the full GET response body to PUT;
    # read-only fields (id, created_at, updated_at) are auto-stripped.
    model_config = ConfigDict(extra="ignore")

    reseller_id: Optional[ResellerId] = None
    name: str
    merchant_id: Optional[str] = None
    outbound_number_id: Optional[str] = None
    is_active: bool
    flow: Dict[str, Any]
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[ConfigurationModel] = None
    secrets: Optional[Dict[str, Any]] = None
    supported_channels: Optional[List[Literal["voice", "chat"]]] = Field(
        default=None,
        min_length=1,
    )
