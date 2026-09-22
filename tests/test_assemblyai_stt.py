"""Tests for AssemblyAI Universal Streaming (v3) STT integration.

Locks in: provider routing in ``create_stt_from_config``, the turn-mode ->
``vad_force_turn_endpoint`` mapping for all three enum members, the two guards
that would otherwise surface as a dead call (u3-pro coupling, prompt vs
keyterms), sample-rate inheritance from the transport, and the query string
actually built for a real config.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pydantic import ValidationError

import app.ai.voice.agents.breeze_buddy.stt as bb_stt_mod
from app.ai.voice.agents.breeze_buddy.stt import create_stt_from_config
from app.ai.voice.agents.breeze_buddy.template.types import (
    AssemblyAISTTConfig,
    STTConfiguration,
    STTProvider,
    TurnDetectionMode,
)
from app.ai.voice.stt import AssemblyAIConfig, build_assemblyai_stt
from app.ai.voice.stt.assemblyai import AssemblyAISTTServiceWithLanguageCodes


def _params(svc, sample_rate: int = 8000) -> dict[str, str]:
    """Exact query parameters of the URL the service would connect to.

    Parsed, not substring-matched: ``"max_turn_silence=100" in url`` is true
    for ``max_turn_silence=1000`` too, and ``speech_model=universal-3-5-pro``
    matches ``-preview``. parse_qs gives one exact value per key, so an
    assertion here can only pass on the value it names.
    """
    svc._sample_rate = sample_rate
    query = urlsplit(svc._build_ws_url()).query
    parsed = parse_qs(query, keep_blank_values=True)
    assert all(len(v) == 1 for v in parsed.values()), f"duplicate params: {query}"
    return {k: v[0] for k, v in parsed.items()}


def _json_param(params: dict[str, str], key: str) -> list[str]:
    return json.loads(params[key])


def test_assemblyai_provider_enum_value():
    assert STTProvider.ASSEMBLYAI.value == "assemblyai"


def test_config_defaults():
    cfg = AssemblyAISTTConfig()
    assert cfg.model == "universal-3-5-pro"
    assert cfg.keyterms_prompt is None
    assert cfg.prompt is None
    # AssemblyAI's documented voice-agent recommendation, not None: a None
    # here would override the builder's default and silently drop the tuning.
    assert cfg.min_turn_silence == 100
    assert cfg.max_turn_silence == 1000


def test_prompt_and_keyterms_are_mutually_exclusive():
    """AssemblyAI rejects both; pipecat raises in the service constructor,
    which on the voice path is a call that dies before any audio flows. The
    validator moves that failure to template-parse time, where it names the
    field."""
    with pytest.raises(ValidationError, match="cannot both be set"):
        AssemblyAISTTConfig.model_validate(
            {"prompt": "support call", "keyterms_prompt": ["refund"]}
        )
    # Either alone is fine.
    assert AssemblyAISTTConfig(prompt="support call").prompt == "support call"
    assert AssemblyAISTTConfig(keyterms_prompt=["refund"]).keyterms_prompt == ["refund"]


def test_documented_ranges_are_enforced():
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"min_turn_silence": 10})  # < 50
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"max_turn_silence": 20000})  # > 10000
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"end_of_turn_confidence_threshold": 1.5})
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"vad_threshold": -0.1})


def test_keyterms_capped_at_the_documented_hundred():
    AssemblyAISTTConfig(keyterms_prompt=["w"] * 100)
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"keyterms_prompt": ["w"] * 101})


# ---------------------------------------------------------------------------
# turn_detection -> vad_force_turn_endpoint, all three enum members
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [
        (TurnDetectionMode.STT_NATIVE, False),
        (TurnDetectionMode.SMART_TURN, True),
        (TurnDetectionMode.TIMEOUT, False),
    ],
)
async def test_turn_detection_maps_to_endpoint_mode(monkeypatch, mode, expected):
    """SMART_TURN is the only mode the pipeline auto-creates a Silero VAD for,
    so it is the only one that may force local endpointing. TIMEOUT must NOT:
    it gets no VAD, BREEZE_BUDDY_ENABLE_VAD defaults False, and without that
    frame AssemblyAI is never told to close a turn -- no final transcript for
    the whole call."""
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.ASSEMBLYAI, turn_detection=mode)
    )
    assert isinstance(svc, AssemblyAISTTService)
    assert svc._vad_force_turn_endpoint is expected


async def test_assemblyai_endpointing_requires_the_pro_model(monkeypatch):
    """pipecat raises ValueError for vad_force_turn_endpoint=False on a
    non-u3-pro model. Rejecting it here names the template field instead."""
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="Universal-3.5 Pro"):
        await create_stt_from_config(
            STTConfiguration(
                provider=STTProvider.ASSEMBLYAI,
                turn_detection=TurnDetectionMode.STT_NATIVE,
                assemblyai=AssemblyAISTTConfig(model="universal-streaming-english"),
            )
        )


async def test_non_pro_model_is_fine_under_smart_turn(monkeypatch):
    """The coupling only applies to AssemblyAI-side endpointing."""
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ASSEMBLYAI,
            turn_detection=TurnDetectionMode.SMART_TURN,
            assemblyai=AssemblyAISTTConfig(model="universal-streaming-english"),
        )
    )
    assert isinstance(svc, AssemblyAISTTService)


async def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "")
    with pytest.raises(ValueError, match="ASSEMBLYAI_API_KEY"):
        await create_stt_from_config(STTConfiguration(provider=STTProvider.ASSEMBLYAI))


async def test_legacy_env_provider_map_routes_assemblyai(monkeypatch):
    monkeypatch.setattr(bb_stt_mod, "BREEZE_BUDDY_STT_SERVICE", "assemblyai")
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "test-key")
    svc = await bb_stt_mod.get_stt_service()
    assert isinstance(svc, AssemblyAISTTService)


# ---------------------------------------------------------------------------
# The wire: what actually reaches AssemblyAI
# ---------------------------------------------------------------------------


def test_sample_rate_follows_the_transport():
    """The builder must NOT pin a rate. Telephony runs at 8 kHz and web at
    16 kHz; pipecat resolves it from the StartFrame in start(), which runs
    before _connect() builds the URL."""
    svc = build_assemblyai_stt(AssemblyAIConfig(api_key="k"))
    assert svc._init_sample_rate is None

    assert _params(svc, 8000)["sample_rate"] == "8000"
    assert _params(svc, 16000)["sample_rate"] == "16000"


def test_voice_agent_defaults_reach_the_url():
    """AssemblyAI's own recommendation for voice agents is 100/1000ms; pipecat
    leaves them unset, which applies a dictation-tuned default instead."""
    p = _params(build_assemblyai_stt(AssemblyAIConfig(api_key="k")))

    assert p["min_turn_silence"] == "100"
    assert p["max_turn_silence"] == "1000"
    assert p["speech_model"] == "universal-3-5-pro"


def test_unset_settings_are_omitted_not_zeroed():
    """A None must leave the parameter off the URL entirely so AssemblyAI
    applies its own default, rather than being sent as a zero."""
    p = _params(build_assemblyai_stt(AssemblyAIConfig(api_key="k")))

    assert "vad_threshold" not in p
    assert "speaker_labels" not in p
    assert "domain" not in p
    # formatted_finals/format_turns are typed `bool | _NotGiven` with no None
    # member, so it is tempting to pass NOT_GIVEN for "unset" -- but that
    # resolves to pipecat's own default and puts `formatted_finals=true` on
    # the wire. Only None omits it.
    assert "formatted_finals" not in p
    assert "format_turns" not in p


def test_explicitly_set_booleans_do_reach_the_url():
    svc = build_assemblyai_stt(AssemblyAIConfig(api_key="k", formatted_finals=True))
    assert _params(svc)["formatted_finals"] == "true"


def test_keyterms_are_json_encoded_on_the_wire():
    """AssemblyAI expects keyterms_prompt as a JSON array, urlencoded."""
    svc = build_assemblyai_stt(
        AssemblyAIConfig(api_key="k", keyterms_prompt=["order", "MacBook Pro"])
    )
    assert _json_param(_params(svc), "keyterms_prompt") == ["order", "MacBook Pro"]


def test_no_language_parameter_means_native_code_switching():
    """Universal-3.5 Pro is multilingual by default and code-switches
    mid-sentence across 18 languages. Sending a single language code would
    make the session MONOLINGUAL -- the failure that forced English into
    Devanagari on the ElevenLabs path. pipecat 1.1.0 exposes no
    language_codes, so the default is the correct behaviour here."""
    p = _params(build_assemblyai_stt(AssemblyAIConfig(api_key="k")))

    assert "language_codes" not in p
    assert "language_code" not in p


async def test_voice_agent_tuning_survives_the_template_path(monkeypatch):
    """Regression: AssemblyAISTTConfig's defaults feed AssemblyAIConfig, so a
    None on the template model silently overrides the builder's 100/1000 and
    the recommended tuning never reaches the wire."""
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.ASSEMBLYAI)
    )
    assert isinstance(svc, AssemblyAISTTService)
    p = _params(svc)

    assert p["min_turn_silence"] == "100"
    assert p["max_turn_silence"] == "1000"


# ---------------------------------------------------------------------------
# language_codes: pipecat 1.1.0 has no such parameter, so we append it.
# ---------------------------------------------------------------------------


def test_language_codes_reach_the_url():
    """Hinglish steering. pipecat 1.1.0 exposes no language_codes at all, so
    the subclass appends it using 1.8.1's wire format: a JSON array,
    urlencoded with the rest of the query string."""
    svc = build_assemblyai_stt(
        AssemblyAIConfig(api_key="k", language_codes=["hi", "en"])
    )
    assert _json_param(_params(svc), "language_codes") == ["hi", "en"]


def test_language_codes_omitted_when_unset():
    """No steering means native code switching across all 18 languages, which
    is u3-rt-pro's default and a valid configuration."""
    assert "language_codes" not in _params(
        build_assemblyai_stt(AssemblyAIConfig(api_key="k"))
    )


