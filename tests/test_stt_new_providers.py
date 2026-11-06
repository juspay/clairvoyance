"""Smallest, Deepgram Flux, Sarvam language pinning, and the provider-neutral
``end_of_speech_ms``.

Every provider here runs on pipecat 1.1.0's own service. Locks in: routing,
the precedence rule (a provider's own field > end_of_speech_ms > today's
default), the save-time refusals (out of range, unsupported provider,
smart_turn, a field of the wrong Deepgram family), and the settings each
service is built with.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pipecat.services.smallest.stt import SmallestSTTService
from pydantic import ValidationError

import app.ai.voice.agents.breeze_buddy.stt as bb_stt_mod
from app.ai.voice.agents.breeze_buddy.stt import create_stt_from_config
from app.ai.voice.agents.breeze_buddy.template.types import (
    END_OF_SPEECH_RANGE_MS,
    STTConfiguration,
    STTProvider,
    set_by_template,
)
from app.ai.voice.stt import (
    DeepgramFluxConfig,
    DeepgramFluxSTTServiceWithInterims,
    SarvamConfig,
    SmallestConfig,
    build_deepgram_flux_stt,
    build_sarvam_stt,
    build_smallest_stt,
)
from app.core.config import static


def _capture(monkeypatch: pytest.MonkeyPatch, builder: str) -> dict[str, Any]:
    """Replace one builder in the routing module; return what it was given."""
    seen: dict[str, Any] = {}

    def fake(config: Any) -> str:
        seen["config"] = config
        return "svc"

    monkeypatch.setattr(bb_stt_mod, builder, fake)
    return seen


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SONIOX_API_KEY",
        "DEEPGRAM_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "ELEVENLABS_STT_API_KEY",
        "SMALLEST_API_KEY",
        "SARVAM_API_KEY",
    ):
        monkeypatch.setattr(static, key, "test-key")


# ---------------------------------------------------------------- routing


def test_smallest_provider_enum_value():
    assert STTProvider.SMALLEST.value == "smallest"


async def test_smallest_routes_with_template_settings(monkeypatch):
    seen = _capture(monkeypatch, "build_smallest_stt")
    await create_stt_from_config(
        STTConfiguration(provider="smallest", smallest={"numerals": False})
    )
    cfg: SmallestConfig = seen["config"]
    assert (cfg.language, cfg.numerals) == ("hi", False)


async def test_flux_model_routes_to_flux_not_nova(monkeypatch):
    flux = _capture(monkeypatch, "build_deepgram_flux_stt")
    monkeypatch.setattr(
        bb_stt_mod,
        "build_deepgram_stt",
        lambda c: pytest.fail("a flux-* model must not build Nova"),
    )
    await create_stt_from_config(
        STTConfiguration(
            provider="deepgram",
            language=["hi", "en"],
            deepgram={"model": "flux-general-multi", "eot_threshold": 0.8},
        )
    )
    cfg: DeepgramFluxConfig = flux["config"]
    assert cfg.model == "flux-general-multi"
    assert cfg.language_hints == ["hi", "en"]
    assert cfg.eot_threshold == 0.8
    assert cfg.mip_opt_out is True


async def test_nova_stays_the_deepgram_default(monkeypatch):
    nova = _capture(monkeypatch, "build_deepgram_stt")
    await create_stt_from_config(STTConfiguration(provider="deepgram"))
    assert nova["config"].model == "nova-3-general"
    assert nova["config"].endpointing == 25


async def test_legacy_env_map_knows_smallest(monkeypatch):
    seen = _capture(monkeypatch, "build_smallest_stt")
    monkeypatch.setattr(bb_stt_mod, "BREEZE_BUDDY_STT_SERVICE", "smallest")
    await bb_stt_mod.get_stt_service()
    assert isinstance(seen["config"], SmallestConfig)


# ---------------------------------------------------------------- end_of_speech_ms


@pytest.mark.parametrize(
    "provider, builder, read",
    [
        ("soniox", "build_soniox_stt", lambda c: c.max_endpoint_delay_ms),
        ("deepgram", "build_deepgram_stt", lambda c: c.endpointing),
        ("assemblyai", "build_assemblyai_stt", lambda c: c.max_turn_silence),
    ],
)
async def test_end_of_speech_reaches_each_providers_own_setting(
    monkeypatch, provider, builder, read
):
    seen = _capture(monkeypatch, builder)
    await create_stt_from_config(
        STTConfiguration(provider=provider, end_of_speech_ms=700)
    )
    assert read(seen["config"]) == pytest.approx(700)


async def test_end_of_speech_reaches_flux_as_eot_timeout(monkeypatch):
    seen = _capture(monkeypatch, "build_deepgram_flux_stt")
    await create_stt_from_config(
        STTConfiguration(
            provider="deepgram",
            deepgram={"model": "flux-general-multi"},
            end_of_speech_ms=1500,
        )
    )
    assert seen["config"].eot_timeout_ms == 1500


@pytest.mark.parametrize(
    "block, builder, read",
    [
        (
            {"provider": "deepgram", "deepgram": {"endpointing_ms": 900}},
            "build_deepgram_stt",
            lambda c: c.endpointing,
        ),
        (
            {"provider": "assemblyai", "assemblyai": {"max_turn_silence": 900}},
            "build_assemblyai_stt",
            lambda c: c.max_turn_silence,
        ),
        (
            {
                "provider": "deepgram",
                "deepgram": {"model": "flux-general-multi", "eot_timeout_ms": 900},
            },
            "build_deepgram_flux_stt",
            lambda c: c.eot_timeout_ms,
        ),
    ],
)
async def test_a_providers_own_field_wins_over_end_of_speech(
    monkeypatch, block, builder, read
):
    seen = _capture(monkeypatch, builder)
    await create_stt_from_config(STTConfiguration(**block, end_of_speech_ms=700))
    assert read(seen["config"]) == pytest.approx(900)


async def test_unset_end_of_speech_keeps_todays_defaults(monkeypatch):
    soniox = _capture(monkeypatch, "build_soniox_stt")
    await create_stt_from_config(STTConfiguration(provider="soniox"))
    assert (
        soniox["config"].max_endpoint_delay_ms
        == static.BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS
    )

    aai = _capture(monkeypatch, "build_assemblyai_stt")
    await create_stt_from_config(STTConfiguration(provider="assemblyai"))
    assert (aai["config"].min_turn_silence, aai["config"].max_turn_silence) == (
        100,
        1000,
    )


async def test_assemblyai_min_turn_silence_follows_a_shorter_max(monkeypatch):
    """AssemblyAI refuses min > max; the default min (100) must not exceed
    an end_of_speech_ms of 80."""
    seen = _capture(monkeypatch, "build_assemblyai_stt")
    await create_stt_from_config(
        STTConfiguration(provider="assemblyai", end_of_speech_ms=80)
    )
    assert (seen["config"].min_turn_silence, seen["config"].max_turn_silence) == (
        80,
        80,
    )


@pytest.mark.parametrize(
    "block, message",
    [
        ({"provider": "soniox", "end_of_speech_ms": 300}, "soniox supports 500-3000"),
        (
            {"provider": "elevenlabs", "end_of_speech_ms": 500},
            "not supported by elevenlabs",
        ),
        (
            {"provider": "assemblyai", "end_of_speech_ms": 20000},
            "assemblyai supports 50-10000",
        ),
        ({"provider": "deepgram", "end_of_speech_ms": 5}, "deepgram supports >=10"),
        (
            {
                "provider": "deepgram",
                "deepgram": {"model": "flux-general-multi"},
                "end_of_speech_ms": 400,
            },
            "deepgram_flux supports 500-60000",
        ),
        ({"provider": "sarvam", "end_of_speech_ms": 500}, "not supported by sarvam"),
        (
            {"provider": "smallest", "end_of_speech_ms": 500},
            "not supported by smallest",
        ),
        ({"provider": "google", "end_of_speech_ms": 500}, "not supported by google"),
        (
            {
                "provider": "soniox",
                "turn_detection": "smart_turn",
                "end_of_speech_ms": 800,
            },
            "smart_turn",
        ),
    ],
)
def test_end_of_speech_is_refused_at_parse_not_ignored(block, message):
    with pytest.raises(ValidationError, match=message):
        STTConfiguration.model_validate(block)


def test_every_supported_provider_has_a_range():
    assert set(END_OF_SPEECH_RANGE_MS) == {
        "soniox",
        "deepgram",
        "deepgram_flux",
        "assemblyai",
    }


# ---------------------------------------------------------------- template blocks


@pytest.mark.parametrize(
    "deepgram, message",
    [
        ({"model": "flux-general-multi", "endpointing_ms": 50}, "do not apply to Flux"),
        (
            {"model": "flux-general-multi", "smart_format": False},
            "do not apply to Flux",
        ),
        ({"eot_threshold": 0.8}, "do not apply to Nova"),
        (
            {
                "model": "flux-general-multi",
                "eot_threshold": 0.6,
                "eager_eot_threshold": 0.8,
            },
            "must not exceed",
        ),
        ({"model": "flux-general-multi", "eot_timeout_ms": 100}, "eot_timeout_ms"),
    ],
)
def test_deepgram_fields_must_match_the_model_family(deepgram, message):
    with pytest.raises(ValidationError, match=message):
        STTConfiguration(provider="deepgram", deepgram=deepgram)


def test_smallest_refuses_an_unknown_language():
    with pytest.raises(ValidationError, match="unknown language"):
        STTConfiguration(provider="smallest", smallest={"language": "xx-nope"})


# ---------------------------------------------------------------- built services


def test_smallest_is_pipecats_own_service_with_digits():
    """Stock pipecat service, no subclass: left to Pulse's default the
    number came back as English words on Hindi audio."""
    svc = build_smallest_stt(SmallestConfig(api_key="k"))
    assert type(svc) is SmallestSTTService
    assert svc._settings.numerals == "true"


def test_flux_hands_barge_in_to_the_pipeline():
    """With interims flowing, the template's interruption mode and min_words
    decide, as for every other provider; Flux's own StartOfTurn interruption
    would fire on the first sound and bypass both."""
    svc = build_deepgram_flux_stt(
        DeepgramFluxConfig(
            api_key="k", language_hints=["hi", "en"], eot_timeout_ms=1500
        )
    )
    assert isinstance(svc, DeepgramFluxSTTServiceWithInterims)
    assert svc._should_interrupt is False
    assert svc._settings.eot_timeout_ms == 1500
    hints = svc._settings.language_hints
    assert isinstance(hints, list)
    assert [h.value for h in hints] == ["hi", "en"]


async def test_flux_pushes_its_live_updates_as_interims(monkeypatch):
    """pipecat 1.1.0 only fires on_update; the pipeline needs interim frames."""
    from pipecat.frames.frames import InterimTranscriptionFrame

    svc = build_deepgram_flux_stt(DeepgramFluxConfig(api_key="k"))
    pushed: list[Any] = []

    async def capture(frame, *args, **kwargs):
        pushed.append(frame)

    monkeypatch.setattr(svc, "push_frame", capture)
    await svc._handle_update("haan kal nahi")
    await svc._handle_update("")
    assert [type(f) for f in pushed] == [InterimTranscriptionFrame]
    assert pushed[0].text == "haan kal nahi"


async def test_flux_interims_drive_min_words_like_other_providers():
    """pipecat's own MinWords rule, fed Flux's live words while the bot
    speaks: a short 'haan' does not interrupt, ten words do."""
    from pipecat.frames.frames import BotStartedSpeakingFrame, InterimTranscriptionFrame
    from pipecat.turns.types import ProcessFrameResult
    from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
        MinWordsUserTurnStartStrategy,
    )

    rule = MinWordsUserTurnStartStrategy(min_words=10, use_interim=True)
    await rule.process_frame(BotStartedSpeakingFrame())

    def interim(text: str) -> InterimTranscriptionFrame:
        return InterimTranscriptionFrame(text, "", "now", None)

    assert await rule.process_frame(interim("haan")) == ProcessFrameResult.CONTINUE
    ten = "haan ji mujhe kal nahi parso delivery chahiye please bhaiya"
    assert await rule.process_frame(interim(ten)) == ProcessFrameResult.STOP


def test_eager_eot_must_not_exceed_deepgrams_default_eot():
    """With eot_threshold unset Deepgram uses 0.7; eager 0.8 is invalid."""
    with pytest.raises(ValidationError, match="Deepgram default"):
        STTConfiguration(
            provider="deepgram",
            deepgram={"model": "flux-general-multi", "eager_eot_threshold": 0.8},
        )
    STTConfiguration(
        provider="deepgram",
        deepgram={"model": "flux-general-multi", "eager_eot_threshold": 0.6},
    )


@pytest.mark.parametrize(
    "model, language, expected",
    [
        # saaras:v3 accepts a language: pinning it was silently dropped before.
        ("saaras:v3", "hi-IN", "hi-IN"),
        # Unset stays auto-detect: no language is sent, exactly as before.
        ("saaras:v3", None, None),
        ("saarika:v2.5", "hi-IN", "hi-IN"),
    ],
)
def test_sarvam_pins_the_language_on_models_that_accept_one(model, language, expected):
    svc = build_sarvam_stt(
        SarvamConfig(api_key="k", model=model, sample_rate=8000, language_code=language)
    )
    language = svc._settings.language
    assert getattr(language, "value", language) == expected


def test_sarvam_never_sends_a_prompt_to_a_model_that_rejects_it():
    """saaras:v3 raises on a prompt; the old family rule sent one to it."""
    build_sarvam_stt(
        SarvamConfig(
            api_key="k", model="saaras:v3", sample_rate=8000, prompt="COD call"
        )
    )


# ---------------------------------------------------------------- storage round trips
# Templates are saved with model_dump(exclude_none=True) and cached with
# model_dump_json(); both must reload with the same behaviour. model_fields_set
# does not survive either, which broke the first version of this PR.


def _saved(block: dict) -> STTConfiguration:
    parsed = STTConfiguration.model_validate(block)
    return STTConfiguration.model_validate(
        parsed.model_dump(exclude_none=True, mode="json")
    )


def _cached(block: dict) -> STTConfiguration:
    parsed = STTConfiguration.model_validate(block)
    return STTConfiguration.model_validate_json(parsed.model_dump_json())


@pytest.mark.parametrize("reload", [_saved, _cached])
@pytest.mark.parametrize(
    "block",
    [
        {"provider": "deepgram", "deepgram": {"model": "flux-general-multi"}},
        {"provider": "deepgram", "deepgram": {"model": "nova-3-general"}},
        {"provider": "deepgram"},
        {"provider": "smallest", "smallest": {"language": "en"}},
    ],
)
def test_templates_reload_after_save_and_cache(reload, block):
    reload(block)


@pytest.mark.parametrize("reload", [_saved, _cached])
@pytest.mark.parametrize(
    "block, builder, read",
    [
        (
            {"provider": "deepgram", "deepgram": {}, "end_of_speech_ms": 800},
            "build_deepgram_stt",
            lambda c: c.endpointing,
        ),
        (
            {"provider": "assemblyai", "assemblyai": {}, "end_of_speech_ms": 800},
            "build_assemblyai_stt",
            lambda c: c.max_turn_silence,
        ),
    ],
)
async def test_end_of_speech_survives_save_and_cache(
    monkeypatch, reload, block, builder, read
):
    """Defaults written out by the save must not masquerade as the
    template's own value and silently override end_of_speech_ms."""
    seen = _capture(monkeypatch, builder)
    await create_stt_from_config(reload(block))
    assert read(seen["config"]) == 800


