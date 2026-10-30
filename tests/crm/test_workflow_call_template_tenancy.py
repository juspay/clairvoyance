"""A call square may use only its merchant's template, or a shared template
of the merchant's own reseller (N-CRM-2)."""

from typing import Any, List, Optional

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach.nodes.spec import NodeParked
from tests.crm.test_workflow_call_visits import (
    _DEFINITION,
    _NODE,
    _install,
    _merchant_under,
    _run,
)


def _template(reseller_id: str, merchant_id: Optional[str]) -> Any:
    async def fake_template(_id: str) -> Any:
        return type(
            "T",
            (),
            {
                "id": "tpl-1",
                "name": "nudge",
                "reseller_id": reseller_id,
                "merchant_id": merchant_id,
            },
        )()

    return fake_template


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    template_reseller: str,
    template_merchant: Optional[str],
    merchant_lookup: Any,
) -> List[str]:
    inserted: List[str] = []
    _install(monkeypatch, inserted)
    monkeypatch.setattr(
        call_node,
        "get_template_by_id",
        _template(template_reseller, template_merchant),
    )
    monkeypatch.setattr(
        call_node, "get_merchant_by_merchant_identifier", merchant_lookup
    )
    return inserted


async def test_another_resellers_shared_template_parks_and_dials_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted = _setup(
        monkeypatch,
        template_reseller="r-other",
        template_merchant=None,
        merchant_lookup=_merchant_under("r1"),
    )

    with pytest.raises(NodeParked, match="another reseller"):
        await call_node.execute(_run(), _NODE, _DEFINITION)
    assert inserted == [], "no lead is minted under the other reseller"


async def test_own_resellers_shared_template_dials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minted: List[dict] = []
    inserted: List[str] = []
    _install(monkeypatch, inserted, minted=minted)
    monkeypatch.setattr(call_node, "get_template_by_id", _template("r1", None))

    await call_node.execute(_run(), _NODE, _DEFINITION)

    assert len(inserted) == 1
    assert minted[0]["reseller_id"] == "r1"


@pytest.mark.parametrize(
    "merchant_lookup",
    [
        pytest.param(_merchant_under(None), id="merchant-with-no-reseller"),
        pytest.param(lambda _m: _none(), id="unknown-merchant"),
    ],
)
async def test_a_merchant_without_a_known_reseller_gets_no_shared_template(
    monkeypatch: pytest.MonkeyPatch, merchant_lookup: Any
) -> None:
    """Fail closed: no reseller to match means no shared template."""
    inserted = _setup(
        monkeypatch,
        template_reseller="r1",
        template_merchant=None,
        merchant_lookup=merchant_lookup,
    )

    with pytest.raises(NodeParked, match="another reseller"):
        await call_node.execute(_run(), _NODE, _DEFINITION)
    assert inserted == []


async def _none() -> None:
    return None


async def test_another_merchants_template_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted = _setup(
        monkeypatch,
        template_reseller="r1",
        template_merchant="m-other",
        merchant_lookup=_merchant_under("r1"),
    )

    with pytest.raises(NodeParked, match="another merchant"):
        await call_node.execute(_run(), _NODE, _DEFINITION)
    assert inserted == []


async def test_own_merchant_template_dials_without_a_reseller_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lookup(_merchant_id: str) -> Any:
        raise AssertionError("a merchant's own template needs no reseller lookup")

    inserted = _setup(
        monkeypatch,
        template_reseller="r1",
        template_merchant="m1",
        merchant_lookup=no_lookup,
    )

    await call_node.execute(_run(), _NODE, _DEFINITION)
    assert len(inserted) == 1


async def test_a_merchant_lookup_error_raises_instead_of_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB error is a bad moment the walker retries, not a verdict."""

    async def broken(_merchant_id: str) -> Any:
        raise RuntimeError("pool exhausted")

    inserted = _setup(
        monkeypatch,
        template_reseller="r1",
        template_merchant=None,
        merchant_lookup=broken,
    )

    with pytest.raises(RuntimeError, match="pool exhausted"):
        await call_node.execute(_run(), _NODE, _DEFINITION)
    assert inserted == []
