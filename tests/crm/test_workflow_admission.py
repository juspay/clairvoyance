"""W2 admission guards (canon: entry carries reenter + cooldown, enforced
for both doors) and arrival scheduling — pure decide functions."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, cast
from uuid import UUID, uuid4

import pytest

import app.crm.outreach.enrol as enrol_mod
from app.crm.outreach.db import DbTxn
from app.crm.outreach.enrol import _admission, _first_wake
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowDefinition

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
CUSTOMER = str(uuid4())


def _definition(reenter: bool = True, cooldown_hours: float = 0.0, first_node=None):
    return WorkflowDefinition.model_validate(
        {
            "entry": {
                "topic": "checkout.initiated",
                "reenter": reenter,
                "cooldown_hours": cooldown_hours,
            },
            "nodes": [
                first_node or {"id": "wait-30m", "type": "wait", "minutes": 30},
            ],
            "edges": [],
            "goal": {"topics": ["order.placed"]},
        }
    )


def test_first_run_admits() -> None:
    admit, reason = _admission(_definition().entries[0], 0, None, NOW)
    assert admit and reason == "admitted"


def test_reenter_disabled_blocks_second_run() -> None:
    admit, reason = _admission(
        _definition(reenter=False).entries[0], 1, NOW - timedelta(days=2), NOW
    )
    assert not admit and reason == "reenter_disabled"


def test_cooldown_blocks_inside_window() -> None:
    admit, reason = _admission(
        _definition(cooldown_hours=24).entries[0], 1, NOW - timedelta(hours=5), NOW
    )
    assert not admit and reason == "cooldown_active"


def test_cooldown_admits_after_window() -> None:
    admit, reason = _admission(
        _definition(cooldown_hours=24).entries[0], 1, NOW - timedelta(hours=25), NOW
    )
    assert admit


def test_first_wake_of_wait_node_is_arrival_plus_delay() -> None:
    assert _first_wake(_definition().nodes[0], NOW) == NOW + timedelta(minutes=30)


def test_first_wake_of_wait_event_node_is_arrival_plus_delay() -> None:
    # The MAJOR from the 31 Aug review: a plan whose FIRST square listens
    # (wait_event) used to enrol with wake_at = now — the walker claimed
    # it at once, saw no reply, took the timeout edge, and the listening
    # window was silently zero.
    definition = _definition(
        first_node={
            "id": "listen",
            "type": "wait_event",
            "topics": ["payment.confirmed"],
            "key": "status",
            "minutes": 30,
        }
    )
    assert _first_wake(definition.nodes[0], NOW) == NOW + timedelta(minutes=30)


def test_first_wake_of_action_node_is_immediate() -> None:
    definition = _definition(
        first_node={"id": "call-now", "type": "call", "template_id": "t"}
    )
    assert _first_wake(definition.nodes[0], NOW) == NOW


def test_context_passthrough_keeps_scalars_drops_structures() -> None:
    """The template-variable bridge: standard identity keys + the
    merchant's scalar facts ride to the lead payload; nested payload
    stays on the event row (pointers, not photocopies)."""
    from app.crm.outreach.entry import _context_from_payload

    context = _context_from_payload(
        {
            "customer_mobile_number": "+919845012345",
            "customer_name": "Priya",
            "item": "washing machine",
            "cart_value": 3499,
            "gift_wrap": True,
            "line_items": [{"sku": "WM-1"}],  # nested -> dropped
            "huge": "x" * 500,  # oversized -> dropped
        }
    )
    assert context["item"] == "washing machine"
    assert context["cart_value"] == 3499
    assert context["gift_wrap"] is True
    assert "line_items" not in context and "huge" not in context


def test_context_phone_is_normalized_for_the_send_path() -> None:
    # resolve() normalizes what it PROBES on, but context is a separate
    # copy and it is what the call and send nodes actually dial. Left raw,
    # identity would resolve to +919876543210 while the node dialled the
    # bare form — and a suppression stored in E.164 would not match it,
    # which is the one failure normalize-at-every-writer exists to stop.
    from app.crm.outreach.entry import _phone_from_payload

    assert (
        _phone_from_payload({"customer_mobile_number": "9876543210"}) == "+919876543210"
    )
    assert _phone_from_payload({"phone": "+91 98765 43210"}) == "+919876543210"
    assert (
        _phone_from_payload({"customer": {"phone": "09876543210"}}) == "+919876543210"
    )
    # Unparseable is handed through, not dropped: the node then parks with
    # a clear reason, which beats losing the number at this seam.
    assert _phone_from_payload({"phone": "n/a"}) == "n/a"
    assert _phone_from_payload({}) is None


# --- rollout phase 02 (B2): keyed plans judge admission per key, not per customer ---


def _workflow(key: Optional[str], reenter: bool = False) -> Workflow:
    entry: Dict[str, Any] = {"topic": "orders/create", "reenter": reenter}
    if key:
        entry["key"] = key
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="wismo",
        status="live",
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition={
            "entry": entry,
            "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
            "edges": [],
            "goal": {"topics": ["order.delivered"]},
        },
        draft=None,
    )


_REAL_ACCESSOR = enrol_mod.accessor


class _History:
    """The accessor slice _enrol_in_txn touches. The customer has ONE run
    on this plan, five minutes old, keyed ORD-1; ORD-2 has never run."""

    def __init__(self) -> None:
        self.judged: List[Optional[str]] = []
        self.inserted: List[str] = []
        self.locked: List[List[Any]] = []
        self.order: List[str] = []

    async def source_event_used(self, conn: Any, *args: Any) -> bool:
        return False

    async def lock_templates_shared(
        self, conn: Any, merchant_id: str, templates: List[Any]
    ) -> None:
        self.locked.append(list(templates))
        self.order.append("lock")

    async def admission_facts(
        self,
        conn: Any,
        merchant_id: str,
        workflow_id: str,
        customer_id: str,
        enrollment_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.judged.append(enrollment_key)
        if enrollment_key in (None, "ORD-1"):
            return {"runs": 1, "latest_entered_at": NOW - timedelta(minutes=5)}
        return {"runs": 0, "latest_entered_at": None}

    async def insert_enrollment(
        self,
        conn: Any,
        merchant_id: str,
        workflow_id: str,
        workflow_version: int,
        customer_id: str,
        current_node: str,
        wake_at: datetime,
        context: Dict[str, Any],
        enrollment_key: str,
    ) -> EnrollmentRun:
        self.inserted.append(enrollment_key)
        self.order.append("insert")
        return EnrollmentRun(
            id=uuid4(),
            merchant_id=merchant_id,
            workflow_id=UUID(workflow_id),
            workflow_version=workflow_version,
            customer_id=UUID(customer_id),
            status="waiting",
            current_node=current_node,
            wake_at=wake_at,
            entered_at=NOW,
            exited_at=None,
            exit_reason=None,
            context=context,
            enrollment_key=enrollment_key,
            attempts=0,
            last_error=None,
        )


def _enrol(history: _History, workflow: Workflow, key: str) -> Optional[EnrollmentRun]:
    definition = WorkflowDefinition.model_validate(workflow.definition)
    return asyncio.run(
        enrol_mod._enrol_in_txn(
            cast(DbTxn, object()),
            "m1",
            workflow,
            definition,
            definition.entries[0],
            CUSTOMER,
            {},
            key,
        )
    )


def test_a_keyed_plan_admits_a_new_key_despite_the_customers_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: with the defaults (reenter False, cooldown 24h) the customer's
    second order was refused as reenter_disabled, because admission
    counted ALL her runs on the plan. "Has this ORDER ever run" is what
    entry.key declared — a new order id has no history, so it is admitted."""
    history = _History()
    monkeypatch.setattr(enrol_mod, "accessor", history)
    run = _enrol(history, _workflow(key="order_id"), "ORD-2")
    assert run is not None and history.inserted == ["ORD-2"]
    assert history.judged == ["ORD-2"]  # judged per key, never per customer


