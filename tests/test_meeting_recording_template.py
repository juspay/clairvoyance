"""meeting_recording template block: validation, synthesized tool, diarization
prep, and that a template without the block changes nothing."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.recorder.config import (
    meeting_recording_enabled,
    prepare_configurations_for_meeting_recording,
)
from app.ai.voice.agents.breeze_buddy.template.kb_tool import append_kb_tool
from app.ai.voice.agents.breeze_buddy.template.meeting_tool import (
    synthesize_meeting_recording_function,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    MeetingRecordingConfig,
)


def test_block_defaults_and_hook_validation():
    cfg = MeetingRecordingConfig(enabled=True)
    assert cfg.start_function_name == "start_meeting_recording"
    assert cfg.start_confirmation.startswith("Starting the recording")
    assert "{start_confirmation}" in cfg.start_instruction
    assert "{duration}" in cfg.stop_confirmation
    assert cfg.on_stop_hooks == [] and cfg.stop_phrase is None

    with pytest.raises(ValidationError):
        MeetingRecordingConfig(
            enabled=True, on_stop_hooks=[{"name": "send_http_request"}]
        )
    ok = MeetingRecordingConfig(
        enabled=True,
        on_stop_hooks=[
            {"name": "send_http_request", "http_request": {"url": "https://x/y"}}
        ],
    )
    assert ok.on_stop_hooks[0].http_request is not None


def test_configuration_model_accepts_the_block_and_defaults_to_none():
    assert ConfigurationModel.model_validate({}).meeting_recording is None
    c = ConfigurationModel.model_validate({"meeting_recording": {"enabled": True}})
    assert c.meeting_recording is not None and c.meeting_recording.enabled


def test_synthesized_tool_only_when_enabled():
    off = SimpleNamespace(configurations=ConfigurationModel.model_validate({}))
    assert synthesize_meeting_recording_function(off) is None
    assert append_kb_tool([{"name": "x"}], None) == [{"name": "x"}]

    on = SimpleNamespace(
        configurations=ConfigurationModel.model_validate(
            {"meeting_recording": {"enabled": True, "start_description": "custom"}}
        )
    )
    fn = synthesize_meeting_recording_function(on)
    assert fn is not None
    assert fn["type"] == "builtin" and fn["handler"] == "start_meeting_recording"
    assert fn["name"] == "start_meeting_recording" and fn["description"] == "custom"
    # explicit declaration with the same name wins, no duplicate tool names
    assert append_kb_tool([{"name": "start_meeting_recording"}], fn) == [
        {"name": "start_meeting_recording"}
    ]


def test_diarization_is_forced_only_for_recording_templates():
    plain = ConfigurationModel.model_validate(
        {"stt_configuration": {"provider": "soniox", "soniox": {}}}
    )
    assert not meeting_recording_enabled(plain)
    assert prepare_configurations_for_meeting_recording(plain) is False
    assert (
        plain.stt_configuration is not None
        and plain.stt_configuration.soniox is not None
    )
    assert plain.stt_configuration.soniox.enable_speaker_diarization is None

    rec = ConfigurationModel.model_validate(
        {
            "meeting_recording": {"enabled": True},
            "stt_configuration": {"provider": "soniox", "soniox": {}},
        }
    )
    assert meeting_recording_enabled(rec)
    assert prepare_configurations_for_meeting_recording(rec) is True
    assert (
        rec.stt_configuration is not None and rec.stt_configuration.soniox is not None
    )
    assert rec.stt_configuration.soniox.enable_speaker_diarization is True
    assert prepare_configurations_for_meeting_recording(rec) is False  # idempotent


def test_pipeline_task_keeps_pipecats_idle_cancel_by_default():
    import inspect

    from app.ai.voice.agents.breeze_buddy.agent.pipeline import create_pipeline_task

    param = inspect.signature(create_pipeline_task).parameters["cancel_on_idle_timeout"]
    assert param.default is True
