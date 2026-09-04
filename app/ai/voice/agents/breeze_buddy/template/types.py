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
    ValidationInfo,
    field_serializer,
    model_validator,
)

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import ActionUnion, Icon
from app.ai.voice.llm.types import LLMConfiguration
from app.core.deprecation import format_template_ref, log_deprecated_fields
from app.core.logger import logger


class ActionType(str, Enum):
    TTS_SAY = "tts_say"
    END_CONVERSATION = "end_conversation"
    FUNCTION = "function"
    ALERT = "alert"


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
    max_endpoint_delay_ms: Optional[int] = Field(
        None,
        ge=500,
        le=3000,
        description="Soniox native endpoint detection delay (500-3000ms). "
        "Overrides env default if set.",
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
    """Legacy noise-filter identifiers kept for template compatibility."""

    AIC = "aic"  # ai-coustics noise enhancement filter
    AIC_VOICE_FOCUS = "aic_voice_focus"  # primary-speaker isolation


class NoiseFilterProvider(str, Enum):
    """Audio-filter integrations available to the voice pipeline."""

    AIC = "aic"


class NoiseFilterModel(str, Enum):
    """Models available from the selected noise-filter provider."""

    NOISE_CANCELLATION = "noise_cancellation"
    VOICE_FOCUS = "voice_focus"


class NoiseFilterConfig(BaseModel):
    """Configuration for audio input noise filtering."""

    enable: bool = Field(False, description="Whether to enable the noise filter")
    type: Optional[NoiseFilterType] = Field(
        None,
        description=(
            "DEPRECATED compatibility field. Existing configurations may use "
            "this instead of provider/model."
        ),
        exclude_if=lambda value: value is None,
        json_schema_extra={"deprecated": True},
    )
    provider: Optional[NoiseFilterProvider] = Field(
        None,
        description="Noise-filter integration provider.",
        exclude_if=lambda value: value is None,
    )
    model: Optional[NoiseFilterModel] = Field(
        None,
        description="Noise cancellation preserves speech from all speakers; Voice "
        "Focus isolates the foreground speaker.",
        exclude_if=lambda value: value is None,
    )
    enhancement_level: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="ai-coustics enhancement strength. None uses the selected "
        "model's default; Voice Focus experiments should start at 0.5.",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _reject_mixed_selector_shapes(self) -> "NoiseFilterConfig":
        """Accept the legacy or canonical selector shape, never both."""
        if self.type is not None and (
            self.provider is not None or self.model is not None
        ):
            raise ValueError(
                "noise_filter must use either legacy 'type' or new "
                "'provider'/'model' fields, not both"
            )
        if (self.provider is None) != (self.model is None):
            raise ValueError(
                "noise_filter 'provider' and 'model' must be provided together"
            )
        return self

    @model_validator(mode="after")
    def _warn_deprecated_fields(self, info: ValidationInfo) -> "NoiseFilterConfig":
        log_deprecated_fields(
            self,
            {
                "type": "noise_filter.provider/noise_filter.model",
            },
            context=info.context,
        )
        return self


class TTSProvider(str, Enum):
    """Supported TTS providers."""

    ELEVENLABS = "elevenlabs"
    CARTESIA = "cartesia"
    SARVAM = "sarvam"
    GEMINI = "gemini"
    GOOGLE = "google"
    SONIOX = "soniox"
    DRAGONTTS = "dragontts"


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
        description="TTS provider (elevenlabs, cartesia, sarvam, gemini, google, soniox, dragontts)",
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
    stability: Optional[float] = Field(
        None,
        description=("Voice stability, 0.0-1.0 — lower is more"),
    )
    similarity_boost: Optional[float] = Field(
        None,
        description=(
            "Similarity boost, 0.0-1.0 — how closely the output matches the original voice sample"
        ),
    )
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
    enable_tts_caching: Optional[bool] = Field(
        None,
        description=(
            "When true AND provider is not 'dragontts', route TTS through the "
            "DragonTTS caching proxy at call time using model "
            "'<provider>:<model>' (e.g. provider=cartesia + model=sonic-3.5 -> "
            "dragontts:cartesia:sonic-3.5). Default None (off) — opt in per "
            "template. The DragonTTS kill switch flips this to false on DragonTTS "
            "failure; it never rewrites provider/model. Ignored when provider is "
            "already 'dragontts' (those always route through DragonTTS)."
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
    VOICEMAIL_BEEP = "voicemail-beep"  # voicemail_beep_{8k,24k}.mp3


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


class GreetingTileOption(BaseModel):
    """One visual quick-tap tile on the widget's greeting screen
    (2026-08-03) — an image-backed sibling of QuickReplyOption.

    Rendered as a small playful card (image + label) below the initial
    greeting; tapping it sends ``prompt`` as the user's message (the
    label is the visible bubble text, mirroring chicklet semantics).
    Purely template-authored: images come from the merchant's CDN, no
    catalog fetch, no hydration."""

    label: str = Field(..., min_length=1, description="Tile caption shown to the user.")
    prompt: str = Field(
        ...,
        min_length=1,
        description="Message sent to the agent when the tile is tapped.",
    )
    image_url: str = Field(
        ..., min_length=1, description="Tile image (merchant CDN, https)."
    )


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
    call to a third party, this config tells the handler which telephony number
    (and therefore which template) to use for the outbound conversation.

    Example:
        {
            "telephony_number_id": "uuid-of-telephony-number",
            "hold_music": "typing",
            "hold_timeout_seconds": 180,
            "summarize": true
        }
    """

    telephony_number_id: Optional[str] = Field(
        default=None,
        description="Telephony number ID used to make the outbound call. "
        "The template associated with this telephony_number_id "
        "will be used for the outbound conversation.",
    )
    # Deprecated alias: stored template configurations predating the telephony
    # rename carry this key in their JSON. It keeps parsing (and logs
    # [Deprecated] per occurrence, the house convention for tracking legacy
    # fields) but is excluded from serialization, so any GET->PUT cycle
    # rewrites a template to the new key.
    outbound_number_id: Optional[str] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _coalesce_number_id_and_warn(
        self, info: ValidationInfo
    ) -> "HoldTransferConfig":
        log_deprecated_fields(
            self,
            {"outbound_number_id": "hold_transfer.telephony_number_id"},
            context=info.context,
        )
        if self.telephony_number_id is None:
            self.telephony_number_id = self.outbound_number_id
        if self.telephony_number_id is None:
            raise ValueError("hold_transfer requires telephony_number_id")
        return self

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
    tool_response_schemas: Dict[str, Union[Dict[str, str], Literal["full"]]] = Field(
        default_factory=dict,
        description=(
            "Per-tool response projection (JMESPath whitelist) applied after the "
            "MCP tool call returns — before transforms/ui-hint and before the "
            "result reaches the LLM. The MCP counterpart of "
            "GlobalHttpFunction.expected_response_schema. Keys are the raw MCP "
            "tool names; values are either a dict of {llm_field: jmespath} or the "
            '``"full"`` sentinel (no projection). Applied by the shared result '
            "pipeline (handlers/transport/utils/tool_pipeline.py). Absent "
            "(default) → no projection, unchanged behavior."
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


class FlavorProtocolConfig(BaseModel):
    """One protocol dialect of a flavor: which platform connectors serve it
    and which optional features are on.

    Mirrors the flavor package layout — ``assist/<flavor>/<protocol>/`` is
    the protocol layer, ``assist/<flavor>/connectors/<name>/`` are the
    platforms that serve it — so config and code read the same way::

        "flavor": {"ucp": {"connectors": ["shopify"],
                           "features": {"upsell": true}}}

    The engine treats both fields opaquely: it never knows which protocols,
    connectors, or features exist. A flavor resolves its own block by name
    and falls back to its defaults for anything unset.
    """

    connectors: List[str] = Field(
        default_factory=list,
        description=(
            "Platform connectors to consult for this protocol, by name. "
            "EMPTY (the default) means every registered connector "
            "self-selects on the data it is handed — correct when the "
            "platform is unambiguous from the payload. Name them "
            "explicitly when a connector would otherwise mis-fire on a "
            "look-alike gateway (e.g. a non-Shopify store whose product "
            "URLs are also /products/{handle}, which would cost a dead "
            "storefront fetch per product view)."
        ),
    )
    features: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Optional flavor features → on/off. OPT-IN by contract: every "
            "feature is OFF unless turned on here, so enabling a flavor "
            "never silently starts extra behaviour on an existing "
            "merchant. Commerce/ucp: 'upsell' streams a complementary "
            "ProductGrid after a successful add_to_cart (one extra LLM "
            "call + catalog search per add, run after the cart is already "
            "on the wire)."
        ),
    )


class UiIntentsConfig(BaseModel):
    """Per-template overrides for flavor ``ui_intent`` executors
    (RFC-001 §3.3 Stage B) — the no-LLM DIRECT path where the widget
    fires a canonical intent and the server picks the tool.

    Generic role → value mappings — the Python engine knows nothing about
    which roles exist; each flavor defines its own roles and falls back to
    its built-in defaults for any role the template leaves unset. The
    commerce flavor's roles::

        "configurations": {
            "ui_intents": {
                "tools": {"create_cart": "shopify_cart_create"},
                "state_keys": {"cart_id": "basket_id"},
                "labels": {"checkout": "Review and checkout"},
                "urls": {"checkout_page": "https://…/cart"}
            }
        }

    Only set roles that DIFFER from the flavor defaults (commerce:
    create_cart/update_cart/get_cart/get_product/search, cart_id/
    checkout_url) — identity entries are noise. Lets a merchant whose
    MCP exposes differently-named cart tools (or whose reducers persist
    different state keys) run the same DIRECT intent executors without
    code changes.

    ``tools`` is load-bearing beyond the DIRECT path: it is also how the
    engine decides which tool a flavor's step label, safety annotation and
    post-condition verifier attach to (see ``chat/flavors.py``). That is
    the point — a rebound tool keeps the metadata it needs — but it means
    a role must name a tool that actually does that role's job. Binding
    ``search`` to a mutating tool, say, marks that tool ``read_only`` and
    makes it eligible for parallel fan-out.
    """

    tools: Dict[str, str] = Field(
        default_factory=dict,
        description="Flavor tool role → actual tool name on this template's "
        "MCP/function surface. Binds the role's flavor METADATA too — its "
        "step label, safety annotation (read_only/idempotent, which drives "
        "parallel fan-out) and post-condition verifier follow the tool "
        "named here. Point a role at a tool that does a different job and "
        "it inherits the wrong safety class and post-conditions.",
    )
    state_keys: Dict[str, str] = Field(
        default_factory=dict,
        description="Flavor state role → agent_session_state key (must match "
        "this template's state_reducers output).",
    )
    labels: Dict[str, str] = Field(
        default_factory=dict,
        description="Flavor label role → display string (e.g. the checkout "
        "button label on the server-authored CartView).",
    )
    urls: Dict[str, str] = Field(
        default_factory=dict,
        description="Flavor URL role → fixed URL. Commerce: 'checkout_page' "
        "overrides the CartView checkout button's destination (e.g. the "
        "storefront /cart page) instead of the cart tool's checkout-bound "
        "continue_url; unset keeps continue_url.",
    )


class RenderUiConfig(BaseModel):
    """The render_ui function-tool surface (RFC-002) — UI authoring via
    function calling instead of the in-band ``<ui_stream>`` text
    protocol. One block per subsystem: everything here is inert unless
    ``enabled`` is true (and the session negotiated catalog v2)."""

    enabled: bool = Field(
        False,
        description=(
            "Expose the render_ui function tool on chat catalog-v2 "
            "sessions. The server keeps accepting text-channel show ops "
            "(dual-read) during migration. False = text channel only "
            "(fleet default)."
        ),
    )
    force_after: Optional[List[str]] = Field(
        None,
        description=(
            "RFC-002 Decision 2 ('force to THINK, not to always SHOW'): "
            "tool names whose successful result forces a render_ui call on "
            "the NEXT LLM cycle (mode=ANY; {decision:'no_ui', reason} is a "
            "legal payload). Absent = the enabled flavor's default "
            "(commerce: ['search_catalog']); no flavor → no forced step."
        ),
    )
    quick_replies: Optional[Literal["model_choice", "forced_final", "off"]] = Field(
        None,
        description=(
            "Placement policy for the LLM QuickReplies component "
            "(distinct from the top-level `quick_replies`, the static "
            "widget-open chicklets). Absent/'model_choice' = the model "
            "may render QuickReplies whenever it judges chips help. "
            "'forced_final' = QuickReplies are banned mid-turn; chip "
            "labels ride any call's quick_replies arg and flush below "
            "the final prose (falling back to ONE extra forced "
            "render_ui cycle). 'off' = QuickReplies never render."
        ),
    )
    trusted_link_urls: Optional[List[str]] = Field(
        None,
        description=(
            "URL allowlist for the LinkButton component (exact-match), "
            "e.g. the storefront checkout/cart URL. A LinkButton url is "
            "accepted only if it matches one of these OR appears verbatim "
            "in the current turn's tool results — the model selects "
            "links, it never authors them."
        ),
    )


class ToolExecutionConfig(BaseModel):
    """Per-template tool-dispatch safety policy (Phase 2): how the chat
    engine schedules and classifies tool calls within a cycle."""

    annotations: Optional[Dict[str, str]] = Field(
        None,
        description=(
            "Tool-name → safety annotation ('read_only' | 'idempotent' | "
            "'destructive') overrides. Precedence: this map > flavor "
            "registry defaults > 'destructive'. read_only tool calls from "
            "one LLM cycle dispatch in parallel; everything else stays "
            "serialized."
        ),
    )
    parallel_read_only: Optional[bool] = Field(
        None,
        description=(
            "Kill-switch for the read-only parallel fan-out. Absent/true "
            "= a cycle whose tool calls are ALL read_only dispatches them "
            "concurrently; false = strictly sequential (pre-Phase-2 "
            "behavior)."
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


class KnowledgeBaseMode(str, Enum):
    """How attached knowledge bases reach the LLM at runtime."""

    AUTO = "auto"  # size-tiered: full_injection if small, else auto_retrieve
    FULL_INJECTION = "full_injection"  # whole KB in the initial context
    AUTO_RETRIEVE = "auto_retrieve"  # hybrid retrieval every user turn
    TOOL = "tool"  # explicit query_knowledge_base function call


class KnowledgeBaseConfig(BaseModel):
    """Template-level knowledge base (RAG) configuration.

    Attaches merchant knowledge bases (see /knowledge-bases API) to this
    template and selects the retrieval mode. All attached KBs must share one
    embedding provider (query vectors are not comparable across providers).
    """

    enabled: bool = Field(True, description="Master switch for KB retrieval.")
    knowledge_base_ids: List[str] = Field(
        ...,
        min_length=1,
        description="IDs of knowledge_base rows attached to this template.",
    )
    mode: KnowledgeBaseMode = Field(
        KnowledgeBaseMode.AUTO,
        description=(
            "auto (default) resolves to full_injection when the KB total "
            "token count is at or below full_injection_token_threshold, "
            "else auto_retrieve. Realtime (speech-to-speech) templates "
            "support only full_injection and tool."
        ),
    )
    top_k: int = Field(
        5, ge=1, le=20, description="Chunks retrieved per query (auto_retrieve/tool)."
    )
    score_threshold: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum vector cosine similarity for retrieved chunks. "
            "Keyword-leg exact matches are always kept."
        ),
    )
    max_context_tokens: int = Field(
        2000,
        ge=100,
        le=8000,
        description="Per-turn token budget for injected chunks (auto_retrieve).",
    )
    full_injection_token_threshold: int = Field(
        20_000,
        ge=1_000,
        le=60_000,
        description="AUTO resolves to full_injection when the KB total token "
        "count is at or below this.",
    )
    retrieval_timeout_secs: float = Field(
        0.4,
        ge=0.1,
        le=2.0,
        description="Fail-open budget around retrieval on the voice hot path; "
        "on timeout the turn proceeds without KB context.",
    )
    include_recent_turns: int = Field(
        1,
        ge=0,
        le=6,
        description="Prior user turns appended to the retrieval query for "
        "conversational context ('how much is it?').",
    )
    injection_header: str = Field(
        "## Related knowledge",
        description="Header line above injected chunks (auto_retrieve).",
    )
    injection_instruction: Optional[str] = Field(
        None,
        description="Overrides the default refusal-by-default instruction "
        "that accompanies injected KB content.",
    )
    # --- tool mode ---
    tool_name: str = Field(
        "query_knowledge_base",
        description="LLM-visible function name when mode='tool'.",
    )
    tool_description: Optional[str] = Field(
        None,
        description="LLM-visible function description when mode='tool'. "
        "Name the KB contents here so the model knows when to call it.",
    )

    @model_validator(mode="after")
    def _validate_tool_name(self):
        if not self.tool_name.strip():
            raise ValueError("tool_name cannot be empty")
        return self


class AgentTransferConfig(BaseModel):
    """Configuration for agent-to-agent transfer (connect_to_agent builtin)."""

    targets: Dict[str, str] = Field(
        ...,
        description="Alias -> target template id. The LLM selects an alias "
        "(e.g. 'sales'); the template controls which template it maps to.",
    )
    max_transfers: int = Field(
        5,
        ge=1,
        le=20,
        description="Max transfers per call — loop guard against A->B->A ping-pong.",
    )
    context_mode: Literal["summary", "full", "fresh"] = Field(
        "summary",
        description="What the next agent inherits: 'summary' = handoff note with "
        "the LLM-provided summary; 'full' = replay entire message history; "
        "'fresh' = nothing but a transfer marker.",
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
    render_ui: Optional["RenderUiConfig"] = Field(
        None,
        description=(
            "Optional. The render_ui function-tool subsystem (RFC-002): "
            "enabled flag, forced think-step tools, QuickReplies "
            "placement policy, and the LinkButton URL allowlist. Absent "
            "= text channel only (fleet default)."
        ),
    )
    ui_intents: Optional["UiIntentsConfig"] = Field(
        None,
        description=(
            "Optional. Per-template overrides for the tool names, state "
            "keys, labels, and URLs a flavor's DIRECT ui_intent executors "
            "use (RFC-001 §3.3 Stage B). Absent = each flavor's built-in "
            "defaults (commerce: create_cart/update_cart/get_cart, "
            "cart_id/checkout_url, 'Review and checkout')."
        ),
    )
    flavor: Optional[Dict[str, "FlavorProtocolConfig"]] = Field(
        None,
        description=(
            "Optional. Protocol dialect → which platform connectors serve "
            "it and which optional features are on, keyed the same way the "
            "flavor packages are laid out on disk::\n\n"
            '    "flavor": {"ucp": {"connectors": ["shopify"],\n'
            '                       "features": {"upsell": true}}}\n\n'
            "Absent (or a protocol left unset) = connectors self-select on "
            "the payload and every optional feature is OFF. The engine "
            "never interprets the keys — a flavor resolves its own "
            "protocol block by name."
        ),
    )
    tool_execution: Optional["ToolExecutionConfig"] = Field(
        None,
        description=(
            "Optional. Tool-dispatch safety policy: per-tool safety "
            "annotations and the read-only parallel fan-out kill-switch. "
            "Absent = flavor defaults, fan-out enabled."
        ),
    )
    plan_enforcement: Optional[bool] = Field(
        None,
        description=(
            "RFC-002 Decision 4: when the model declares a <plan> (2+ "
            "steps), constrain each cycle's callable functions to the "
            "current step's tool + revise_plan, gate step-completion on "
            "the deterministic verifiers, and require revise_plan after a "
            "retry fails. Single-tool turns are exempt by construction. "
            "Absent/false = plans stay advisory (UX skeleton only)."
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
    payload_based_language_selection: Optional[bool] = Field(
        None,
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
    def _pre_validate(cls, data: Any, info: ValidationInfo) -> Any:
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
                ctx = info.context if isinstance(info.context, dict) else {}
                ref = format_template_ref(
                    ctx.get("template_id"), ctx.get("template_name")
                )
                for old_field in ("ivr_greeting", "ivr_goodbye", "ivr_priority"):
                    if data.get(old_field) is not None:
                        new_field = old_field.replace("ivr_", "")
                        logger.warning(
                            f"[Deprecated] field '{old_field}' is set{ref}. "
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
    greeting_tiles: List[GreetingTileOption] = Field(
        default_factory=list,
        description=(
            "Visual quick-tap tiles (image + label → prompt) on the widget's "
            "greeting screen, below the initial greeting. 3-4 recommended; "
            "empty list = no tiles."
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
    enable_topic_evaluation: Optional[bool] = None
    user_idle_configuration: Optional[UserIdleHandlingConfig] = (
        None  # User idle handling config
    )
    noise_filter: Optional[NoiseFilterConfig] = Field(
        None, description="Noise filter configuration for audio input processing"
    )
    client_noise_cancellation: Optional[bool] = Field(
        None,
        description=(
            "Browser-side (Daily Krisp) noise cancellation for web voice "
            "sessions. None/True = ON (platform default), False = opt out. "
            "Rides the /connect response as `noise_cancellation`; echo "
            "cancellation itself is always forced client-side and has no knob."
        ),
    )
    dial_tone: bool = Field(
        True,
        description=(
            "Whether to play the dial-tone fallback when no initial greeting "
            "audio is cached. True (default) preserves legacy behaviour — a "
            "short dial tone plays so the caller knows the line is live. Set "
            "False for speech-to-speech realtime LLMs (e.g. Gemini Live) so "
            "that, when no greeting is cached, nothing plays out-of-band and "
            "the realtime LLM speaks first (avoids the dial tone colliding "
            "with the LLM's opening response)."
        ),
    )
    # NOTE: Gemini Live greeting pre-generation is automatic from this
    # initial_greeting field + llm_configurations.realtime — no flag here.
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
    knowledge_base: Optional[KnowledgeBaseConfig] = Field(
        None,
        description="Knowledge base (RAG) retrieval configuration. Attaches "
        "merchant knowledge bases and selects how retrieved content reaches "
        "the LLM (auto / full_injection / auto_retrieve / tool).",
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
    observers: Optional[List["ObserverConfig"]] = Field(
        None,
        description="Real-time observers that watch the conversation in parallel. "
        "Each observer is a side-LLM that checks the transcript after every turn "
        "and can take action (e.g., end_conversation) on detection.",
    )

    @model_validator(mode="after")
    def _warn_deprecated_fields(self, info: ValidationInfo):
        log_deprecated_fields(
            self,
            {
                "stt_language": "stt_configuration.language",
                "soniox_context": "stt_configuration.soniox.context",
                "payload_based_language_selection": "stt_configuration.payload_based_language_selection",
                "mira_voice_id": "cartesia_voice_configurations.voice_id",
            },
            context=info.context,
        )
        return self

    @model_validator(mode="after")
    def _validate_kb_realtime_compat(self):
        """Reject auto/auto_retrieve KB modes on realtime (speech-to-speech)
        LLMs at parse time, so template authors get a 422 at save instead of
        every call failing at setup (validate_template_compat re-checks the
        same invariant at call load as a backstop). The realtime pipeline has
        no pre-LLM text hop, so per-turn retrieval injection cannot work —
        and mode 'auto' may resolve to auto_retrieve whenever the KB grows.
        """
        kb = self.knowledge_base
        llm = self.llm_configurations
        if (
            kb is not None
            and kb.enabled
            and llm is not None
            and llm.realtime is not None
            and kb.mode in (KnowledgeBaseMode.AUTO, KnowledgeBaseMode.AUTO_RETRIEVE)
        ):
            raise ValueError(
                "knowledge_base mode 'auto'/'auto_retrieve' cannot be combined "
                "with a realtime LLM (llm_configurations.realtime) — the "
                "realtime pipeline has no pre-LLM text hop for per-turn "
                "injection. Use mode='full_injection' or 'tool'."
            )
        return self


class FlowAction(BaseModel):
    type: ActionType
    text: Optional[str] = None
    handler: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    description: Optional[str] = None


class ObserverConfig(BaseModel):
    """Real-time observer definition.

    Observers are side-LLMs that watch the conversation transcript in parallel
    with the main pipeline and can take action (e.g., end the call) when they
    detect a problem such as voicemail, hallucination, or abuse.
    """

    name: str = Field(..., description="Unique observer identifier")
    enabled: bool = Field(True, description="Whether this observer runs during calls.")
    system_prompt: str = Field(
        ...,
        description="Detection instructions for the observer LLM. "
        "The LLM will call the configured action function when the condition is detected.",
    )
    trigger_on: List[str] = Field(
        default=["on_user_turn_message_added"],
        description="Pipecat events that trigger this observer. "
        "User events: on_user_turn_started, on_user_turn_stopped, "
        "on_user_turn_message_added, on_user_turn_idle. "
        "Assistant events: on_assistant_turn_started, on_assistant_turn_stopped. "
        "LLM events: on_function_calls_started. "
        "Default: on_user_turn_message_added.",
    )
    start_after_turn: int = Field(
        0,
        ge=0,
        description="Skip the first N turns before checking. "
        "0 = check from the very first turn.",
    )
    llm: Optional[LLMConfiguration] = Field(
        None,
        description="Optional LLM override. Omit to inherit from "
        "the template's llm_configurations with model defaulting "
        "to gpt-4o-mini.",
    )
    action: FlowAction = Field(
        ...,
        description="Action to execute on detection. Uses the same "
        "FlowAction type and handler_map as template pre/post actions.",
    )


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
    ui_hint: Optional["ToolUiHint"] = Field(
        default=None,
        description=(
            "Optional JIT UI authoring guidance (Sidekick pattern) spliced into "
            "this function's result before the LLM sees it — the Global-HTTP "
            "counterpart of McpServerConfig.tool_ui_instructions. Injected under "
            "'_ui_instructions' / '_ui_examples' / '_ui_skip' by the shared "
            "result pipeline (handlers/transport/utils/tool_pipeline.py) per the "
            "hint's ToolUiTrigger. Absent (default) → no UI hint, unchanged "
            "behavior."
        ),
    )


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
    # Agent-to-agent transfer config (single source of truth — lives on the
    # function entry, not on ConfigurationModel). Only read when
    # handler == "connect_to_agent"; inert on every other builtin.
    agent_transfer: Optional[AgentTransferConfig] = Field(
        None,
        description="Config for the connect_to_agent handler: targets allow-list "
        "(alias -> template_id), max_transfers loop guard, and context_mode "
        "(what the next agent inherits: summary | full | fresh). Ignored by "
        "other builtins.",
    )


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
    IVR:             Pure DTMF state machine — no STT, no LLM, no Pipecat
                     pipeline. Each node plays a TTS prompt and maps pressed
                     digits to actions (transition to another node, or end the
                     call). Driven entirely by keypad input over the telephony
                     websocket. See ``IvrModeFlow``.
    """

    FLOW = "flow"
    DIRECT = "direct"
    IVR = "ivr"


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


class IvrAction(str, Enum):
    """What a pressed digit does in IVR mode. Deliberately just two actions.

    TRANSITION:  Go to another IVR node (a sub-menu).
    END:         Hang up. If the option has a ``message`` it is spoken first,
                 otherwise the call ends silently.
    """

    TRANSITION = "transition"
    END = "end"


class IvrOption(BaseModel):
    """One digit -> action mapping within an IVR node.

    ``outcome``/``metadata`` are applied to the lead IMMEDIATELY when the
    option fires (in-memory, plus a non-blocking background DB write) so that a
    caller who hangs up inside a sub-menu still has their last intent
    persisted. ``outcome`` is valid on BOTH ``transition`` and ``end`` — on a
    transition it records an intermediate outcome (e.g. ``CANCEL_STARTED``).

    No hook is required to persist ``outcome`` — that happens automatically.
    ``hooks`` are optional and only for EXTERNAL side-effects (e.g. notifying a
    merchant API); they are fire-and-forget, same as flow/direct mode.
    """

    digit: str = Field(
        ...,
        description="The keypad digit that selects this option: "
        "'0'-'9', '*', or '#'.",
    )
    action: IvrAction
    label: Optional[str] = Field(None, description="Human-readable note (not spoken).")
    target_node: Optional[str] = Field(
        None, description="Target node name. Required when action == 'transition'."
    )
    message: Optional[str] = Field(
        None,
        description="action == 'end' only: spoken before hangup. Omit for a "
        "silent hangup. Supports {placeholder} variables.",
    )
    outcome: Optional[str] = Field(
        None,
        description="Outcome written to the lead immediately when this option "
        "fires (valid on both 'transition' and 'end').",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Merged into lead.metaData immediately when this option "
        "fires (e.g. {'cancellation_reason': 'too_expensive'}).",
    )
    hooks: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Optional fire-and-forget hooks for external side-effects "
        "(HookConfig dicts, same shape as flow mode). NOT needed to persist "
        "'outcome'.",
    )

    @model_validator(mode="after")
    def _check_transition_target(self) -> "IvrOption":
        """A transition option must name the node it transitions to.

        Without this, a missing ``target_node`` makes the walker's
        ``if kind == "transition" and target:`` check silently fall through
        and end the call instead of transitioning. Fail fast at load time.
        """
        if self.action == IvrAction.TRANSITION and not self.target_node:
            raise ValueError(
                f"IVR option digit '{self.digit}': target_node is required "
                "when action == 'transition'"
            )
        return self


class IvrNode(BaseModel):
    """A single IVR menu node: a prompt plus digit->action options."""

    name: str
    prompt: Optional[str] = Field(
        None,
        description="TTS text spoken on entry (the menu). Supports "
        "{placeholder} variables resolved from lead payload. May be omitted "
        "ONLY for the initial_node, which then relies on "
        "configurations.initial_greeting (already spoken at call start, and "
        "re-spoken on no-input / return-to-main). Every non-initial node must "
        "set a prompt.",
    )
    options: List[IvrOption]
    invalid_prompt: Optional[str] = Field(
        None,
        description="Spoken when an unmapped digit is pressed. Defaults to "
        "replaying the prompt.",
    )
    timeout_secs: float = Field(
        7.0,
        description="Seconds to wait for a keypad digit AFTER the prompt "
        "finishes speaking (the prompt's own playback time is added "
        "automatically). Barge-in during the prompt is always allowed.",
    )
    max_retries: int = Field(
        3, description="How many times to replay the prompt before giving up."
    )
    on_timeout_message: Optional[str] = Field(
        None,
        description="Closing line spoken when retries are exhausted (else a "
        "default goodbye).",
    )
    on_timeout_outcome: Optional[str] = Field(
        None,
        description="Outcome recorded when the node times out / gives up. If "
        "omitted, the call defaults to BUSY (retry-eligible) — a no-input timeout "
        "is then re-dialled like a hangup. Set an explicit value (e.g. "
        "'NO_RESPONSE', 'CANCELLED') to record a terminal, non-retry outcome.",
    )


class IvrModeFlow(BaseModel):
    """Schema for ``flow`` JSON when ``mode == "ivr"``.

    A pure DTMF menu tree. Only ``tts_configuration`` from the template is
    consumed (to synthesize prompts); ``stt_configuration`` and
    ``llm_configurations`` are ignored. Example::

        {
            "mode": "ivr",
            "initial_node": "main",
            "nodes": {
                "main": {
                    "name": "main",
                    "prompt": "Press 1 to confirm, 2 to cancel.",
                    "options": [
                        {"digit": "1", "action": "end", "outcome": "CONFIRMED",
                         "message": "Thank you, your order is confirmed."},
                        {"digit": "2", "action": "transition",
                         "target_node": "cancel_reasons", "outcome": "CANCEL_STARTED"}
                    ]
                },
                "cancel_reasons": { ... }
            }
        }
    """

    mode: FlowMode = Field(FlowMode.IVR, description="Must be 'ivr' for this schema.")
    initial_node: str = Field(..., description="Name of the node to start at.")
    nodes: Dict[str, IvrNode] = Field(..., description="Map of node name -> IvrNode.")

    @model_validator(mode="after")
    def _check_node_references(self) -> "IvrModeFlow":
        """Fail fast on dangling node references at template-load time.

        Catches a typo'd ``initial_node`` or a transition ``target_node`` that
        points at a non-existent node before the call runs (otherwise the
        walker only discovers it mid-call as IVR_NODE_MISSING).
        """
        if self.initial_node not in self.nodes:
            raise ValueError(f"initial_node '{self.initial_node}' not found in nodes")
        for node_name, node in self.nodes.items():
            for opt in node.options:
                if (
                    opt.action == IvrAction.TRANSITION
                    and opt.target_node not in self.nodes
                ):
                    raise ValueError(
                        f"node '{node_name}' option '{opt.digit}' transitions "
                        f"to unknown node '{opt.target_node}'"
                    )
        return self


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
    telephony_number_id: Optional[str] = None
    # TEMPORARY read-compat: some clients still read outbound_number_id from
    # template GET responses, so responses mirror the pin under the old key
    # too (see _mirror_deprecated_number_id). Remove once the [Deprecated]
    # logs show no client sends the old key on writes anymore.
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

    @model_validator(mode="after")
    def _mirror_deprecated_number_id(self) -> "TemplateModel":
        # Server code constructs this model with telephony_number_id; the
        # elif tolerates direct construction from an old-key dict.
        if self.telephony_number_id is not None:
            self.outbound_number_id = self.telephony_number_id
        elif self.outbound_number_id is not None:
            self.telephony_number_id = self.outbound_number_id
        return self


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


def _resolve_deprecated_number_id(model: Any, info: ValidationInfo) -> None:
    """Shared Create/Replace resolution for the telephony_number_id /
    outbound_number_id pair.

    GET responses mirror the pin under BOTH keys (read-compat for old
    clients), so round-tripped PUT bodies routinely carry both. The rules:

    - only ``telephony_number_id`` sent           -> it applies, no log
    - both sent with the SAME value               -> round-trip, no log
    - only ``outbound_number_id`` sent, or both
      sent with DIFFERENT values                  -> the old key is the
      operative one (only an old-key writer produces this), so it wins and
      the use is logged [Deprecated]
    - explicit ``"outbound_number_id": null``     -> clears the pin (the
      house helper skips logging None values)
    """
    if "outbound_number_id" not in model.model_fields_set:
        return
    if model.telephony_number_id == model.outbound_number_id:
        return
    log_deprecated_fields(
        model,
        {"outbound_number_id": "telephony_number_id"},
        context=info.context,
    )
    model.telephony_number_id = model.outbound_number_id


class CreateTemplateRequest(BaseModel):
    # New field names
    reseller_id: ResellerId
    name: str
    merchant_id: Optional[str] = None
    telephony_number_id: Optional[str] = None
    # Deprecated: pre-rename key, accepted from older API clients and
    # converted (precedence rules in _resolve_deprecated_number_id). Every
    # operative use is logged [Deprecated] so migration can be tracked.
    outbound_number_id: Optional[str] = Field(default=None, exclude=True)
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

    @model_validator(mode="after")
    def _coalesce_number_id_and_warn(
        self, info: ValidationInfo
    ) -> "CreateTemplateRequest":
        _resolve_deprecated_number_id(self, info)
        return self


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
    Nullable fields (merchant_id, telephony_number_id, expected_payload_schema,
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
    telephony_number_id: Optional[str] = None
    # Deprecated: pre-rename key, accepted and converted. Precedence lives
    # in _resolve_deprecated_number_id: equal round-tripped values are a
    # no-op, and when the values differ the OLD key wins (only an old-key
    # writer produces that shape) — so old clients neither lose edits nor
    # silently clear pins. Every operative use is logged [Deprecated].
    outbound_number_id: Optional[str] = Field(default=None, exclude=True)
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

    @model_validator(mode="after")
    def _coalesce_number_id_and_warn(
        self, info: ValidationInfo
    ) -> "ReplaceTemplateRequest":
        _resolve_deprecated_number_id(self, info)
        return self