def test_language_codes_cannot_inject_query_params():
    """urlencode escapes the JSON, so a malformed code cannot become a
    separate parameter -- unlike the ElevenLabs provider, where pipecat
    interpolates the value raw."""
    svc = build_assemblyai_stt(
        AssemblyAIConfig(api_key="k", language_codes=["hi&formatted_finals=true"])
    )
    p = _params(svc)

    # parse_qs would have split an unescaped & into a second key
    assert "formatted_finals" not in p
    assert _json_param(p, "language_codes") == ["hi&formatted_finals=true"]


def test_language_codes_capped_at_ten():
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"language_codes": ["en"] * 11})


async def test_language_codes_flow_from_template(monkeypatch):
    monkeypatch.setattr(bb_stt_mod, "ASSEMBLYAI_API_KEY", "test-key")
    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.ASSEMBLYAI,
            assemblyai=AssemblyAISTTConfig(language_codes=["hi", "en"]),
        )
    )
    assert isinstance(svc, AssemblyAISTTServiceWithLanguageCodes)
    assert _json_param(_params(svc), "language_codes") == ["hi", "en"]


# ---------------------------------------------------------------------------
# Model-name normalisation. Two names exist for one model and only one of
# them works on the wire.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_value",
    ["universal-3-5-pro", "u3-rt-pro", "universal-3-5-pro-preview", "u3-rt-pro-beta-1"],
)
def test_every_pro_spelling_reaches_the_wire_as_the_documented_name(template_value):
    """pipecat invented "u3-rt-pro" and gates on it; AssemblyAI only knows
    "universal-3-5-pro". A template written with pipecat's spelling must not
    reach the wire verbatim -- AssemblyAI does not recognise it and falls back
    to Universal-Streaming English, so a Hinglish call returns Latin-script
    romanisation with no error anywhere."""
    svc = build_assemblyai_stt(
        AssemblyAIConfig(
            api_key="k", model=template_value, vad_force_turn_endpoint=False
        )
    )
    assert _params(svc)["speech_model"] == "universal-3-5-pro"


