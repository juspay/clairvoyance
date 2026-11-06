"""Tests for ElevenLabs Scribe v2 Realtime STT integration.

Locks in: provider routing in ``create_stt_from_config``, the turn-mode ->
``commit_strategy`` mapping, Scribe VAD-parameter validation gating, the
missing-API-key guard, and end-to-end construction of pipecat's realtime
service from an ``ElevenLabsSTTConfig``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.services.elevenlabs.stt import (
    CommitStrategy,
    ElevenLabsRealtimeSTTService,
)
from pydantic import ValidationError

import app.ai.voice.agents.breeze_buddy.stt as bb_stt_mod
from app.ai.voice.agents.breeze_buddy.stt import create_stt_from_config
from app.ai.voice.agents.breeze_buddy.template.types import (
    ElevenLabsSTTConfig,
    STTConfiguration,
    STTProvider,
    TurnDetectionMode,
)
from app.ai.voice.stt import ElevenLabsConfig, build_elevenlabs_stt
from app.ai.voice.stt.elevenlabs import (
    ElevenLabsRealtimeSTTServiceWithSecondaryLanguages,
    resolve_languages,
)
from app.core.config import static

# pipecat's own default host, asserted rather than inlined so a pipecat upgrade
# that changes it fails loudly here instead of silently on a live call.
GLOBAL_HOST = "api.elevenlabs.io"
RESIDENCY_HOST = "api.in.residency.elevenlabs.io"


def test_elevenlabs_provider_enum_value():
    assert STTProvider.ELEVENLABS.value == "elevenlabs"


def test_elevenlabs_stt_config_defaults():
    cfg = ElevenLabsSTTConfig()
    assert cfg.model == "scribe_v2_realtime"
    assert cfg.language_code is None
    assert cfg.include_language_detection is False
    assert cfg.include_timestamps is False
    assert cfg.enable_logging is False
    assert cfg.vad_silence_threshold_secs is None
    assert cfg.vad_threshold is None
    assert cfg.min_speech_duration_ms is None
    assert cfg.min_silence_duration_ms is None


def test_elevenlabs_stt_config_accepts_full_schema():
    parsed = ElevenLabsSTTConfig.model_validate(
        {
            "model": "scribe_v2_realtime",
            "language_code": "hi",
            "include_language_detection": True,
            "include_timestamps": True,
            "enable_logging": True,
            "vad_silence_threshold_secs": 1.0,
            "vad_threshold": 0.5,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": 300,
        }
    )
    assert parsed.language_code == "hi"
    assert parsed.vad_silence_threshold_secs == 1.0


def test_elevenlabs_vad_params_range_validation():
    with pytest.raises(ValidationError):
        ElevenLabsSTTConfig.model_validate({"vad_silence_threshold_secs": 0.2})
    with pytest.raises(ValidationError):
        ElevenLabsSTTConfig.model_validate({"vad_silence_threshold_secs": 3.1})
    with pytest.raises(ValidationError):
        ElevenLabsSTTConfig.model_validate({"vad_threshold": 0.05})
    with pytest.raises(ValidationError):
        ElevenLabsSTTConfig.model_validate({"vad_threshold": 0.95})
    with pytest.raises(ValidationError):
        ElevenLabsSTTConfig.model_validate({"min_speech_duration_ms": 25})
    with pytest.raises(ValidationError):
        ElevenLabsSTTConfig.model_validate({"min_silence_duration_ms": 2500})


async def test_stt_native_maps_to_vad_commit_strategy(monkeypatch):
    """STT_NATIVE -> Scribe cloud VAD commits turns (no local VAD).
    The VAD params must flow into the service settings even though the config
    leaves them null by default."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            turn_detection=TurnDetectionMode.STT_NATIVE,
            elevenlabs=ElevenLabsSTTConfig(
                vad_silence_threshold_secs=1.0, vad_threshold=0.5
            ),
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._commit_strategy == CommitStrategy.VAD
    assert svc._settings.vad_silence_threshold_secs == 1.0
    assert svc._settings.vad_threshold == 0.5


