"""Regression coverage for VAD param resolution (template > Redis/defaults).

Locks in `_resolve_vad_params` / `_layer_template_vad` /
`_apply_vad_config_to_analyzer` behavior across the config_resolver
migration: per-field override precedence, partial overrides, and the
no-override passthrough.
"""

from types import SimpleNamespace
from typing import cast

import pytest
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
    VadConfig,
)
from app.ai.voice.agents.breeze_buddy.template.vad import (
    SMART_TURN_TRIGGER_STOP_SECS,
    _apply_vad_config_to_analyzer,
    _layer_template_vad,
    _resolve_vad_params,
    build_smart_turn_trigger_vad_params,
    create_telephony_vad_params,
)

DEFAULTS = VADParams(confidence=0.5, start_secs=0.2, stop_secs=0.8, min_volume=0.6)


def _no_override(_field):
    return None


@pytest.mark.asyncio
async def test_no_override_returns_base_unchanged():
    result = await _resolve_vad_params(_no_override, DEFAULTS)
    assert result == DEFAULTS


@pytest.mark.asyncio
async def test_full_override_wins_on_every_field():
    override = VADParams(confidence=0.9, start_secs=0.1, stop_secs=0.3, min_volume=0.7)
    result = await _resolve_vad_params(lambda f: getattr(override, f), DEFAULTS)
    assert result == override


@pytest.mark.asyncio
async def test_partial_override_merges_field_by_field():
    def getter(field):
        return 0.99 if field == "confidence" else None

    result = await _resolve_vad_params(getter, DEFAULTS)
    assert result.confidence == 0.99
    assert result.start_secs == DEFAULTS.start_secs
    assert result.stop_secs == DEFAULTS.stop_secs
    assert result.min_volume == DEFAULTS.min_volume


@pytest.mark.asyncio
async def test_layer_template_vad_no_template_returns_defaults():
    result = await _layer_template_vad(None, DEFAULTS)
    assert result == DEFAULTS


@pytest.mark.asyncio
async def test_layer_template_vad_with_partial_template_config():
    template = SimpleNamespace(
        configurations=SimpleNamespace(
            vad_config=SimpleNamespace(
                confidence=0.42, start_secs=None, stop_secs=None, min_volume=None
            )
        )
    )
    result = await _layer_template_vad(cast(TemplateModel, template), DEFAULTS)
    assert result.confidence == 0.42
    assert result.start_secs == DEFAULTS.start_secs


@pytest.mark.asyncio
async def test_layer_template_vad_no_configurations_returns_defaults():
    template = SimpleNamespace(configurations=None)
    result = await _layer_template_vad(cast(TemplateModel, template), DEFAULTS)
    assert result == DEFAULTS


class _FakeAnalyzer:
    def __init__(self, params: VADParams):
        self.params = params

    def set_params(self, params: VADParams):
        self.params = params


@pytest.mark.asyncio
async def test_apply_vad_config_to_analyzer_dict_access():
    analyzer = _FakeAnalyzer(DEFAULTS)
    vad_config = {"confidence": 0.77, "start_secs": None}
    await _apply_vad_config_to_analyzer(
        cast(VADAnalyzer, analyzer), vad_config, call_sid="call-1"
    )
    assert analyzer.params.confidence == 0.77
    assert analyzer.params.start_secs == DEFAULTS.start_secs


@pytest.mark.asyncio
async def test_apply_vad_config_to_analyzer_object_access():
    analyzer = _FakeAnalyzer(DEFAULTS)
    vad_config = SimpleNamespace(
        confidence=None, start_secs=0.05, stop_secs=None, min_volume=None
    )
    await _apply_vad_config_to_analyzer(
        cast(VADAnalyzer, analyzer), cast(VadConfig, vad_config), call_sid="call-1"
    )
    assert analyzer.params.start_secs == 0.05
    assert analyzer.params.confidence == DEFAULTS.confidence


@pytest.mark.asyncio
async def test_smart_turn_trigger_vad_keeps_its_own_stop_secs_default():
    """Latency guard: the trigger must not inherit BB_TELEPHONY_VAD_STOP_SECS.

    That flag is tuned for the plain-VAD/timeout path; retuning it there must
    not change SmartTurn latency.
    """
    result = await build_smart_turn_trigger_vad_params(None)
    assert result.stop_secs == SMART_TURN_TRIGGER_STOP_SECS
    assert result.stop_secs != (await create_telephony_vad_params()).stop_secs


@pytest.mark.asyncio
async def test_smart_turn_trigger_vad_honours_template_override():
    configurations = SimpleNamespace(
        vad_config=SimpleNamespace(
            confidence=None, start_secs=None, stop_secs=0.45, min_volume=None
        )
    )
    result = await build_smart_turn_trigger_vad_params(
        cast(ConfigurationModel, configurations)
    )
    assert result.stop_secs == 0.45


@pytest.mark.asyncio
async def test_smart_turn_trigger_vad_handles_configurations_without_vad_config():
    configurations = SimpleNamespace(vad_config=None)
    result = await build_smart_turn_trigger_vad_params(
        cast(ConfigurationModel, configurations)
    )
    assert result == VADParams(stop_secs=SMART_TURN_TRIGGER_STOP_SECS)
