"""W2 admission guards (canon: entry carries reenter + cooldown, enforced
for both doors) and arrival scheduling — pure decide functions."""

from datetime import datetime, timedelta, timezone

from app.crm.outreach.enrol import _admission, _first_wake
from app.crm.outreach.schemas import WorkflowDefinition

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


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
    admit, reason = _admission(_definition(), 0, None, NOW)
    assert admit and reason == "admitted"


def test_reenter_disabled_blocks_second_run() -> None:
    admit, reason = _admission(
        _definition(reenter=False), 1, NOW - timedelta(days=2), NOW
    )
    assert not admit and reason == "reenter_disabled"


def test_cooldown_blocks_inside_window() -> None:
    admit, reason = _admission(
        _definition(cooldown_hours=24), 1, NOW - timedelta(hours=5), NOW
    )
    assert not admit and reason == "cooldown_active"


def test_cooldown_admits_after_window() -> None:
    admit, reason = _admission(
        _definition(cooldown_hours=24), 1, NOW - timedelta(hours=25), NOW
    )
    assert admit


def test_first_wake_of_wait_node_is_arrival_plus_delay() -> None:
    assert _first_wake(_definition(), NOW) == NOW + timedelta(minutes=30)


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
    assert _first_wake(definition, NOW) == NOW + timedelta(minutes=30)


def test_first_wake_of_action_node_is_immediate() -> None:
    definition = _definition(
        first_node={"id": "call-now", "type": "call", "template_id": "t"}
    )
    assert _first_wake(definition, NOW) == NOW


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
