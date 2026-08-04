"""Regression: a ``status:"failed"`` tool result is a FAILURE everywhere.

The RFC-002 bookkeeping (plan enforcer advance, render_ui force-arming) and
the step verifiers must read failure the same way the step rail, binding
store, and reducers do — via the canonical ``_is_tool_success``, which
treats BOTH ``"error"`` and ``"failed"`` as failure. A backend body with
``{"status":"failed"}`` on an HTTP 200 reaches this bookkeeping intact
(result_normalizer only unwraps the transport envelope on "success"), so a
narrow ``== "error"`` check would count it as success and silently advance
a plan past a failed step while the widget paints it red.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.steps.enforcer import PlanEnforcer
from app.ai.voice.agents.breeze_buddy.chat.steps.verification import (
    register_tool_verifier,
    run_tool_verifiers,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import _is_tool_success


def test_failed_status_reads_as_not_success():
    # The exact read cycle.py::call_success now uses.
    assert _is_tool_success({"status": "failed", "error": "declined"}) is False
    assert _is_tool_success({"status": "error"}) is False
    assert _is_tool_success({"status": "success", "data": {}}) is True
    assert _is_tool_success({"id": "cart-1"}) is True  # no status → success
    assert _is_tool_success("plain string") is True  # non-dict → success


def test_verifier_skips_failed_result():
    calls = {"n": 0}

    def _verifier(_args, _result):
        calls["n"] += 1
        return "should never run on a failed result"

    register_tool_verifier("probe_failed_skip", _verifier)
    # A failed backend body must skip verification entirely — the tool
    # already failed; there is nothing to post-condition.
    assert run_tool_verifiers("probe_failed_skip", {}, {"status": "failed"}) is None
    assert calls["n"] == 0
    # A successful result still runs the verifier.
    assert (
        run_tool_verifiers("probe_failed_skip", {}, {"ok": True})
        == "should never run on a failed result"
    )
    assert calls["n"] == 1


def test_plan_enforcer_does_not_advance_past_failed_step():
    enforcer = PlanEnforcer()
    armed = enforcer.start(
        ["search_records", "build_report"],
        known_tools={"search_records", "build_report"},
    )
    assert armed
    # A failed first step must NOT advance the cursor (retry stays on it) —
    # this is the polarity cycle.py feeds from _is_tool_success.
    enforcer.on_tool_result("search_records", _is_tool_success({"status": "failed"}))
    assert enforcer.current_step == "search_records"
    # A success advances.
    enforcer.on_tool_result("search_records", _is_tool_success({"ok": True}))
    assert enforcer.current_step == "build_report"