async def test_smart_turn_maps_to_manual_commit_strategy(monkeypatch):
    """SMART_TURN -> local Silero/SmartTurn commits; Scribe stays manual.
    VAD settings remain unset on the service (they only apply in VAD mode)."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            turn_detection=TurnDetectionMode.SMART_TURN,
            elevenlabs=ElevenLabsSTTConfig(
                vad_silence_threshold_secs=1.0, vad_threshold=0.5
            ),
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._commit_strategy == CommitStrategy.MANUAL


async def test_default_turn_detection_maps_to_vad_commit(monkeypatch):
    """Default turn_detection is STT_NATIVE, whose mapped commit_strategy is
    VAD, so a fully-unset config must still land on VAD commit."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.ELEVENLABS)
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._commit_strategy == CommitStrategy.VAD


async def test_language_and_timestamps_flow_to_service(monkeypatch):
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            elevenlabs=ElevenLabsSTTConfig(
                language_code="hi",
                include_language_detection=True,
                include_timestamps=True,
            ),
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._settings.language == "hi"
    assert svc._include_language_detection is True
    assert svc._include_timestamps is True


async def test_sample_rate_left_to_the_pipeline(monkeypatch):
    """The builder must NOT pin a sample rate.

    Telephony transports run at 8 kHz (``audio_in_sample_rate=
    TELEPHONY_SAMPLE_RATE``) while web runs at 16 kHz. pipecat resolves the
    rate at ``start()`` via ``_init_sample_rate or frame.audio_in_sample_rate``
    — an explicit value short-circuits that, so Scribe would be told
    ``audio_format=pcm_16000`` while receiving 8 kHz bytes and decode the call
    at the wrong speed."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    captured = {}
    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")

    def fake_build(config):
        captured["sample_rate"] = config.sample_rate
        return ElevenLabsRealtimeSTTService(
            api_key=config.api_key,
            commit_strategy=config.commit_strategy,
            sample_rate=config.sample_rate,
            settings=ElevenLabsRealtimeSTTService.Settings(model=config.model),
        )

    monkeypatch.setattr(bb_stt, "build_elevenlabs_stt", fake_build)

    svc = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.ELEVENLABS)
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert captured["sample_rate"] is None
    assert svc._init_sample_rate is None


async def test_language_code_none_flows_auto_detect(monkeypatch):
    """language_code=None is the auto-detect default; it must reach the
    service settings as None (not be replaced by a fallback)."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            elevenlabs=ElevenLabsSTTConfig(language_code=None),
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._settings.language is None


