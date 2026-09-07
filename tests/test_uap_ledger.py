"""Session ledger: the pure settle rule (NY paymentStatus -> our draw status)
and the shape of the writes behind it."""

from app.services.preview.uap.ledger import settled_status
from app.services.preview.uap.queries import (
    session_draws_query,
    settle_session_draw_query,
)


def test_settle_is_keyed_by_order_id_not_journey() -> None:
    # A journey can carry several draws (a retry after a failed one); each
    # is its own Juspay order and settles on its own.
    sql, values = settle_session_draw_query("s1", "ord_1", {"status": "CHARGED"})
    assert "d ->> 'order_id' = $2" in sql and "journey_id" not in sql
    assert values[:2] == ["s1", "ord_1"]
    sql, values = session_draws_query("s1")
    assert "uap_draws" in sql and values == ["s1"]


def test_settled_status() -> None:
    assert settled_status("PENDING", "PAID") == "CHARGED"
    assert settled_status("PENDING", "FAILED") == "FAILED"
    assert settled_status("PENDING", "NEW") == "PENDING"
    assert settled_status("PENDING", "") == "PENDING"
    # only a PENDING draw settles; a charged/refused one never moves
    assert settled_status("CHARGED", "FAILED") == "CHARGED"
    assert settled_status("REFUSED", "PAID") == "REFUSED"