async def test_smallest_takes_the_template_language_when_its_block_has_none(
    monkeypatch,
):
    seen = _capture(monkeypatch, "build_smallest_stt")
    await create_stt_from_config(STTConfiguration(provider="smallest", language="en"))
    assert seen["config"].language == "en"


async def test_smallest_defaults_to_hindi(monkeypatch):
    seen = _capture(monkeypatch, "build_smallest_stt")
    await create_stt_from_config(STTConfiguration(provider="smallest"))
    assert seen["config"].language == "hi"


def test_flux_is_refused_under_smart_turn():
    """Flux only finalizes on its own EndOfTurn; SmartTurn's tuning would
    silently do nothing."""
    with pytest.raises(ValidationError, match="ignores SmartTurn"):
        STTConfiguration(
            provider="deepgram",
            turn_detection="smart_turn",
            deepgram={"model": "flux-general-multi"},
        )


def test_nova_is_still_fine_under_smart_turn():
    STTConfiguration(provider="deepgram", turn_detection="smart_turn")


def test_a_provider_field_left_at_its_default_reads_as_unset():
    """Documented edge: pinning the vendor default does not beat
    end_of_speech_ms, because stored templates carry every default written
    out and must still honour end_of_speech_ms."""
    config = STTConfiguration(
        provider="assemblyai",
        assemblyai={"max_turn_silence": 1000},
        end_of_speech_ms=3000,
    )
    assert config.assemblyai is not None
    assert not set_by_template(config.assemblyai, "max_turn_silence")


