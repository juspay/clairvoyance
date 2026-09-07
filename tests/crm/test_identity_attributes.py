"""identity.attributes — the keyed store other modules use on crm_customer.

The DB is faked at the accessor seam: what is tested is the door's own
rules (tenant guard before any write, the document handed back, the SQL
shape the GIN index needs)."""

from typing import Any, Dict, List, Optional

import pytest

import app.crm.identity.attributes as attributes
from app.crm.identity.db.queries import (
    find_customer_by_attribute_query,
    select_attributes_by_id_query,
    select_attributes_for_update_by_id_query,
)

# ---- query shapes ----


def test_find_by_attribute_is_whole_document_containment() -> None:
    sql, values = find_customer_by_attribute_query("agents", '[{"agent_id": "x"}]')
    assert "attributes @> jsonb_build_object($1::text, $2::jsonb)" in sql
    assert "attributes -> $1" not in sql  # that form cannot use the GIN index
    assert values == ["agents", '[{"agent_id": "x"}]']


def test_by_id_reads_are_merchantless_and_the_mutating_one_locks() -> None:
    plain, _ = select_attributes_by_id_query("c1")
    locked, values = select_attributes_for_update_by_id_query("c1")
    assert "FOR UPDATE" not in plain and "FOR UPDATE" in locked
    assert "merchant_id =" not in locked and values == ["c1"]


# ---- the door, over a faked accessor ----


class _Fake:
    """One customer row; records every write."""

    def __init__(self, merchant_id: str, attrs: Any) -> None:
        self.row: Optional[Dict[str, Any]] = {
            "id": "c1",
            "merchant_id": merchant_id,
            "attributes": attrs,
        }
        self.writes: List[Any] = []

    async def fetch_attributes_for_update_by_id(self, _txn, customer_id):
        return self.row if customer_id == "c1" else None

    async def update_attributes(self, _txn, merchant_id, customer_id, doc, mat):
        self.writes.append((merchant_id, customer_id, doc, mat))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    fake = _Fake("m1", '{"agents": [1], "other": true}')
    monkeypatch.setattr(attributes, "accessor", fake)

    async def run_inline(fn, *args):
        return await fn(None, *args)

    monkeypatch.setattr(attributes, "atomically", run_inline)
    return fake


async def test_mutation_sees_current_value_and_writes_the_whole_document(
    fake: _Fake,
) -> None:
    def mutation(current):
        return (current or []) + [2], "done"

    result = await attributes.mutate_customer_attribute("c1", "agents", mutation)
    assert result == ("m1", "done")
    ((merchant, customer, doc, materialized),) = fake.writes
    assert (merchant, customer, materialized) == ("m1", "c1", {})
    assert '"agents": [1, 2]' in doc and '"other": true' in doc  # other keys kept


async def test_wrong_merchant_is_refused_before_any_write(fake: _Fake) -> None:
    calls = []

    def mutation(current):
        calls.append(current)
        return [], None

    result = await attributes.mutate_customer_attribute(
        "c1", "agents", mutation, merchant_id="someone_else"
    )
    assert result is None and fake.writes == [] and calls == []


async def test_right_merchant_and_unknown_customer(fake: _Fake) -> None:
    ok = await attributes.mutate_customer_attribute(
        "c1", "agents", lambda cur: ([], None), merchant_id="m1"
    )
    assert ok == ("m1", None) and len(fake.writes) == 1
    missing = await attributes.mutate_customer_attribute(
        "nope", "agents", lambda cur: ([], None)
    )
    assert missing is None and len(fake.writes) == 1


def test_attributes_parser_tolerates_strings_and_garbage() -> None:
    assert attributes._attributes('{"a": 1}') == {"a": 1}
    assert attributes._attributes({"a": 1}) == {"a": 1}
    assert attributes._attributes("not json") == {}
    assert attributes._attributes(None) == {}
