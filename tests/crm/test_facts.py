"""assert_facts internals: the evidence ladder's winner logic, the canon's
inferred-confidence cap (0.5), and the drift-only append that keeps the
assertion history a record of evidence rather than of traffic."""

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest

import app.crm.identity.facts as facts
from app.crm.identity.db import DbTxn
from app.crm.identity.facts import _winner, claim_confidence


def _claim(v: str, e: str, at: str, k: float = 1.0) -> dict:
    return {"v": v, "e": e, "k": k, "at": at}


def test_higher_evidence_wins() -> None:
    claims = [
        _claim("Ravi", "declared", "2026-08-01T00:00:00+00:00"),
        _claim("R. Kumar", "imported", "2026-08-20T00:00:00+00:00"),
    ]
    assert _winner(claims)["v"] == "Ravi"


def test_tie_breaks_to_newest() -> None:
    claims = [
        _claim("Ravi", "observed", "2026-08-01T00:00:00+00:00"),
        _claim("Ravi K", "observed", "2026-08-20T00:00:00+00:00"),
    ]
    assert _winner(claims)["v"] == "Ravi K"


def test_unknown_evidence_ranks_lowest() -> None:
    claims = [
        _claim("Ravi", "declared", "2026-08-01T00:00:00+00:00"),
        _claim("X", "someday-class", "2026-08-20T00:00:00+00:00"),
    ]
    assert _winner(claims)["v"] == "Ravi"


def test_inferred_confidence_capped() -> None:
    assert claim_confidence("inferred", None) <= 0.5
    assert claim_confidence("inferred", 0.9) == 0.5


def test_declared_defaults_full_confidence() -> None:
    assert claim_confidence("declared", None) == 1.0


def test_confidence_clamped_to_unit_interval() -> None:
    assert claim_confidence("observed", 1.7) == 1.0
    assert claim_confidence("observed", -0.2) == 0.0


# --- drift-only append -----------------------------------------------------
#
# The worker asserts on EVERY event, and the buddy mirrors put the
# customer's name on all four voice topics. Appending unconditionally
# grew one call cycle into 3-4 identical history entries, forever, and
# made every later assertion rewrite an ever-longer jsonb array under a
# row lock -- worst on exactly the busiest customers.


class _FakeAccessor:
    """Stands in for identity/db/accessor: holds one customer's attributes
    and records what the atom wrote."""

    def __init__(self, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.attributes: Dict[str, Any] = attributes if attributes is not None else {}
        self.writes: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    async def fetch_attributes_for_update(
        self, conn: Any, merchant_id: str, customer_id: str
    ) -> Dict[str, Any]:
        return {"attributes": json.loads(json.dumps(self.attributes))}

    async def update_attributes(
        self,
        conn: Any,
        merchant_id: str,
        customer_id: str,
        attributes_json: str,
        materialized: Dict[str, Any],
    ) -> None:
        self.attributes = json.loads(attributes_json)
        self.writes.append((self.attributes, materialized))


def _assert(
    fake: _FakeAccessor,
    value: str,
    evidence: str = "observed",
    source: str = "lead-api",
    at: str = "2026-08-27T08:12:35+00:00",
    attribute: str = "name",
) -> None:
    asyncio.run(
        facts._assert_facts_in_txn(
            cast(DbTxn, object()),
            "m1",
            "cust-1",
            {attribute: value},
            evidence,
            source,
            at,
            1.0,
        )
    )


def test_repeating_an_identical_claim_appends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAccessor()
    monkeypatch.setattr(facts, "accessor", fake)

    _assert(fake, "Rhea", at="2026-08-27T08:12:35.946+00:00")
    _assert(fake, "Rhea", at="2026-08-27T08:12:35.950+00:00")
    _assert(fake, "Rhea", at="2026-08-27T08:12:35.953+00:00")

    assert len(fake.attributes["name"]) == 1


def test_a_changed_value_is_drift_and_is_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAccessor()
    monkeypatch.setattr(facts, "accessor", fake)

    _assert(fake, "Rhea", at="2026-08-01T00:00:00+00:00")
    _assert(fake, "Rhea Kapoor", at="2026-08-20T00:00:00+00:00")

    claims = fake.attributes["name"]
    assert [c["v"] for c in claims] == ["Rhea", "Rhea Kapoor"]
    # Same evidence class, so the tie breaks to the newest claim.
    assert fake.writes[-1][1] == {"display_name": "Rhea Kapoor"}


def test_a_stronger_evidence_class_is_drift_even_at_the_same_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The value did not change but our confidence in it did -- that is
    exactly what the history exists to record."""
    fake = _FakeAccessor()
    monkeypatch.setattr(facts, "accessor", fake)

    _assert(fake, "Rhea", evidence="observed")
    _assert(fake, "Rhea", evidence="declared")

    claims = fake.attributes["name"]
    assert [c["e"] for c in claims] == ["observed", "declared"]


def test_a_different_producer_is_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two independent sources agreeing is corroboration worth keeping."""
    fake = _FakeAccessor()
    monkeypatch.setattr(facts, "accessor", fake)

    _assert(fake, "Rhea", source="lead-api")
    _assert(fake, "Rhea", source="telephony")

    assert [c["src"] for c in fake.attributes["name"]] == ["lead-api", "telephony"]


def test_a_value_returning_after_a_change_is_appended_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift is measured against the LATEST claim, not the whole history:
    a value coming back is a real event and must flip the winner."""
    fake = _FakeAccessor()
    monkeypatch.setattr(facts, "accessor", fake)

    _assert(fake, "Rhea", at="2026-08-01T00:00:00+00:00")
    _assert(fake, "R. Kapoor", at="2026-08-02T00:00:00+00:00")
    _assert(fake, "Rhea", at="2026-08-03T00:00:00+00:00")

    assert [c["v"] for c in fake.attributes["name"]] == ["Rhea", "R. Kapoor", "Rhea"]
    assert fake.writes[-1][1] == {"display_name": "Rhea"}


def test_inferred_winner_never_materializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canon T05: a guess may steer, never decide."""
    fake = _FakeAccessor()
    monkeypatch.setattr(facts, "accessor", fake)

    _assert(fake, "Maybe Rhea", evidence="inferred")

    assert len(fake.attributes["name"]) == 1
    assert fake.writes[-1][1] == {}
