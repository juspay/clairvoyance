"""uap credential resolution: merchant row > reseller row > global row."""

from types import SimpleNamespace as Row

from app.services.preview.uap.api import pick_uap_row


def _row(name="uap", reseller_id=None, merchant_id=None, active=True):
    return Row(
        name=name, reseller_id=reseller_id, merchant_id=merchant_id, is_active=active
    )


def test_merchant_row_wins_over_reseller_and_global() -> None:
    g, r, m = (
        _row(),
        _row(reseller_id="breeze"),
        _row(reseller_id="breeze", merchant_id="chennai_one"),
    )
    assert pick_uap_row([g, r, m], "uap", "chennai_one") is m
    assert pick_uap_row([m, r, g], "uap", "chennai_one") is m  # order-independent


def test_falls_back_to_reseller_then_global() -> None:
    g, r = _row(), _row(reseller_id="breeze")
    assert pick_uap_row([g, r], "uap", "chennai_one") is r
    assert pick_uap_row([g], "uap", "chennai_one") is g
    assert pick_uap_row([], "uap", "chennai_one") is None


def test_other_merchants_rows_and_inactive_rows_are_ignored() -> None:
    other = _row(reseller_id="breeze", merchant_id="someone_else")
    dead = _row(reseller_id="breeze", merchant_id="chennai_one", active=False)
    r = _row(reseller_id="breeze")
    assert pick_uap_row([other, dead, r], "uap", "chennai_one") is r
    assert (
        pick_uap_row([_row(name="shopify", reseller_id="breeze")], "uap", "chennai_one")
        is None
    )