# ---------------------------------------------------------------- third review


def test_smallest_refuses_a_language_pulse_does_not_support():
    """pipecat knows 'zh'; Pulse is not verified for it, and a rejected code
    leaves the call with no transcripts."""
    with pytest.raises(ValidationError, match="Pulse does not support"):
        STTConfiguration(provider="smallest", smallest={"language": "zh"})
    STTConfiguration(provider="smallest", smallest={"language": "hi-IN"})


def test_smallest_builder_degrades_an_unsupported_template_language_to_hindi():
    from pipecat.transcriptions.language import Language

    svc = build_smallest_stt(SmallestConfig(api_key="k", language="zh"))
    assert svc._settings.language == Language.HI


async def test_comma_joined_legacy_language_is_split(monkeypatch):
    """The legacy path joins a language list into one string ("en,hi")."""
    smallest = _capture(monkeypatch, "build_smallest_stt")
    await create_stt_from_config(
        STTConfiguration(provider="smallest", language="en,hi")
    )
    assert smallest["config"].language == "en"

    flux = _capture(monkeypatch, "build_deepgram_flux_stt")
    await create_stt_from_config(
        STTConfiguration(
            provider="deepgram",
            language="en,hi",
            deepgram={"model": "flux-general-multi"},
        )
    )
    assert flux["config"].language_hints == ["en", "hi"]