async def test_legacy_env_provider_map_routes_elevenlabs(monkeypatch):
    """BREEZE_BUDDY_STT_SERVICE='elevenlabs' must route through the legacy
    env path to the ElevenLabs realtime service."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(bb_stt, "BREEZE_BUDDY_STT_SERVICE", "elevenlabs")
    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await bb_stt.get_stt_service()
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._commit_strategy == CommitStrategy.VAD


def test_languages_list_splits_into_primary_and_secondaries():
    """['hi','en'] is the Hinglish case: Hindi leads, English stays legal."""
    assert resolve_languages(None, None, ["hi", "en"]) == ("hi", ["en"])


def test_languages_single_value_has_no_secondaries():
    assert resolve_languages(None, None, "hi") == ("hi", [])
    assert resolve_languages(None, None, ["hi"]) == ("hi", [])


def test_languages_none_stays_auto_detect():
    assert resolve_languages(None, None, None) == (None, [])
    assert resolve_languages(None, None, []) == (None, [])


def test_languages_explicit_nested_overrides_top_level():
    assert resolve_languages("te", None, ["hi", "en"])[0] == "te"
    assert resolve_languages("hi", ["ta"], ["hi", "en"]) == ("hi", ["ta"])


def test_languages_primary_never_repeats_in_secondaries():
    """A code cannot be both the primary and a secondary on the wire."""
    primary, secondaries = resolve_languages(None, None, ["hi", "en", "hi"])
    assert primary == "hi"
    assert primary not in secondaries


def test_languages_secondaries_dropped_without_a_primary():
    """Scribe rejects secondaries with no primary; auto-detect spans all anyway."""
    assert resolve_languages(None, ["en"], None) == (None, [])


async def test_secondary_languages_reach_the_url(monkeypatch):
    """The repeated-param smuggle must survive pipecat's URL build.

    pipecat has no API for ``secondary_languages``, so the service appends
    ``&secondary_languages=<code>`` pairs onto ``settings.language`` and lets
    pipecat interpolate them raw. If pipecat ever URL-encodes that value the
    ``&`` becomes ``%26`` and Scribe silently gets one nonsense language code
    instead of two real ones — this pins the exact query string so that
    change fails here rather than on a live call."""
    import pipecat.services.elevenlabs.stt as el_stt

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", fake_connect)

    svc = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="test-key",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
            language_code="hi",
            secondary_languages=["en"],
        )
    )
    svc._audio_format = "pcm_16000"  # normally set in start()
    await svc._connect_websocket()

    assert captured["url"] == (
        "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
        "?model_id=scribe_v2_realtime"
        "&language_code=hi"
        "&secondary_languages=en"
        "&audio_format=pcm_16000"
        "&commit_strategy=vad"
        # The privacy default, forced past pipecat's truthiness guard.
        "&enable_logging=false"
    )
    # settings must be restored — a second connect cannot double-append
    assert svc._settings.language == "hi"


async def test_no_secondary_languages_leaves_url_untouched(monkeypatch):
    import pipecat.services.elevenlabs.stt as el_stt

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", fake_connect)

    svc = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="test-key",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
            language_code="hi",
        )
    )
    svc._audio_format = "pcm_8000"
    await svc._connect_websocket()

    assert "secondary_languages" not in captured["url"]
    assert "language_code=hi" in captured["url"]


async def test_languages_flow_from_template_to_service(monkeypatch):
    """End-to-end: the template's ['hi','en'] lands on the built service."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            language=["hi", "en"],
            elevenlabs=ElevenLabsSTTConfig(),
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTServiceWithSecondaryLanguages)
    assert svc._settings.language == "hi"
    assert svc._secondary_languages == ["en"]


async def test_malformed_secondary_language_cannot_inject_params(monkeypatch):
    """A non-alpha code must never reach the URL.

    pipecat interpolates the value raw, so an ``&`` inside a language code
    would be read as a real query separator — ``"en&enable_logging=true"``
    would switch on logging at ElevenLabs without anyone asking."""
    import pipecat.services.elevenlabs.stt as el_stt

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", fake_connect)

    svc = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="test-key",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
            language_code="hi",
            secondary_languages=["en&enable_logging=true", "ta"],
        )
    )
    svc._audio_format = "pcm_8000"
    await svc._connect_websocket()

    # Narrowed deliberately: asserting the parameter is absent entirely would
    # encode the pipecat truthiness bug as the expected state, so fixing it
    # would read as a regression. What must never appear is the INJECTED value.
    assert "enable_logging=true" not in captured["url"]
    assert "secondary_languages=ta" in captured["url"]
    assert svc._settings.language == "hi"


def test_requires_vad_analyzer_only_under_manual_commit():
    """MANUAL commit fires only on VADUserStoppedSpeakingFrame — without a VAD
    upstream the service never produces a final, so the pipeline must warn."""
    manual = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="k",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.MANUAL,
        )
    )
    vad = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="k",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
        )
    )
    assert manual.requires_vad_analyzer is True
    assert vad.requires_vad_analyzer is False


# ---------------------------------------------------------------------------
# Language validation: the security boundary for this provider.
# ---------------------------------------------------------------------------


def test_primary_language_injection_is_dropped():
    """The guard must live in resolve_languages, not _language_query_value.

    With no secondaries, _connect_websocket early-returns before the smuggle
    helper ever runs — so a primary-only injection bypasses that guard
    entirely. /stt/stream builds STTConfiguration straight from the client's
    first message, so this value is not template-authored."""
    primary, secondaries = resolve_languages(None, None, "en&enable_logging=true")
    assert primary is None
    assert secondaries == []


