"""AssemblyAI Universal Streaming (v3) STT config and builder.

Wraps pipecat's :class:`AssemblyAISTTService` (WebSocket streaming against
``wss://streaming.assemblyai.com/v3/ws``). AssemblyAI pushes ``Turn`` messages
carrying ``end_of_turn``; pipecat surfaces the partials as
``InterimTranscriptionFrame`` and the finals as ``TranscriptionFrame``.

Languages: Universal-3.5 Pro (``u3-rt-pro``) is multilingual BY DEFAULT and
code-switches mid-sentence across 18 languages, Hindi included — so Hinglish
works with no language setting at all. AssemblyAI's ``language_codes`` only
narrows that set for accuracy, and pipecat 1.1.0 does not expose it. Note that
narrowing to a SINGLE code makes the session monolingual, which is the failure
that forced English into Devanagari on the ElevenLabs path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, cast
from urllib.parse import urlencode

from pipecat.services.assemblyai.stt import AssemblyAISTTService

from app.core.logger import logger

__all__ = [
    "AssemblyAIConfig",
    "AssemblyAISTTServiceWithLanguageCodes",
    "build_assemblyai_stt",
]

# AssemblyAI's own recommendation for voice agents (docs: STT-based turn
# detection). pipecat leaves these unset, which lets AssemblyAI apply a default
# tuned for dictation rather than conversation.
DEFAULT_MIN_TURN_SILENCE_MS = 100
DEFAULT_MAX_TURN_SILENCE_MS = 1000

# The model name AssemblyAI documents, and the one that actually reaches the
# wire. Universal-3.5 Pro Streaming: multilingual with native mid-sentence code
# switching across 18 languages.
U3_PRO_MODEL = "universal-3-5-pro"

# The name pipecat 1.1.0 hardcodes. Its constructor gates AssemblyAI-side turn
# detection on `settings.model == "u3-rt-pro"` -- an exact string match, not a
# family check (1.8.1's is_u3_pro_model fixed that). AssemblyAI's docs never
# mention this name, and sending it appears to fall back to
# Universal-Streaming English: a live Hinglish call came back entirely in
# Latin script with Hindi romanised ("Mera kupa") and language_codes ignored.
# So we satisfy pipecat's check with this, then send the documented name.
PIPECAT_U3_PRO_ALIAS = "u3-rt-pro"


class AssemblyAISTTServiceWithLanguageCodes(AssemblyAISTTService):
    """AssemblyAI streaming STT that can also send ``language_codes``.

    pipecat 1.1.0 does not expose ``language_codes`` at all (1.8.1 added it).
    Without it, Universal-3.5 Pro still code-switches natively across its 18
    languages -- but unsteered, which on 8 kHz telephony audio is where a
    Hindi/English call gets mis-identified.

    Sending ``language_codes=["hi","en"]`` biases the model toward exactly
    those two while keeping mid-sentence code switching between them. Note
    that a SINGLE code makes the session monolingual, which is the failure
    that forced English into Devanagari on the ElevenLabs path -- so this is
    only useful with two or more.

    It also works around pipecat 1.1.0 hardcoding the model name ``u3-rt-pro``
    where AssemblyAI documents ``universal-3-5-pro`` -- see
    ``PIPECAT_U3_PRO_ALIAS``.

    REMOVE WHEN pipecat >= 1.8.1: ``AssemblyAISTTSettings.language_codes``
    replaces the first, and ``is_u3_pro_model()`` matching both names
    replaces the second.

    The wire format is pipecat 1.8.1's, verified against its ``_build_ws_url``:
    a JSON array, then urlencoded with the rest of the query string.
    """

    def __init__(self, *, language_codes: Optional[list[str]] = None, **kwargs):
        """Store the declared languages; everything else is pipecat's.

        Args:
            language_codes: ISO-639-1 codes to steer the model toward, max 10.
                Only meaningful on the u3-rt-pro family; other streaming models
                ignore it (Universal-Streaming English is English-only).
            **kwargs: Passed through to ``AssemblyAISTTService``.
        """
        # Two names exist for one model: AssemblyAI documents
        # "universal-3-5-pro"; pipecat 1.1.0 invented "u3-rt-pro" and
        # hardcodes `settings.model == "u3-rt-pro"` as the gate for
        # vad_force_turn_endpoint=False. Normalise BOTH spellings (and their
        # -* variants) inward: wear pipecat's name through its constructor,
        # then send AssemblyAI's before connect. _build_ws_url reads
        # settings.model at CONNECT time, well after validation.
        #
        # Accepting only the documented name would let a template written with
        # pipecat's spelling reach the wire verbatim, and AssemblyAI does not
        # recognise it -- it falls back to Universal-Streaming English, so a
        # Hinglish call returns Latin-script romanisation with no error at all.
        requested_model = getattr(kwargs.get("settings"), "model", None)
        is_u3_pro_family = isinstance(requested_model, str) and (
            requested_model.startswith(U3_PRO_MODEL)
            or requested_model.startswith(PIPECAT_U3_PRO_ALIAS)
        )
        if is_u3_pro_family:
            kwargs["settings"].model = PIPECAT_U3_PRO_ALIAS

        super().__init__(**kwargs)

        if is_u3_pro_family:
            # Variants (universal-3-5-pro-preview, u3-rt-pro-beta-1) collapse
            # to the canonical name: pipecat 1.1.0's exact-match gate cannot
            # accept them, and AssemblyAI only documents the base model.
            self._settings.model = U3_PRO_MODEL

        self._language_codes = language_codes or []

    def _build_ws_url(self) -> str:
        """Append ``language_codes`` to the URL pipecat already built.

        Overriding the whole builder would duplicate pipecat's parameter
        handling and drift on upgrade; appending to its return value does not.
        urlencode escapes the JSON, so nothing here can inject a parameter --
        unlike the ElevenLabs provider, where pipecat interpolates raw.
        """
        url = super()._build_ws_url()
        if not self._language_codes:
            return url
        encoded = urlencode({"language_codes": json.dumps(self._language_codes)})
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{encoded}"


@dataclass
class AssemblyAIConfig:
    """Configuration for AssemblyAI Universal Streaming STT.

    ``vad_force_turn_endpoint`` decides who ends a turn:

    - ``False``: AssemblyAI's own semantic endpointing closes turns. Needs the
      u3-rt-pro family (pipecat enforces this) and no local VAD. Pairs with
      ``turn_detection='stt_native'``.
    - ``True``: a local Silero VAD closes turns. Pairs with
      ``turn_detection='smart_turn'``, which is the only mode the pipeline
      auto-creates a VAD for.
    """

    api_key: str
    model: str = U3_PRO_MODEL
    vad_force_turn_endpoint: bool = False
    # Leave None so pipecat inherits the transport's rate (8 kHz telephony,
    # 16 kHz web). Pinning it here overrides the pipeline and mislabels the
    # audio — the same trap the ElevenLabs provider hit.
    sample_rate: Optional[int] = None
    # Capped at 10 to match pipecat 1.8.1's MAX_LANGUAGE_CODES -- AssemblyAI
    # documents 18 supported languages but no explicit cap on this list, so
    # the narrower limit is the one we can point at. Two or more codes steer
    # code switching; a single code makes the session monolingual.
    language_codes: Optional[list[str]] = None
    keyterms_prompt: Optional[list[str]] = None
    prompt: Optional[str] = None
    end_of_turn_confidence_threshold: Optional[float] = None
    min_turn_silence: Optional[int] = DEFAULT_MIN_TURN_SILENCE_MS
    max_turn_silence: Optional[int] = DEFAULT_MAX_TURN_SILENCE_MS
    formatted_finals: Optional[bool] = None
    format_turns: Optional[bool] = None
    language_detection: Optional[bool] = None
    speaker_labels: Optional[bool] = None
    vad_threshold: Optional[float] = None
    word_finalization_max_wait_time: Optional[int] = None
    domain: Optional[str] = None


def build_assemblyai_stt(config: AssemblyAIConfig) -> AssemblyAISTTService:
    """Create an AssemblyAI Universal Streaming STT service.

    Settings that are ``None`` are left off the connection URL entirely
    (``_build_ws_url`` skips them), so AssemblyAI applies its own default
    rather than receiving a zero.
    """
    # None means "leave this parameter off the URL entirely", which is what
    # _build_ws_url does with it (`if v is not None`) and what makes AssemblyAI
    # apply its own default.
    #
    # formatted_finals and format_turns are annotated `bool | _NotGiven` with
    # no None member, but NOT_GIVEN is NOT equivalent here: it resolves to
    # pipecat's own default and the parameter appears on the wire as
    # `formatted_finals=true`. Only None omits it, so the cast keeps the
    # correct runtime behaviour against a narrower-than-reality annotation.
    settings = AssemblyAISTTService.Settings(
        model=config.model,
        keyterms_prompt=config.keyterms_prompt,
        prompt=config.prompt,
        end_of_turn_confidence_threshold=config.end_of_turn_confidence_threshold,
        min_turn_silence=config.min_turn_silence,
        max_turn_silence=config.max_turn_silence,
        formatted_finals=cast(bool, config.formatted_finals),
        format_turns=cast(bool, config.format_turns),
        language_detection=config.language_detection,
        speaker_labels=config.speaker_labels,
        vad_threshold=config.vad_threshold,
        word_finalization_max_wait_time=config.word_finalization_max_wait_time,
        domain=config.domain,
    )

    logger.info(
        "Using AssemblyAI Universal Streaming STT (model={}, "
        "vad_force_turn_endpoint={}, min/max_turn_silence={}/{}, "
        "languages={}, keyterms={}, sample_rate={})",
        config.model,
        config.vad_force_turn_endpoint,
        config.min_turn_silence,
        config.max_turn_silence,
        config.language_codes or None,
        len(config.keyterms_prompt) if config.keyterms_prompt else 0,
        config.sample_rate,
    )
    return AssemblyAISTTServiceWithLanguageCodes(
        language_codes=config.language_codes,
        api_key=config.api_key,
        vad_force_turn_endpoint=config.vad_force_turn_endpoint,
        # cast: pipecat types this `int` but hands it straight to
        # WebsocketSTTService, whose `_init_sample_rate or
        # frame.audio_in_sample_rate` is exactly the None-means-inherit
        # behaviour we want. The annotation is narrower than the code.
        sample_rate=cast(int, config.sample_rate),
        settings=settings,
    )
