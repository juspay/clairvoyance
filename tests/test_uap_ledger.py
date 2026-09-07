"""Session ledger: the pure settle rule (NY paymentStatus -> our draw status)."""

from app.services.uap.ledger import settled_status


def test_settled_status() -> None:
    assert settled_status("PENDING", "PAID") == "CHARGED"
    assert settled_status("PENDING", "FAILED") == "FAILED"
    assert settled_status("PENDING", "NEW") == "PENDING"
    assert settled_status("PENDING", "") == "PENDING"
    # only a PENDING draw settles; a charged/refused one never moves
    assert settled_status("CHARGED", "FAILED") == "CHARGED"
    assert settled_status("REFUSED", "PAID") == "REFUSED"