def test_region_tagged_codes_normalise_the_same_in_both_slots():
    """en-IN/hi-IN are used routinely in this repo. Before this, the primary
    went to the wire raw while an identically-shaped secondary was silently
    dropped by isalpha()."""
    assert resolve_languages(None, None, ["hi-IN", "en-IN"]) == ("hi", ["en"])


def test_unicode_lookalikes_are_rejected():
    """str.isalpha() is Unicode-aware, so fullwidth 'ｅｎ' passes it. The
    regex is ASCII-only."""
    assert resolve_languages(None, None, "ｅｎ") == (None, [])


async def test_primary_injection_never_reaches_the_url(monkeypatch):
    import pipecat.services.elevenlabs.stt as el_stt

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", fake_connect)
    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")

    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            language="en&enable_logging=true",
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTServiceWithSecondaryLanguages)
    svc._audio_format = "pcm_8000"
    await svc._connect_websocket()

    assert "enable_logging=true" not in captured["url"]
    assert "language_code" not in captured["url"]


# ---------------------------------------------------------------------------
# turn_detection: TIMEOUT is the third enum member the old ternary missed.
# ---------------------------------------------------------------------------


async def test_timeout_mode_uses_vad_commit(monkeypatch):
    """Under MANUAL, pipecat commits only on a VADUserStoppedSpeakingFrame.
    TIMEOUT gets no VAD (only SMART_TURN auto-creates one) and
    BREEZE_BUDDY_ENABLE_VAD defaults False — so MANUAL here means no final
    transcript for the entire call."""
    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ELEVENLABS,
            turn_detection=TurnDetectionMode.TIMEOUT,
        )
    )
    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._commit_strategy == CommitStrategy.VAD


# ---------------------------------------------------------------------------
# enable_logging: the privacy default must actually reach the wire.
# ---------------------------------------------------------------------------


async def test_enable_logging_false_reaches_the_wire(monkeypatch):
    """pipecat guards on truthiness, so a plain False is dropped and
    ElevenLabs applies its own retention default. The string "false" is
    truthy and renders as enable_logging=false."""
    import pipecat.services.elevenlabs.stt as el_stt

    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", fake_connect)

    svc = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="test-key",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
            language_code="hi",
            enable_logging=False,
        )
    )
    svc._audio_format = "pcm_8000"
    await svc._connect_websocket()

    assert "enable_logging=false" in captured["url"]


# ---------------------------------------------------------------------------
# Concurrent connects: the frame task and the receive task both reach
# _connect_websocket, and the swap spans a handshake.
# ---------------------------------------------------------------------------


async def test_concurrent_connects_do_not_poison_settings(monkeypatch):
    """Two tasks enter _connect_websocket; settings.language must survive.

    pipecat reaches this method from two places — run_stt -> _connect (frame
    task) and _receive_task_handler -> _try_reconnect -> _reconnect_websocket
    (receive task). _connected_event is only touched inside _connect() and
    _reconnect_in_progress guards _try_reconnect against itself, so neither
    protects the two paths from each other. Without the lock the second
    entrant captures the already-smuggled value as its `original` and, if it
    restores last, leaves settings.language permanently wrong."""
    import pipecat.services.elevenlabs.stt as el_stt

    urls = []

    async def slow_connect(url, **kwargs):
        urls.append(url)
        # Hold the "handshake" open so the second task interleaves.
        await asyncio.sleep(0.02)
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", slow_connect)

    svc = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="test-key",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
            language_code="hi",
            secondary_languages=["en"],
        )
    )
    svc._audio_format = "pcm_8000"

    await asyncio.gather(
        svc._connect_websocket(),
        svc._connect_websocket(),
    )

    assert svc._settings.language == "hi"
    # Neither connect may carry a doubled secondary.
    for url in urls:
        assert url.count("secondary_languages=en") == 1


