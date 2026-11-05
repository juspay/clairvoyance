"""ElevenLabs Scribe v2 Realtime STT config and builder.

Wraps pipecat's :class:`ElevenLabsRealtimeSTTService` (WebSocket streaming,
model ``scribe_v2_realtime``). Audio is streamed as base64 PCM over a
WebSocket; Scribe pushes ``partial_transcript`` (interim) and
``committed_transcript`` (final) that pipecat surfaces as
``InterimTranscriptionFrame`` / ``TranscriptionFrame``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional, cast

from pipecat.services.elevenlabs.stt import (
    CommitStrategy,
    ElevenLabsRealtimeSTTService,
)

from app.core.logger import logger

__all__ = [
    "ElevenLabsConfig",
    "resolve_languages",
    "ElevenLabsRealtimeSTTServiceWithSecondaryLanguages",
    "build_elevenlabs_stt",
]


class ElevenLabsRealtimeSTTServiceWithSecondaryLanguages(ElevenLabsRealtimeSTTService):
    """ElevenLabs realtime STT that can also send ``secondary_languages``.

    pipecat 1.1.0 never emits ``secondary_languages``, Scribe v2 Realtime's
    constrained-decoding parameter. On a Hinglish call ``language_code=hi``
    alone makes English an illegal hypothesis, so English words get forced
    into Devanagari or hallucinated outright; omitting the language entirely
    sends Scribe searching 90+ languages and it has been observed returning
    Hindi speech as Russian Cyrillic.
    ``language_code=hi&secondary_languages=en`` keeps the Hindi prior while
    letting English tokens through.

    The parameter is *repeated* on the wire
    (``secondary_languages=hi&secondary_languages=en``), so it cannot be
    carried by any single pipecat setting. Rather than duplicate pipecat's
    whole URL builder — which would drift silently on upgrade — this appends
    the extra pairs to ``settings.language`` for the duration of the parent
    call and restores it afterwards.

    That trick depends on pipecat interpolating the value raw:
    ``params.append(f"language_code={self._settings.language}")`` with no
    ``quote()``. ``test_secondary_languages_reach_the_url`` asserts the exact
    query string so this fails loudly if pipecat ever starts URL-encoding —
    an encoded ``&`` (``%26``) would otherwise make the connect fail silently.
    """

    def __init__(self, *, secondary_languages: list[str] | None = None, **kwargs):
        """Store the secondary language codes; everything else is pipecat's.

        Args:
            secondary_languages: Extra ISO-639-1/639-3 codes Scribe may decode
                alongside ``settings.language``. Ignored when empty, or when
                no primary ``language_code`` is set (Scribe rejects secondaries
                without a primary — auto-detect already spans all languages).
            **kwargs: Passed through to ``ElevenLabsRealtimeSTTService``.
        """
        super().__init__(**kwargs)
        self._secondary_languages = secondary_languages or []
        # Serialises the _settings.language swap in _connect_websocket. Two
        # independent tasks reach that method: the frame task (run_stt ->
        # _connect) and the receive task (_receive_task_handler ->
        # _maybe_try_reconnect -> _try_reconnect -> _reconnect_websocket).
        # _connected_event is only touched inside _connect(), so it does not
        # gate the reconnect path, and _reconnect_in_progress guards
        # _try_reconnect against itself only — neither protects the two paths
        # from each other. Without this, the second entrant reads the
        # already-smuggled value as its "original" and restores that instead,
        # leaving settings.language permanently wrong for the rest of the call.
        self._connect_lock = asyncio.Lock()

    @property
    def requires_vad_analyzer(self) -> bool:
        """Whether turn endpoints depend on a VAD analyzer being in the pipeline.

        Under ``CommitStrategy.MANUAL`` pipecat commits a turn only when it
        sees a ``VADUserStoppedSpeakingFrame``; with no VAD upstream that frame
        never arrives, so Scribe streams interim transcripts forever and never
        emits a final. ``turn_detection='smart_turn'`` auto-creates a Silero
        VAD, but ``'timeout'`` does not — this is what makes the pipeline warn
        instead of going silently deaf. Under ``VAD`` commit Scribe closes its
        own turns and no analyzer is needed.
        """
        return self._commit_strategy == CommitStrategy.MANUAL

    def _language_query_value(self) -> str:
        """Build the raw value pipecat will interpolate after ``language_code=``.

        Returns the primary code alone, or the primary followed by one
        ``&secondary_languages=<code>`` pair per secondary.

        Codes are rejected unless they are pure letters. pipecat interpolates
        this value without ``quote()`` — that is what lets the extra pairs
        through at all, but it equally means an ``&`` inside a code would be
        read as a real separator: ``["en&enable_logging=true"]`` would silently
        switch on logging at ElevenLabs. ISO-639-1/639-3 codes are letters
        only, so anything else is a malformed template, not a language.
        """
        value = str(self._settings.language)
        for lang in self._secondary_languages:
            if not lang.isalpha():
                logger.warning(
                    "ElevenLabs Scribe: dropping malformed secondary language "
                    "{!r} — ISO-639 codes are letters only",
                    lang,
                )
                continue
            value += f"&secondary_languages={lang}"
        return value

    async def _connect_websocket(self):
        # pipecat's own URL log sits at DEBUG, which is usually off in
        # production — this is the INFO-level record of what was actually
        # negotiated, including the audio_format that is only resolved from
        # the transport at start().
        logger.info(
            "ElevenLabs Scribe connect: model={} language={} secondary={} "
            "commit={} audio_format={}",
            self._settings.model,
            self._settings.language,
            self._secondary_languages or None,
            self._commit_strategy.value,
            self._audio_format,
        )

        # enable_logging=False can never reach the wire: pipecat guards on
        # truthiness (`if self._enable_logging:`, stt.py:772), so the privacy
        # default is silently dropped and ElevenLabs applies its own. The
        # string "false" is truthy, so the guard fires, and pipecat renders it
        # with str(...).lower() -> "enable_logging=false". This is a pipecat
        # STT bug, not an API shape: the same release's ElevenLabs TTS uses
        # `if self._enable_logging is not None:` (tts.py:705) and does send an
        # explicit false. Remove this once pipecat is patched.
        if self._enable_logging is False:
            # cast, not a bool: the value is deliberately the STRING "false".
            # pipecat types the attribute bool, but only ever reads it as
            # `if x:` then `str(x).lower()` — so a truthy string is what gets
            # the parameter emitted with the right value.
            self._enable_logging = cast(bool, "false")

        if not (self._secondary_languages and self._settings.language):
            await super()._connect_websocket()
            return

        # The swap spans a TLS + WebSocket handshake, so without the lock a
        # concurrent reconnect can read the smuggled value as its own
        # `original`. Whoever restores last wins, and the loser's correct
        # value is lost. Bonus: this also serialises pipecat's double-connect
        # window for this service.
        async with self._connect_lock:
            original = self._settings.language
            self._settings.language = self._language_query_value()
            try:
                await super()._connect_websocket()
            finally:
                self._settings.language = original


# ISO-639-1/639-3 is two or three ASCII letters. A regex, not str.isalpha():
# isalpha() is Unicode-aware, so the fullwidth "ｅｎ" passes it, and it rejects the
# region-tagged codes this repo uses routinely (en-IN / hi-IN at
# template/types.py:486,503,517).
_LANG_CODE = re.compile(r"[A-Za-z]{2,3}")


def _clean_lang(code: str) -> Optional[str]:
    """Normalise one language code, or return None if it is not one.

    ``"en-IN"`` -> ``"en"``; ``"en&enable_logging=true"`` -> ``None``.

    This is the security boundary for the whole provider. pipecat builds the
    Scribe URL with a raw f-string and no ``quote()``
    (pipecat/services/elevenlabs/stt.py:763), so an ``&`` inside a language
    code becomes a real query separator and injects a parameter of the
    attacker's choosing -- ``enable_logging=true`` switches on ElevenLabs-side
    retention of call audio and transcripts. Unlike Soniox (JSON body) or
    Deepgram (SDK urlencodes), this provider has no escaping in front of it.

    The value is not template-only: /stt/stream builds STTConfiguration
    straight from the client's first message (breeze_buddy/stt/handlers.py:221)
    and POST /signup is public self-service.
    """
    short = str(code).split("-")[0].strip()
    return short.lower() if _LANG_CODE.fullmatch(short) else None


def resolve_languages(
    language_code: str | None,
    secondary_languages: list[str] | None,
    language: str | list[str] | None,
) -> tuple[str | None, list[str]]:
    """Resolve Scribe's primary ``language_code`` + ``secondary_languages``.

    Scribe v2 Realtime takes ONE primary code plus any number of secondaries
    (repeated ``secondary_languages=`` params) for constrained decoding.
    Omitting the primary entirely puts it in auto-detect across 90+ languages.

    Three inputs feed this, in precedence order:

    - ``language_code`` — explicit nested ``elevenlabs.language_code``
    - ``secondary_languages`` — explicit nested ``elevenlabs.secondary_languages``
    - ``language`` — the template's top-level, shared with every other
      provider (``"hi"`` or ``["hi", "en"]``)

    Returns:
        ``(primary, secondaries)``. ``primary`` is ``None`` for auto-detect,
        in which case ``secondaries`` must be empty — Scribe rejects
        secondaries with no primary, and auto-detect already spans everything.
    """
    top_level: list[str] = []
    if isinstance(language, list):
        top_level = [lang for lang in language if lang]
    elif language:
        top_level = [language]

    # Each nested field overrides its own slot independently; the top-level
    # list is the fallback for both. Reading `language` here is safe for the
    # other providers — they read the same field themselves and are unaffected
    # by who else looks at it. What would affect them is *editing* it, so an
    # ElevenLabs-only template should set the nested fields instead.
    primary_raw = language_code or (top_level[0] if top_level else None)
    primary: str | None = _clean_lang(primary_raw) if primary_raw else None
    if primary_raw and primary is None:
        logger.warning(
            "ElevenLabs Scribe: dropping malformed primary language {!r} — "
            "ISO-639 codes are two or three ASCII letters",
            primary_raw,
        )

    candidates = secondary_languages if secondary_languages is not None else top_level

    # Every code is normalised the same way, so "en-IN" and "hi-IN" behave
    # identically whichever slot they land in. A code can never be both
    # primary and secondary on the wire, and repeats would emit duplicate
    # `secondary_languages=` params.
    seen: set[str] = set()
    secondaries: list[str] = []
    for lang in candidates:
        if not lang:
            continue
        clean = _clean_lang(lang)
        if clean is None:
            logger.warning(
                "ElevenLabs Scribe: dropping malformed secondary language {!r}",
                lang,
            )
            continue
        if clean != primary and clean not in seen:
            seen.add(clean)
            secondaries.append(clean)

    if primary is None and secondaries:
        logger.warning(
            "ElevenLabs Scribe: secondary_languages {} ignored — no primary "
            "language_code to constrain decoding against",
            secondaries,
        )
        secondaries = []
    return primary, secondaries


@dataclass
class ElevenLabsConfig:
    """Configuration for ElevenLabs Scribe v2 Realtime STT.

    Only parameters pipecat 1.1.0 exposes on the realtime WebSocket service
    are surfaced here. ``keyterms``, ``no_verbatim``, and token-based auth are
    not yet part of pipecat and are out of scope.

    ``commit_strategy`` drives how turns are segmented:
    - ``CommitStrategy.MANUAL``: pipecat controls commit points (e.g. via a
      Silero VAD user-turn-stop / SmartTurn analyzer). Best paired with
      ``turn_detection='smart_turn'``.
    - ``CommitStrategy.VAD``: Scribe's own cloud VAD commits turns. Best
      paired with ``turn_detection='stt_native'``. The ``vad_*`` settings only
      apply in this mode.
    """

    api_key: str
    # Bare host, no scheme — pipecat builds ``wss://{base_url}/v1/...`` itself,
    # so a value carrying ``wss://`` yields ``wss://wss://...`` and fails every
    # handshake.
    #
    # Deliberately has NO default. A key is only accepted by the account it
    # belongs to, so the host is never a detail the caller may skip: a default
    # pointing at the worldwide host would let a caller pair a residency key
    # with it and get a 401 on every call, with nothing red until a customer
    # is on the line. Required here, that mistake is a TypeError at import.
    base_url: str
    commit_strategy: CommitStrategy = CommitStrategy.MANUAL
    model: str = "scribe_v2_realtime"
    # Leave None so pipecat inherits the transport's rate (8 kHz telephony,
    # 16 kHz web). Pinning it here overrides the pipeline and mislabels the
    # audio — see build_elevenlabs_stt.
    sample_rate: int | None = None
    language_code: Optional[str] = None
    # Constrained decoding: Scribe may also decode these alongside
    # language_code. Only meaningful when language_code is set.
    secondary_languages: Optional[list[str]] = None
    include_language_detection: bool = False
    include_timestamps: bool = False
    enable_logging: bool = False
    vad_silence_threshold_secs: Optional[float] = None
    vad_threshold: Optional[float] = None
    min_speech_duration_ms: Optional[int] = None
    min_silence_duration_ms: Optional[int] = None


def build_elevenlabs_stt(
    config: ElevenLabsConfig,
) -> ElevenLabsRealtimeSTTServiceWithSecondaryLanguages:
    """Create an ElevenLabs Scribe v2 Realtime STT service.

    ``settings=ElevenLabsRealtimeSTTService.Settings(...)`` is the canonical
    way to set model/language/VAD params in pipecat; the deprecated top-level
    kwargs are avoided.

    ``sample_rate`` is normally left ``None``: pipecat then resolves it from
    the pipeline at ``start()`` (``_init_sample_rate or
    frame.audio_in_sample_rate``) and derives ``audio_format`` from it, so the
    same service works on 8 kHz telephony and 16 kHz web. Passing an explicit
    rate wins over the transport's — a mismatch makes Scribe decode the audio
    at the wrong speed and return garbage.
    """
    settings = ElevenLabsRealtimeSTTService.Settings(
        model=config.model,
        language=config.language_code,
        vad_silence_threshold_secs=config.vad_silence_threshold_secs,
        vad_threshold=config.vad_threshold,
        min_speech_duration_ms=config.min_speech_duration_ms,
        min_silence_duration_ms=config.min_silence_duration_ms,
    )

    logger.info(
        "Using ElevenLabs Scribe v2 Realtime STT (model={}, commit_strategy={}, "
        "language={}, secondary_languages={}, sample_rate={}, base_url={})",
        config.model,
        config.commit_strategy.value,
        config.language_code,
        config.secondary_languages,
        config.sample_rate,
        config.base_url,
    )
    return ElevenLabsRealtimeSTTServiceWithSecondaryLanguages(
        api_key=config.api_key,
        base_url=config.base_url,
        commit_strategy=config.commit_strategy,
        sample_rate=config.sample_rate,
        include_timestamps=config.include_timestamps,
        enable_logging=config.enable_logging,
        include_language_detection=config.include_language_detection,
        secondary_languages=config.secondary_languages,
        settings=settings,
    )
