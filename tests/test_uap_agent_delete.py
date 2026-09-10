"""Rider deletes a payment agent: the /agents/delete route and the Setup
path that must never re-adopt the Juspay agent behind an INACTIVE row."""

from typing import Any, Dict, List, Optional, cast

import pytest
from fastapi import HTTPException

from app.api.routers.breeze_buddy.preview.uap import handlers
from app.crm.preview.uap.schemas import CrmCustomerAgent
from app.services.preview.uap.api import JuspayCredentials

CUSTOMER = "0dd139fc-3569-4c78-b686-e42f0e6f61fa"
PREFIX = f"agent_{CUSTOMER}_"


class _Scope:
    reseller_id = "breeze"
    merchant_id = "chennai_one"
    customer_id = CUSTOMER


CREDS = cast(JuspayCredentials, object())
SCOPE = cast(handlers.Scope, _Scope())


def _row(
    ref: str, status: str, agent_id: Optional[str] = "cont_old"
) -> CrmCustomerAgent:
    return CrmCustomerAgent(
        id="11111111-1111-1111-1111-111111111111",
        merchant_id="chennai_one",
        customer_id=CUSTOMER,
        agent_obj_ref=ref,
        action_obj_ref=f"intent_{ref}",
        agent_id=agent_id,
        action_id="cont_x",
        payer_avpa="a@upi",
        status=status,
        action_status="ACTIVE",
    )


def _juspay_agent(ref: str, agent_id: str) -> Dict[str, Any]:
    return {
        "object_reference_id": ref,
        "agent_id": agent_id,
        "status": "ACTIVE",
        "actions": [
            {
                "object_reference_id": f"intent_{ref}",
                "status": "ACTIVE",
                "action_id": "c",
            }
        ],
    }


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    agents: List[Dict[str, Any]],
    rows: Dict[str, CrmCustomerAgent],
):
    """Juspay lists ``agents``; our store holds ``rows`` (by ref, and by
    agent_id for the fallback lookup). Records what got adopted/refreshed."""
    calls: Dict[str, List[str]] = {"created": [], "refreshed": []}

    async def list_agents(_creds, _cid):
        return agents

    async def by_ref(ref):
        return rows.get(ref)

    async def by_agent_id(agent_id):
        return next((r for r in rows.values() if r.agent_id == agent_id), None)

    async def create_attempt(**kw):
        calls["created"].append(kw["agent_obj_ref"])
        return _row(kw["agent_obj_ref"], "PENDING", agent_id=None)

    async def refresh_agent(_creds, row):
        calls["refreshed"].append(row.agent_obj_ref)
        return _row(row.agent_obj_ref, "ACTIVE", agent_id="cont_new")

    monkeypatch.setattr(handlers.uap_api, "list_agents", list_agents)
    monkeypatch.setattr(handlers, "get_by_agent_obj_ref", by_ref)
    monkeypatch.setattr(handlers, "get_by_agent_id", by_agent_id)
    monkeypatch.setattr(handlers, "create_attempt", create_attempt)
    monkeypatch.setattr(handlers, "refresh_agent", refresh_agent)
    return calls


async def test_reuse_skips_inactive_matched_by_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = PREFIX + "1"
    calls = _wire(
        monkeypatch, [_juspay_agent(ref, "cont_1")], {ref: _row(ref, "INACTIVE")}
    )
    assert await handlers._reuse_existing(CREDS, SCOPE, "cth_1") is None
    assert calls == {"created": [], "refreshed": []}


async def test_reuse_skips_inactive_matched_by_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Juspay echoes a ref we do not hold, but the agent_id is on a deleted row.
    ref = PREFIX + "2"
    calls = _wire(
        monkeypatch,
        [_juspay_agent(ref, "cont_2")],
        {PREFIX + "other": _row(PREFIX + "other", "INACTIVE", agent_id="cont_2")},
    )
    assert await handlers._reuse_existing(CREDS, SCOPE, "cth_1") is None
    assert calls == {"created": [], "refreshed": []}


async def test_reuse_still_adopts_unknown_and_refreshes_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown, known, dead = PREFIX + "3", PREFIX + "4", PREFIX + "5"
    calls = _wire(
        monkeypatch,
        [
            _juspay_agent(dead, "cont_5"),
            _juspay_agent(unknown, "cont_3"),
            _juspay_agent(known, "cont_4"),
            {
                "object_reference_id": "agent_stranger_1",
                "agent_id": "z",
                "status": "ACTIVE",
            },
        ],
        {
            dead: _row(dead, "INACTIVE", agent_id="cont_5"),
            known: _row(known, "ACTIVE", agent_id="cont_4"),
        },
    )
    got = await handlers._reuse_existing(CREDS, SCOPE, "cth_1")
    assert got is not None and got.agent_obj_ref == unknown
    assert calls == {"created": [unknown], "refreshed": [unknown]}


async def test_delete_route_scoped_to_rider(monkeypatch: pytest.MonkeyPatch) -> None:
    deactivated: List[tuple] = []

    async def scope(_sid, **_kw):
        return SCOPE

    async def deactivate_agent(merchant_id, customer_id, ref):
        deactivated.append((merchant_id, customer_id, ref))
        return ref == PREFIX + "mine"

    async def list_drawable(_m, _c):
        return [_row(PREFIX + "left", "ACTIVE")]

    async def view(_scope, agent, selected):
        return {"agent_ref": agent.agent_obj_ref, "selected": selected}

    monkeypatch.setattr(handlers, "_scope", scope)
    monkeypatch.setattr(handlers, "deactivate_agent", deactivate_agent)
    monkeypatch.setattr(handlers, "list_drawable_for_customer", list_drawable)
    monkeypatch.setattr(handlers, "_agent_view", view)

    body = handlers.SelectAgentRequest(
        session_id="sess-12345678", agent_ref=PREFIX + "mine"
    )
    out = await handlers.delete_agent_for_rider(body)
    assert out == {
        "agents": [{"agent_ref": PREFIX + "left", "selected": True}],
        "count": 1,
    }
    assert deactivated == [("chennai_one", CUSTOMER, PREFIX + "mine")]

    other = handlers.SelectAgentRequest(
        session_id="sess-12345678", agent_ref="agent_someone_else_1"
    )
    with pytest.raises(HTTPException) as exc:
        await handlers.delete_agent_for_rider(other)
    assert exc.value.status_code == 404