def test_stt_stream_config_runs_the_deepgram_family_check():
    """The /stt stream fills the flat model into the nested block; it must
    go through validation, not model_copy (which skips validators)."""
    from app.api.routers.breeze_buddy.stt.handlers import _stream_configuration
    from app.schemas.breeze_buddy.stt import TranscriptionStreamRequest

    # Nova settings under a flat Flux model: refused when the request is read.
    with pytest.raises(ValidationError, match="do not apply to Flux"):
        TranscriptionStreamRequest(
            provider="deepgram",
            model="flux-general-multi",
            deepgram={"endpointing_ms": 300},
        )
    # Flux settings under a flat Flux model: accepted, and built as Flux.
    request = TranscriptionStreamRequest(
        provider="deepgram",
        model="flux-general-multi",
        deepgram={"eot_threshold": 0.8},
    )
    config = _stream_configuration(request)
    assert config.deepgram is not None
    assert (config.deepgram.model, config.deepgram.eot_threshold) == (
        "flux-general-multi",
        0.8,
    )


# ---------------------------------------------------------------- sarvam barge-in
# Sarvam streams no partial words, only START_SPEECH and the finished segment.
# Its own interruption fires on the first sound; it is kept only when the
# template has no rule that needs to judge the words (option C).


@pytest.mark.parametrize("self_interrupt", [True, False])
async def test_sarvam_self_interruption_follows_the_flag(monkeypatch, self_interrupt):
    from pipecat.services.sarvam.stt import SarvamSTTService

    calls: list[bool] = []

    async def parent_broadcast(self):
        calls.append(True)

    monkeypatch.setattr(SarvamSTTService, "broadcast_interruption", parent_broadcast)
    svc = build_sarvam_stt(
        SarvamConfig(
            api_key="k",
            model="saaras:v3",
            sample_rate=8000,
            self_interrupt=self_interrupt,
        )
    )
    await svc.broadcast_interruption()
    assert calls == ([True] if self_interrupt else [])


