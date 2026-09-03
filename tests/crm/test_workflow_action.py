"""The action node (modules/05-outreach): signs and POSTs the run's
Shopify order action(s) — a tag, a note, or both — to whatever webhook
this run/node resolves to — the run's own context first (a per-run
override, same idea as a call's reporting_webhook_url), the node's own
configured value otherwise, never an env var. A missing order id, an
unresolvable URL, or a failed POST all park the node — the honest
outcome, same idiom execute_call/execute_send already use."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest

import app.crm.outreach.nodes as nodes
from app.crm.outreach.nodes import NodeParked, _validate_action, execute_action
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

_DEFINITION = WorkflowDefinition(
    entry={"topic": "checkout.initiated"},
    nodes=[{"id": "tag-vip", "type": "action", "add_shopify_tag": ["vip"]}],
    edges=[],
    goals={"topics": ["order.placed"]},
)


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="tag-vip",
        wake_at=NOW,
        entered_at=NOW - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
    )


def _node(**overrides: Any) -> WorkflowNode:
    fields: Dict[str, Any] = {
        "id": "tag-vip",
        "type": "action",
        "add_shopify_tag": ["vip"],
    }
    fields.update(overrides)
    return WorkflowNode(**fields)


def test_validate_action_needs_a_tag_or_a_note() -> None:
    assert _validate_action(_node(add_shopify_tag=[]), _DEFINITION) == [
        "action node tag-vip needs add_shopify_tag or add_shopify_note"
    ]
    assert _validate_action(_node(), _DEFINITION) == []
    assert (
        _validate_action(
            _node(add_shopify_tag=[], add_shopify_note="vip customer"), _DEFINITION
        )
        == []
    )
    assert (
        _validate_action(
            _node(add_shopify_tag=["vip"], add_shopify_note="vip customer"), _DEFINITION
        )
        == []
    )


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _install_webhook(
    monkeypatch: pytest.MonkeyPatch, calls: List[Dict[str, Any]], ok: bool
) -> None:
    async def fake_send(session: Any, url: str, data: Dict[str, Any]) -> bool:
        calls.append({"url": url, "data": data})
        return ok

    monkeypatch.setattr(nodes, "send_webhook_with_retry", fake_send)
    monkeypatch.setattr(nodes, "create_aiohttp_session", lambda **_: _FakeSession())


async def test_a_missing_order_id_parks_the_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=True)
    run = _run({"reporting_webhook_url": "https://payload.example/hook"})
    with pytest.raises(NodeParked):
        await execute_action(run, _node(), _DEFINITION)
    assert calls == []


async def test_no_resolvable_webhook_url_parks_the_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=True)
    run = _run({"order_id": "o-1001"})
    with pytest.raises(NodeParked):
        await execute_action(run, _node(), _DEFINITION)
    assert calls == []


async def test_the_payloads_reporting_webhook_url_wins_over_the_nodes_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=True)
    run = _run(
        {"order_id": "o-1001", "reporting_webhook_url": "https://payload.example/hook"}
    )
    result = await execute_action(
        run, _node(webhook_url="https://node.example/hook"), _DEFINITION
    )
    assert result == {}
    assert calls[0]["url"] == "https://payload.example/hook"


async def test_the_nodes_own_url_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=True)
    run = _run({"order_id": "o-1001"})
    result = await execute_action(
        run, _node(webhook_url="https://node.example/hook"), _DEFINITION
    )
    assert result == {}
    assert calls[0]["url"] == "https://node.example/hook"


async def test_the_payload_shape_carries_the_tags_and_shopify_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=True)
    run = _run(
        {
            "order_id": "o-1001",
            "reporting_webhook_url": "https://payload.example/hook",
            "vip": True,
        }
    )
    await execute_action(run, _node(add_shopify_tag=["vip", "repeat"]), _DEFINITION)
    data = calls[0]["data"]
    assert data["type"] == "order_action"
    assert data["merchant_id"] == "m1"
    assert data["shopify_order_id"] == "o-1001"
    assert data["add_shopify_tag"] == ["vip", "repeat"]
    assert data["add_shopify_note"] is None
    assert data["run_id"] == str(run.id)
    assert data["node_id"] == "tag-vip"
    assert data["vip"] is True


async def test_the_payload_shape_carries_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=True)
    run = _run(
        {"order_id": "o-1001", "reporting_webhook_url": "https://payload.example/hook"}
    )
    await execute_action(
        run,
        _node(add_shopify_tag=[], add_shopify_note="called, promised a refund"),
        _DEFINITION,
    )
    data = calls[0]["data"]
    assert data["add_shopify_tag"] == []
    assert data["add_shopify_note"] == "called, promised a refund"


async def test_a_failed_webhook_parks_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_webhook(monkeypatch, calls, ok=False)
    run = _run(
        {"order_id": "o-1001", "reporting_webhook_url": "https://payload.example/hook"}
    )
    with pytest.raises(NodeParked):
        await execute_action(run, _node(), _DEFINITION)
    assert len(calls) == 1
