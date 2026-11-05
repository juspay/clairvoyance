"""The answer path for a call stamped on a lead the merchant aborted mid-dial:
hang up at once, never connect an agent."""

from __future__ import annotations

from typing import Any, cast

# dispatch must import first (managers.calls <-> dispatch.worker cycle).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.api.routers.breeze_buddy.telephony.answer import handlers as ans_mod
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.core import CALL_ATTACHED_AFTER_FINISH
from tests.breeze_buddy.dispatch.conftest import make_lead


class _Request:
    method = "POST"

    async def form(self):
        return {"CallUUID": "CA-1", "From": "+15550001111", "To": "+15551230000"}

    @property
    def query_params(self):
        return {}


def _aborted_lead():
    lead = make_lead("lead-abort", status=LeadCallStatus.FINISHED)
    lead.call_id = "CA-1"
    lead.outcome = "ABORT"
    lead.metaData = {CALL_ATTACHED_AFTER_FINISH: {"at": "t", "call_id": "CA-1"}}
    return lead


async def test_answer_hangs_up_on_a_call_attached_to_an_aborted_lead(monkeypatch):
    lead = _aborted_lead()

    async def _by_call(call_sid):
        return lead

    async def _template(*a, **k):
        raise AssertionError("an agent must not be resolved for an aborted lead")

    built: list = []

    async def _build(*a, **k):
        built.append(a)

    monkeypatch.setattr(ans_mod, "get_lead_by_call_id", _by_call)
    monkeypatch.setattr(ans_mod, "get_template_by_id", _template)
    monkeypatch.setattr(ans_mod, "_build_provider_response", _build)

    response = await ans_mod._handle_provider_answer(cast(Any, _Request()), "plivo")

    body = bytes(response.body).decode()
    assert "<Hangup/>" in body
    assert "<Stream" not in body and "<Speak>" not in body
    assert built == []  # no agent / media stream


async def test_answer_still_serves_a_normal_outbound_lead(monkeypatch):
    lead = make_lead("lead-ok", status=LeadCallStatus.PROCESSING)
    lead.call_id = "CA-1"

    async def _by_call(call_sid):
        return lead

    class _Tmpl:
        id = "tmpl-1"

    async def _template(template_id):
        return _Tmpl()

    monkeypatch.setattr(ans_mod, "get_lead_by_call_id", _by_call)
    monkeypatch.setattr(ans_mod, "get_template_by_id", _template)

    result = await ans_mod.resolve_call_templates("CA-1", "+1", "+2")
    assert result["is_outbound"] is True