async def test_routing_passes_the_self_interrupt_flag_to_sarvam(monkeypatch):
    seen = _capture(monkeypatch, "build_sarvam_stt")
    await bb_stt_mod.get_stt_service(
        stt_configuration=STTConfiguration(provider="sarvam"),
        stt_self_interrupt=False,
    )
    assert seen["config"].self_interrupt is False


@pytest.mark.parametrize(
    "interruption, expected",
    [
        ({"mode": "enabled"}, True),  # no rule: Sarvam's instant barge-in stays
        ({"mode": "enabled", "min_words": 10}, False),  # rule counts the words
        ({"mode": "disabled_discard"}, False),  # the bot must not be cut
    ],
)
async def test_pipeline_lets_the_stt_self_interrupt_only_without_a_rule(
    monkeypatch, interruption, expected
):
    from types import SimpleNamespace

    from app.ai.voice.agents.breeze_buddy.agent import pipeline
    from app.ai.voice.agents.breeze_buddy.template.types import InterruptionConfig

    seen: dict[str, Any] = {}

    async def fake_get_stt_service(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after STT")

    monkeypatch.setattr(pipeline, "get_stt_service", fake_get_stt_service)
    configurations = SimpleNamespace(
        stt_configuration=STTConfiguration(provider="sarvam"),
        interruption=InterruptionConfig(**interruption),
    )
    with pytest.raises(RuntimeError, match="stop after STT"):
        await pipeline.create_services(cast(Any, configurations))
    assert seen["stt_self_interrupt"] is expected


# ---------------------------------------------------------------- fifth review


@pytest.mark.parametrize(
    "model, template_language, expected",
    [
        # saaras never took a language before: the global Redis default must
        # not start pinning every saaras template after deploy.
        ("saaras:v3", None, None),
        ("saaras:v3", "ta-IN", "ta-IN"),  # the template's own choice pins
        ("saarika:v2.5", None, "hi-IN"),  # saarika keeps the Redis default
    ],
)
async def test_sarvam_language_source(monkeypatch, model, template_language, expected):
    async def redis_language():
        return "hi-IN"

    monkeypatch.setattr(bb_stt_mod, "BB_SARVAM_STT_LANGUAGE_CODE", redis_language)
    seen = _capture(monkeypatch, "build_sarvam_stt")
    sarvam: dict[str, Any] = {"model": model}
    if template_language:
        sarvam["language_code"] = template_language
    await create_stt_from_config(STTConfiguration(provider="sarvam", sarvam=sarvam))
    assert seen["config"].language_code == expected


@pytest.mark.parametrize(
    "interruption, expected",
    [
        ({"mode": "enabled"}, True),
        ({"mode": "enabled", "min_words": 10}, False),
        ({"mode": "disabled_discard"}, False),
    ],
)
async def test_node_interruption_override_reaches_sarvam(interruption, expected):
    """Node-level overrides go through _apply_interruption_config; Sarvam's
    self-interruption must follow them, not only the template default."""
    from types import SimpleNamespace

    from app.ai.voice.agents.breeze_buddy.template.interruption import (
        _apply_interruption_config,
    )
    from app.ai.voice.agents.breeze_buddy.template.types import InterruptionConfig

    class Controller:
        async def update_strategies(self, strategies):
            pass

    aggregator = SimpleNamespace(
        _user_turn_controller=Controller(),
        _params=SimpleNamespace(user_mute_strategies=[]),
        task_manager=None,
        _user_is_muted=False,
    )

    class Mute:
        async def setup(self, task_manager):
            pass

    import app.ai.voice.agents.breeze_buddy.template.interruption as interruption_mod

    stt = build_sarvam_stt(
        SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000)
    )
    bot = SimpleNamespace(stt_service=stt)
    original = interruption_mod.AlwaysUserMuteStrategy
    interruption_mod.AlwaysUserMuteStrategy = Mute  # type: ignore[misc]
    try:
        await _apply_interruption_config(
            aggregator,
            InterruptionConfig(**interruption),
            has_vad=False,
            call_sid="t",
            label="node:test",
            bot=bot,
        )
    finally:
        interruption_mod.AlwaysUserMuteStrategy = original  # type: ignore[misc]
    assert stt._self_interrupt is expected