def test_a_keyed_plan_still_refuses_a_key_that_already_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _History()
    monkeypatch.setattr(enrol_mod, "accessor", history)
    assert _enrol(history, _workflow(key="order_id"), "ORD-1") is None
    assert history.inserted == [] and history.judged == ["ORD-1"]


def test_an_unkeyed_plan_keeps_judging_the_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No entry.key: the key IS the customer id and the guards read her
    whole history on the plan, exactly as before."""
    history = _History()
    monkeypatch.setattr(enrol_mod, "accessor", history)
    assert _enrol(history, _workflow(key=None), CUSTOMER) is None
    assert history.inserted == [] and history.judged == [None]


def test_enrol_holds_the_templates_the_document_sends_before_the_insert() -> None:
    """Phase 14: the run being born may send these templates, so the enrol
    atom takes the template lock SHARED for each of them (shared/locks.py)
    before its insert — a retirement's EXCLUSIVE lock then waits for this
    row to commit and counts it, instead of racing past it."""
    history = _History()
    enrol_mod.accessor = history  # type: ignore[assignment]
    try:
        workflow = Workflow(
            id=uuid4(),
            merchant_id="m1",
            name="nudge",
            status="live",
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            draft=None,
            definition={
                "entry": {"topic": "checkouts/update", "key": "order_id"},
                "nodes": [
                    {"id": "wait-30m", "type": "wait", "minutes": 30},
                    {
                        "id": "wa",
                        "type": "send",
                        "channel": "whatsapp",
                        "template": "cart_recovery_1",
                    },
                ],
                "edges": [["wait-30m", "wa"]],
                "goal": {"topics": ["orders/create"]},
                "purpose_key": "marketing.cart.recovery",
            },
        )
        run = _enrol(history, workflow, "ORD-2")
    finally:
        enrol_mod.accessor = _REAL_ACCESSOR  # type: ignore[assignment]
    assert run is not None
    assert history.locked == [[("whatsapp", "cart_recovery_1")]]
    assert history.order == ["lock", "insert"]
