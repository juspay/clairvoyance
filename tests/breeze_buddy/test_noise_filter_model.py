"""noise_filter.model names the ai-coustics model and its size.

noise_cancellation is quail-L (today's model), noise_cancellation_s is quail-S at
about half the CPU (AIC lab, Sep 2026). A missing quail-S file must degrade to
quail-L, never to no filter, and the resolver reports the model it really loaded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

# isort: off
# template.types must load before the agent modules (circular import otherwise).
from app.ai.voice.agents.breeze_buddy.template.types import (
    NoiseFilterConfig,
    NoiseFilterModel,
)

from app.ai.voice.agents.breeze_buddy.agent import transport
from app.ai.voice.agents.breeze_buddy.agent.transport import (
    TRANSPORT_TYPE_DAILY,
    TRANSPORT_TYPE_TELEPHONY,
    _resolve_aic_model,
)

# isort: on

NAMES = {
    "AIC_MODEL_PATH": "quail_l_8khz.aicmodel",
    "AIC_MODEL_PATH_16KHZ": "quail_l_16khz.aicmodel",
    "AIC_VOICE_FOCUS_MODEL_PATH": "quail_vf_2_1_l_16khz.aicmodel",
    "AIC_MODEL_PATH_S": "quail_s_8khz.aicmodel",
    "AIC_MODEL_PATH_S_16KHZ": "quail_s_16khz.aicmodel",
}
NC = NoiseFilterModel.NOISE_CANCELLATION
NC_S = NoiseFilterModel.NOISE_CANCELLATION_S
VF = NoiseFilterModel.VOICE_FOCUS


@pytest.fixture
def models(tmp_path, monkeypatch):
    """Every artifact present in a temp dir, wired into static config."""
    for attr, name in NAMES.items():
        (tmp_path / name).write_bytes(b"model")
        monkeypatch.setattr(transport.static, attr, str(tmp_path / name))
    return tmp_path


@pytest.mark.parametrize(
    "model, transport_type, expected_file",
    [
        (NC, TRANSPORT_TYPE_TELEPHONY, "quail_l_8khz.aicmodel"),
        (NC, TRANSPORT_TYPE_DAILY, "quail_l_16khz.aicmodel"),
        (NC_S, TRANSPORT_TYPE_TELEPHONY, "quail_s_8khz.aicmodel"),
        (NC_S, TRANSPORT_TYPE_DAILY, "quail_s_16khz.aicmodel"),
        (VF, TRANSPORT_TYPE_TELEPHONY, "quail_vf_2_1_l_16khz.aicmodel"),
        (VF, TRANSPORT_TYPE_DAILY, "quail_vf_2_1_l_16khz.aicmodel"),
    ],
)
def test_model_selects_artifact(models: Path, model, transport_type, expected_file):
    used, path = _resolve_aic_model(model, transport_type)
    assert path.name == expected_file
    assert used == model


@pytest.mark.parametrize(
    "transport_type, s_file, l_file",
    [
        (TRANSPORT_TYPE_TELEPHONY, "quail_s_8khz.aicmodel", "quail_l_8khz.aicmodel"),
        (TRANSPORT_TYPE_DAILY, "quail_s_16khz.aicmodel", "quail_l_16khz.aicmodel"),
    ],
)
def test_missing_quail_s_falls_back_to_quail_l(
    models: Path, transport_type, s_file, l_file
):
    (models / s_file).unlink()
    used, path = _resolve_aic_model(NC_S, transport_type)
    assert path.name == l_file
    assert used == NC  # the init log must not claim quail-S for a quail-L file


def test_template_accepts_noise_cancellation_s():
    cfg = NoiseFilterConfig(enable=True, provider="aic", model="noise_cancellation_s")
    assert cfg.model == NC_S


def test_template_rejects_unknown_model():
    with pytest.raises(ValidationError):
        NoiseFilterConfig(enable=True, provider="aic", model="noise_cancellation_m")


def test_legacy_type_still_means_quail_l():
    """type="aic" templates keep resolving to noise_cancellation (quail-L)."""
    cfg = NoiseFilterConfig(enable=True, type="aic")
    assert cfg.model is None and cfg.provider is None
