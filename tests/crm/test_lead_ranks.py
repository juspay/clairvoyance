"""ranks_for_leads: the rank a lead already queued has NOW, judged from its run.

The dialler asks before a number starts using ranks, so leads queued before
the CRM wrote a rank on them take their real place and not the default.
"""

from typing import Any, Dict, List
from uuid import uuid4

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach.contracts import ranks_for_leads
from tests.crm.test_call_priority import (
    HOURS,
    NO_WINDOW,
    _context,
    _ist,
    _ms,
    _plan,
)
from tests.crm.test_grant import _install, _World


def _queued(lead_id: str, topic: str, at: Any) -> Dict[str, Any]:
    """A run's context once call-1 has queued `lead_id`."""
    return {**_context(topic, at), "lead_call-1": lead_id}


async def test_a_queued_leads_rank_is_judged_from_its_run_as_it_is_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _install(monkeypatch, _World(_plan(priority=NO_WINDOW)))
    asked: List[str] = []

    async def config(template_id: str) -> Any:
        asked.append(template_id)
        return HOURS

    monkeypatch.setattr(call_node, "get_call_execution_config_by_template_id", config)
    monkeypatch.setattr(call_node, "_now", lambda: _ist(9, 10, 30))
    live_at, night = _ist(9, 10, 5), _ist(9, 2, 0)
    wait = {"current_node": "after-call-1"}
    live = w.waiting(**wait, context=_queued("lead-live", "LINE_OFFERED", live_at))
    pile = w.waiting(**wait, context=_queued("lead-pile", "LINE_KYC_COMPLETED", night))
    gone = w.waiting(
        status="exited",
        exit_reason="goal_met",
        wake_at=None,
        context=_queued("lead-gone", "LINE_OFFERED", live_at),
    )
    # The run has since queued another call: this lead is not its call any more.
    moved = w.waiting(**wait, context=_queued("lead-new", "LINE_OFFERED", live_at))

    ranks = await ranks_for_leads(
        [
            ("lead-live", str(live.id)),
            ("lead-pile", str(pile.id)),
            ("lead-gone", str(gone.id)),
            ("lead-old", str(moved.id)),
            ("lead-lost", str(uuid4())),
        ]
    )

    assert ranks == {
        "lead-live": {
            "rank": 1,
            "order": "first_ready",
            "event_ms": _ms(live_at),
            "next_rank": 3,
            "next_order": "newest_event",
        },
        "lead-pile": {"rank": 2, "order": "newest_event", "event_ms": _ms(night)},
    }
    assert asked == ["tpl-1"]  # the template's hours, read once for the chunk


async def test_a_plan_with_no_priority_gives_no_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _install(monkeypatch, _World(_plan(priority=None)))
    run = w.waiting(context=_queued("lead-1", "LINE_OFFERED", _ist(9, 10, 5)))

    assert await ranks_for_leads([("lead-1", str(run.id))]) == {}