async def test_repeated_reconnects_never_accumulate(monkeypatch):
    """Sequential reconnects are the common case — the value must stay flat."""
    import pipecat.services.elevenlabs.stt as el_stt

    urls = []

    async def fake_connect(url, **kwargs):
        urls.append(url)
        return AsyncMock()

    monkeypatch.setattr(el_stt, "websocket_connect", fake_connect)

    svc = build_elevenlabs_stt(
        ElevenLabsConfig(
            api_key="test-key",
            base_url=GLOBAL_HOST,
            commit_strategy=CommitStrategy.VAD,
            language_code="hi",
            secondary_languages=["en"],
        )
    )
    svc._audio_format = "pcm_8000"

    for _ in range(5):
        await svc._connect_websocket()

    assert svc._settings.language == "hi"
    assert all(u.count("secondary_languages=en") == 1 for u in urls)


# ── The account: key and host always travel together ──────────────────────
#
# The bug these cover: STT sent ELEVENLABS_API_KEY to api.elevenlabs.io while
# TTS had long used the India-residency key and host. On a residency-
# provisioned account that is an auth failure on every call, while TTS kept
# working — the bot spoke and never heard a word. Switching accounts is now an
# env change, so there is no flag to get wrong: whatever key is configured is
# sent to whatever host is configured, and the two are read from one place.


def test_service_urls_are_bare_hosts():
    """ELEVENLABS_STT_URL and ELEVENLABS_TTS_URL must carry NO scheme.

    Each consumer supplies its own prefix — pipecat's STT builds
    ``wss://{base_url}/v1/...``, the TTS stream prepends ``wss://`` and the TTS
    REST path ``https://``. A scheme stored in the env would serve one of the
    three and produce ``wss://https://...`` for the others, failing every call.
    """
    from app.core.config.static import ELEVENLABS_STT_URL, ELEVENLABS_TTS_URL

    assert "://" not in ELEVENLABS_STT_URL
    assert "://" not in ELEVENLABS_TTS_URL


async def test_configured_key_and_host_both_reach_the_service(monkeypatch):
    """Both halves come from the env, and both must arrive.

    A key is only accepted by the account it belongs to, so a correct key at
    the wrong host is the same 401 as a wrong key — asserting one without the
    other would miss half the bug.
    """
    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "residency-key")
    monkeypatch.setattr(static, "ELEVENLABS_STT_URL", RESIDENCY_HOST)

    svc = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.ELEVENLABS)
    )

    assert isinstance(svc, ElevenLabsRealtimeSTTService)
    assert svc._api_key == "residency-key"
    assert svc._base_url == RESIDENCY_HOST


async def test_missing_residency_key_raises_at_build(monkeypatch):
    """Fail at build, not at the WebSocket handshake.

    An empty key used to reach the socket and 401 there — a live call that
    could not hear. A named ValueError at build time is a dead pod on deploy
    instead of a silently deaf customer call.
    """
    monkeypatch.setattr(static, "ELEVENLABS_STT_API_KEY", "")

    with pytest.raises(ValueError, match="ELEVENLABS_STT_API_KEY"):
        await create_stt_from_config(STTConfiguration(provider=STTProvider.ELEVENLABS))


def test_config_base_url_reaches_the_service_verbatim():
    """Whatever host the config names is the host the service dials.

    The builder must not substitute, normalise or drop it: pipecat renders it
    straight into ``wss://{base_url}/v1/...``, so any silent rewrite here is a
    connection to the wrong account, which is a 401 rather than an error the
    caller can see.
    """
    svc = build_elevenlabs_stt(ElevenLabsConfig(api_key="k", base_url=GLOBAL_HOST))
    assert svc._base_url == GLOBAL_HOST

    svc = build_elevenlabs_stt(ElevenLabsConfig(api_key="k", base_url=RESIDENCY_HOST))
    assert svc._base_url == RESIDENCY_HOST


def test_config_requires_an_explicit_base_url():
    """base_url has no default, so a caller cannot forget the host.

    This is the regression guard for the original bug: STT built a config
    without naming a host, inherited the worldwide one, and sent it a
    residency key. With no default that config will not construct.
    """
    with pytest.raises(TypeError):
        ElevenLabsConfig(api_key="k")  # type: ignore[call-arg]
