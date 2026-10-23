"""The conversations wiring: worker_main registers the projector and the
"Buddy moved" consumer."""

from app.crm.conversations import contracts

# --- the wiring --------------------------------------------------------------


def test_the_consumers_are_registered() -> None:
    import app.crm.worker_main as worker_main  # noqa: F401 — registration runs at import
    from app.crm.record.consumers import consumers

    registered = consumers()
    assert contracts.consume_conversation_event in registered
    assert contracts.consume_buddy_moved in registered
