"""An inbound caller who hangs up while ringing is not an orphan webhook.

Prod, 2026-10-14: two "[P1] orphan call webhook — source: unanswered" pages
for Plivo calls with ``Direction=inbound``, ``CallStatus=cancel``,
``HangupCause=ORIGINATOR_CANCEL`` and an empty ``AnswerTime``. The caller hung
up before Plivo fetched the answer URL, so no lead (and no channel) was ever
created — yet the hangup status callback still arrived, found no lead, and
raised the outbound-only §7.1 duplicate-call alert.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.api.routers.breeze_buddy.telephony.callbacks import handlers as cb_mod
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker

CALL_SID = "45ccad43-ff18-4862-bcf6-36928c96b77c"


class _Harness:
    """Stub everything handle_unanswered_calls touches; record the alert."""

    def __init__(self, lead: Optional[LeadCallTracker] = None) -> None:
        self.lead = lead
        self.alerts: List[Dict[str, Any]] = []
        self.closed: List[Dict[str, Any]] = []
        self.info: List[str] = []
        self.errors: List[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def release_pod(**_kwargs: Any) -> None:
            return None

        async def get_lead(_call_id: str) -> Optional[LeadCallTracker]:
            return self.lead

        async def orphan(**kwargs: Any) -> None:
            self.alerts.append(kwargs)

        async def release_resources(_lead: LeadCallTracker) -> None:
            return None

        async def config(_lead: LeadCallTracker) -> Any:
            # Truthy so the close runs; the inbound branch never retries.
            return SimpleNamespace()

        async def close(**kwargs: Any) -> None:
            self.closed.append(kwargs)

        class _Redis:
            async def delete(self, _key: str) -> None:
                return None

        async def redis() -> _Redis:
            return _Redis()

        monkeypatch.setattr(calls_mod, "safe_release_pod", release_pod)
        monkeypatch.setattr(calls_mod, "get_lead_by_call_id", get_lead)
        monkeypatch.setattr(calls_mod, "raise_orphan_webhook", orphan)
        monkeypatch.setattr(calls_mod, "_release_call_resources", release_resources)
        monkeypatch.setattr(calls_mod, "_get_lead_config", config)
        monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", close)
        monkeypatch.setattr(calls_mod, "get_redis_service", redis)
        # app.core.logger does not propagate to caplog; record lines directly.
        monkeypatch.setattr(
            calls_mod,
            "logger",
            SimpleNamespace(
                info=lambda msg, *a, **k: self.info.append(str(msg)),
                warning=lambda msg, *a, **k: None,
                error=lambda msg, *a, **k: self.errors.append(str(msg)),
            ),
        )


@pytest.mark.asyncio
async def test_inbound_hangup_before_answer_is_not_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No lead + inbound + never answered: one INFO line, no P1, no ERROR."""
    h = _Harness(lead=None)
    h.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(
        CALL_SID, direction="inbound", answered=False
    )

    assert h.alerts == []
    assert h.errors == []
    assert [line for line in h.info if "not an orphan" in line]


@pytest.mark.asyncio
async def test_inbound_direction_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(lead=None)
    h.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(
        CALL_SID, direction="Inbound", answered=False
    )

    assert h.alerts == []


@pytest.mark.asyncio
async def test_outbound_without_lead_still_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §7.1 signal is about outbound and must keep firing there."""
    h = _Harness(lead=None)
    h.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(
        CALL_SID, direction="outbound", answered=False
    )

    assert h.alerts == [{"call_id": CALL_SID, "source": "unanswered"}]


@pytest.mark.asyncio
async def test_missing_direction_still_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-safe: a caller that passes nothing gets today's behaviour."""
    h = _Harness(lead=None)
    h.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_SID)

    assert h.alerts == [{"call_id": CALL_SID, "source": "unanswered"}]
    assert any("channel may be leaked" in line for line in h.errors)


@pytest.mark.asyncio
async def test_inbound_answered_without_lead_still_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answered inbound call did reach the answer URL — no lead there is real."""
    h = _Harness(lead=None)
    h.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(
        CALL_SID, direction="inbound", answered=True
    )

    assert h.alerts == [{"call_id": CALL_SID, "source": "unanswered"}]


@pytest.mark.asyncio
async def test_inbound_with_lead_is_closed_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip only applies when there is no lead; a real row is still closed."""
    lead = LeadCallTracker(
        id="lead-1",
        reseller_id="breeze",
        template="ny-driver-hotline",
        template_id="tmpl-1",
        merchant_id="nammayatri",
        request_id="req-1",
        attempt_count=0,
        payload={},
        status=LeadCallStatus.PROCESSING,
        is_locked=True,
        call_id=CALL_SID,
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.INBOUND,
    )
    h = _Harness(lead=lead)
    h.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(
        CALL_SID, direction="inbound", answered=False
    )

    assert h.alerts == []
    assert [c["outcome"] for c in h.closed] == ["NO_ANSWER"]


# ---------------------------------------------------------------------------
# Status callback: the provider form is where direction and answer time live
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "form,expected",
    [
        # Plivo, verbatim shape from the 2026-10-14 pages.
        ({"Direction": "inbound", "AnswerTime": ""}, ("inbound", False)),
        (
            {"Direction": "inbound", "AnswerTime": "2026-10-14 20:45:01"},
            ("inbound", True),
        ),
        ({"Direction": "outbound", "AnswerTime": ""}, ("outbound", False)),
        # Twilio spellings.
        ({"Direction": "outbound-api"}, ("outbound", False)),
        ({"Direction": "outbound-dial"}, ("outbound", False)),
        # Exotel spelling.
        ({"Direction": "incoming"}, ("inbound", False)),
        # Unknown / missing stay unknown so the handler keeps alerting.
        ({"Direction": "sideways"}, (None, False)),
        ({}, (None, False)),
    ],
)
def test_call_direction_and_answered_are_read_from_the_form(
    form: Dict[str, str], expected: tuple
) -> None:
    assert cb_mod._direction_and_answered(form) == expected