def test_end_of_speech_is_refused_when_soniox_forces_vad_endpoints():
    with pytest.raises(ValidationError, match="vad_force_turn_endpoint"):
        STTConfiguration(
            provider="soniox",
            soniox={"vad_force_turn_endpoint": True},
            end_of_speech_ms=700,
        )


@pytest.mark.parametrize("end_of_speech_ms, expected", [(800, 800), (None, None)])
async def test_assemblyai_explicit_null_reads_as_unset(
    monkeypatch, end_of_speech_ms, expected
):
    seen = _capture(monkeypatch, "build_assemblyai_stt")
    await create_stt_from_config(
        STTConfiguration(
            provider="assemblyai",
            assemblyai={"max_turn_silence": None},
            end_of_speech_ms=end_of_speech_ms,
        )
    )
    assert seen["config"].max_turn_silence == expected


# ---------------------------------------------------------------- sixth review


def test_stt_stream_blank_model_keeps_the_deepgram_default():
    from app.api.routers.breeze_buddy.stt.handlers import _stream_configuration
    from app.schemas.breeze_buddy.stt import TranscriptionStreamRequest

    request = TranscriptionStreamRequest(provider="deepgram", model="  ", deepgram={})
    config = _stream_configuration(request)
    assert config.deepgram is not None
    assert config.deepgram.model == "nova-3-general"


