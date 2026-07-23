"""
Wire-compat tests for the telephony_number_id rename: template create/replace
accept the pre-rename "outbound_number_id" key from older API clients and
convert it to telephony_number_id; responses emit the new name only.
"""

import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from app.ai.voice.agents.breeze_buddy.template.types import (  # noqa: E402
    CreateTemplateRequest,
    HoldTransferConfig,
    ReplaceTemplateRequest,
    TemplateModel,
)
from app.schemas.breeze_buddy.analytics import AnalyticsType  # noqa: E402

_FLOW = {"nodes": []}


def _create(body: dict) -> CreateTemplateRequest:
    return CreateTemplateRequest.model_validate(
        {"reseller_id": "acme", "name": "t", "flow": _FLOW, **body}
    )


def _replace(body: dict) -> ReplaceTemplateRequest:
    return ReplaceTemplateRequest.model_validate(
        {"name": "t", "is_active": True, "flow": _FLOW, **body}
    )


def test_create_accepts_old_key():
    req = _create({"outbound_number_id": "num-1"})
    assert req.telephony_number_id == "num-1"


def test_create_accepts_new_key():
    req = _create({"telephony_number_id": "num-2"})
    assert req.telephony_number_id == "num-2"


def test_create_new_key_wins_when_both_sent():
    req = _create({"telephony_number_id": "new", "outbound_number_id": "old"})
    assert req.telephony_number_id == "new"


def test_create_neither_key_means_no_pin():
    assert _create({}).telephony_number_id is None


def test_replace_old_key_alone_keeps_the_pin():
    # An old client building a PUT body from scratch must not silently
    # clear the template's pin.
    req = _replace({"outbound_number_id": "num-3"})
    assert req.telephony_number_id == "num-3"


def test_replace_get_roundtrip_body_still_works():
    # GET emits telephony_number_id only; sending that body straight back
    # to PUT (with stray extras) keeps the pin.
    req = _replace({"telephony_number_id": "num-4", "id": "ignored", "created_at": "x"})
    assert req.telephony_number_id == "num-4"


def test_replace_omitting_both_clears_the_pin():
    # Existing PUT semantics: omitted nullable fields are set to NULL.
    assert _replace({}).telephony_number_id is None


def test_response_emits_new_name_only():
    tpl = TemplateModel(
        id="t-1",
        reseller_id="acme",
        name="t",
        flow=_FLOW,
        telephony_number_id="num-5",
    )
    dumped = tpl.model_dump()
    assert dumped["telephony_number_id"] == "num-5"
    assert "outbound_number_id" not in dumped


def _capture_warnings(monkeypatch):
    captured = []

    class _Stub:
        # loguru-style: message is a {}-template, values ride in args
        def warning(self, msg, *args, **kwargs):
            captured.append(str(msg).format(*args) if args else str(msg))

    import app.core.deprecation as dep

    monkeypatch.setattr(dep, "logger", _Stub())
    return captured


def test_old_key_usage_is_logged_deprecated(monkeypatch):
    captured = _capture_warnings(monkeypatch)
    _create({"outbound_number_id": "num-6"})
    _replace({"outbound_number_id": "num-7"})
    assert len(captured) == 2
    assert all("[Deprecated]" in m and "outbound_number_id" in m for m in captured)


def test_new_key_usage_is_not_logged(monkeypatch):
    captured = _capture_warnings(monkeypatch)
    _create({"telephony_number_id": "num-8"})
    _replace({"telephony_number_id": "num-9"})
    assert captured == []


def test_hold_transfer_parses_old_stored_key_and_logs(monkeypatch):
    captured = _capture_warnings(monkeypatch)
    cfg = HoldTransferConfig.model_validate({"outbound_number_id": "num-10"})
    assert cfg.telephony_number_id == "num-10"
    assert any("[Deprecated]" in m for m in captured)
    # the old key never serializes back out
    assert "outbound_number_id" not in cfg.model_dump()


def test_hold_transfer_still_requires_a_number():
    import pytest

    with pytest.raises(Exception):
        HoldTransferConfig.model_validate({"hold_music": "typing"})


def test_analytics_type_accepts_deprecated_string(monkeypatch):
    captured = _capture_warnings(monkeypatch)
    assert AnalyticsType("outbound-numbers") is AnalyticsType.TELEPHONY_NUMBERS
    assert AnalyticsType("telephony-numbers") is AnalyticsType.TELEPHONY_NUMBERS
    assert any(
        "[Deprecated]" in m and "outbound-numbers" in m for m in captured
    ), captured