def test_non_pro_models_pass_through_untouched():
    """Normalisation must not capture models that are not the pro family."""
    svc = build_assemblyai_stt(
        AssemblyAIConfig(
            api_key="k",
            model="universal-streaming-english",
            vad_force_turn_endpoint=True,
        )
    )
    assert _params(svc)["speech_model"] == "universal-streaming-english"


def test_vendor_string_limits_are_enforced_at_parse_time():
    """Over either limit AssemblyAI rejects the websocket at connect, which on
    the voice path is a call with no transcript at all."""
    AssemblyAISTTConfig(prompt="x" * 1750)
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"prompt": "x" * 1751})

    AssemblyAISTTConfig(keyterms_prompt=["y" * 50])
    with pytest.raises(ValidationError):
        AssemblyAISTTConfig.model_validate({"keyterms_prompt": ["y" * 51]})


def test_smart_turn_collapses_max_turn_silence():
    """pipecat forces max_turn_silence == min_turn_silence under local VAD
    endpointing, so the configured 1000 never reaches the wire in smart_turn
    mode. Pinned so the field description stays honest about it."""
    svc = build_assemblyai_stt(
        AssemblyAIConfig(
            api_key="k",
            vad_force_turn_endpoint=True,
            min_turn_silence=100,
            max_turn_silence=1000,
        )
    )
    p = _params(svc)

    assert p["min_turn_silence"] == "100"
    assert p["max_turn_silence"] == "100"  # exact: not 1000 — pipecat overrode it