def test_stt_stream_padded_flux_model_still_reads_as_flux():
    from app.schemas.breeze_buddy.stt import TranscriptionStreamRequest

    request = TranscriptionStreamRequest(
        provider="deepgram",
        model=" flux-general-multi ",
        deepgram={"eot_threshold": 0.8},
    )
    assert request.deepgram is not None
    assert request.deepgram.model == "flux-general-multi"
    assert request.deepgram.is_flux


@pytest.mark.parametrize(
    "interruption, expected",
    [
        ({"mode": "enabled"}, True),
        ({"mode": "enabled", "min_words": 10}, False),
        ({"mode": "disabled_discard"}, False),
    ],
)
async def test_legacy_env_stt_path_gets_the_self_interrupt_flag(
    monkeypatch, interruption, expected
):
    """A template with no stt_configuration builds its STT from
    BREEZE_BUDDY_STT_SERVICE; Sarvam there must follow min_words too."""
    from types import SimpleNamespace

    from app.ai.voice.agents.breeze_buddy.agent import pipeline
    from app.ai.voice.agents.breeze_buddy.template.types import InterruptionConfig

    seen: dict[str, Any] = {}

    async def fake_get_stt_service(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after STT")

    monkeypatch.setattr(pipeline, "get_stt_service", fake_get_stt_service)
    configurations = SimpleNamespace(
        stt_configuration=None, interruption=InterruptionConfig(**interruption)
    )
    with pytest.raises(RuntimeError, match="stop after STT"):
        await pipeline.create_services(cast(Any, configurations))
    assert "stt_configuration" not in seen  # the legacy branch ran
    assert seen["stt_self_interrupt"] is expected


async def test_legacy_env_sarvam_receives_the_flag(monkeypatch):
    seen = _capture(monkeypatch, "build_sarvam_stt")
    monkeypatch.setattr(bb_stt_mod, "BREEZE_BUDDY_STT_SERVICE", "sarvam")
    await bb_stt_mod.get_stt_service(stt_self_interrupt=False)
    assert seen["config"].self_interrupt is False
