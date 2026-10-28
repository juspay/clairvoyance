"""Row-kind handles (crm_customer_handle, migration 085) without a database.

Three things are pinned here:

1. The Xyne user id's normal form — what is one person's id and what is a
   placeholder that would staple strangers together.
2. The vocabulary is spelled once (app/crm/shared/handles.py) and every list
   that must know a row kind does: record's identity roles, the walker's
   bookkeeping keys, the never-shown handle lists, the flat reader.
3. resolve()'s statements, recorded on a fake connection. An event that
   carries no row kind runs EXACTLY the statements it ran before 085 —
   the guarantee that Flipkart's phone-only traffic is untouched — and an
   event that does carry one runs the row statements in the order the
   ADR 0021 ladder needs.

The same paths against a real Postgres: test_customer_handle_integration.py.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, get_args

import pytest

from app.crm.identity import facts, resolve as resolve_module
from app.crm.identity.db import queries
from app.crm.outreach import predicates
from app.crm.outreach.nodes.context import _BOOKKEEPING_KEYS
from app.crm.platform.suppression import _platform_pairs
from app.crm.record.extractors import engine, flat
from app.crm.record.schemas import CatalogEntry, CatalogField, IdentityRole
from app.crm.shared.handles import (
    ROW_KIND_NAMES,
    UNSCOPED,
    normalize_row_handle,
    normalize_xyne_user_id,
)

RAVI = "cmh2x9k1p0003qz8w5f4r7t2e"
ASHA = "cmh2xa04m0007qz8l1v6d9b3c"
PHONE = "+919812345678"
MERCHANT = "pragathi"


# --- 1. the Xyne user id's normal form ---------------------------------------


def test_a_xyne_user_id_is_kept_as_xyne_sent_it_minus_the_padding() -> None:
    assert normalize_xyne_user_id(RAVI) == RAVI
    assert normalize_xyne_user_id(f"  {RAVI}\n") == RAVI


@pytest.mark.parametrize(
    "placeholder",
    ["0", "null", "NULL", "None", "undefined", "env-api-user", "ENV-API-USER"],
)
def test_a_placeholder_is_nobodys_id(placeholder: str) -> None:
    """These are the values that would make many people one customer:
    `env-api-user` is the id Xyne answers with when its own user lookup
    fails (apiKeyService.ts), the rest are what an unset field serializes
    to. Each is dropped, like an unparseable phone."""
    assert normalize_xyne_user_id(placeholder) is None


@pytest.mark.parametrize(
    "bad", ["", "   ", "a b", "x/y", "ravi@xyne", "ünï", "a" * 65, "cmh2x9k1p;DROP"]
)
def test_anything_not_shaped_like_an_id_is_dropped(bad: str) -> None:
    assert normalize_xyne_user_id(bad) is None


@pytest.mark.parametrize("not_text", [None, 12345, ["a"], {"id": "a"}])
def test_a_value_that_is_not_text_is_no_id(not_text: Any) -> None:
    assert normalize_xyne_user_id(not_text) is None


def test_the_longest_accepted_id_is_64_characters() -> None:
    assert normalize_xyne_user_id("a" * 64) == "a" * 64


def test_an_unknown_kind_normalizes_to_nothing() -> None:
    assert normalize_row_handle("slack_user_id", "U012AB3CD") is None
    assert normalize_row_handle("xyne_user_id", RAVI) == RAVI


# --- 2. one vocabulary, every list that must know it ---------------------------


def test_every_row_kind_is_an_identity_role_record_can_declare() -> None:
    """record/schemas.py is a leaf and cannot import the vocabulary, so the
    two spellings are pinned here: a kind missing from IdentityRole could
    never be declared by an extractor, and nothing else would say so."""
    assert ROW_KIND_NAMES <= set(get_args(IdentityRole))


def test_a_row_kind_is_never_a_column_kind() -> None:
    """A kind is a row OR a column. Both would probe twice and write the
    value to two places that drift."""
    assert not ROW_KIND_NAMES & set(queries.HANDLE_COLUMNS)


def test_every_row_kind_passes_the_tables_kind_check() -> None:
    """085's CHECK on kind, spelled here: a kind it refuses would fail every
    insert at runtime instead of here."""
    migration = "app/database/migrations/085_create_crm_customer_handle.sql"
    with open(migration) as sql:
        pattern = re.search(r"kind ~ '([^']+)'", sql.read())
    assert pattern is not None
    for kind in ROW_KIND_NAMES:
        assert re.match(pattern.group(1), kind), kind


def test_a_row_kind_is_bookkeeping_never_a_template_variable() -> None:
    """A send reads the id from the customer. A copy in the run's context
    would only be a way for it to reach a message or a log line."""
    assert ROW_KIND_NAMES <= set(_BOOKKEEPING_KEYS)


def test_a_row_kind_is_never_a_predicate_operand_or_a_readable_fact() -> None:
    assert ROW_KIND_NAMES <= predicates.HANDLE_LIKE
    assert ROW_KIND_NAMES <= facts.HANDLE_LIKE_ATTRIBUTES


def test_the_suppression_registry_still_takes_phone_and_email_only() -> None:
    """platform_identity is cross-merchant and its kinds are phone, email,
    device (migration 048). A merchant-scoped chat id does not belong there;
    ensure_identities() must keep skipping it."""
    pairs = _platform_pairs({"phone": PHONE, "xyne_user_id": RAVI})
    assert pairs == [("phone", PHONE)]


# --- the flat reader and the decode engine -------------------------------------


def test_the_flat_reader_takes_a_top_level_row_kind() -> None:
    extracted = flat.extract(
        {"customer_mobile_number": "9812345678", "xyne_user_id": RAVI}
    )
    assert extracted.handles == {"phone": "9812345678", "xyne_user_id": RAVI}


@pytest.mark.parametrize("not_an_id", [12345, 0, None, "", ["a"], {"id": "a"}, True])
def test_the_flat_reader_takes_strings_only(not_an_id: Any) -> None:
    extracted = flat.extract(
        {"customer_mobile_number": "9812345678", "xyne_user_id": not_an_id}
    )
    assert extracted.handles == {"phone": "9812345678"}


def test_a_payload_without_a_row_kind_reads_exactly_as_before() -> None:
    """The flat shape every buddy mirror sends: same handles, same facts."""
    extracted = flat.extract(
        {"customer_mobile_number": "9812345678", "customer_name": "Ravi"}
    )
    assert extracted.handles == {"phone": "9812345678"}
    assert extracted.facts == {"name": "Ravi"}


def _xyne_entry() -> CatalogEntry:
    return CatalogEntry(
        source="x",
        topic="t",
        label="T",
        group="X",
        layer="code",
        fields=[
            CatalogField(
                path="payload.context.userId",
                type="text",
                label="User",
                identity="xyne_user_id",
            )
        ],
    )


def test_a_declared_row_kind_is_normalized_by_the_engine() -> None:
    spec = engine.spec_for_entry(_xyne_entry(), {})
    extracted = engine.extract({"context": {"userId": f" {RAVI} "}}, spec)
    assert extracted.handles["xyne_user_id"] == RAVI


def test_a_declared_row_kind_that_is_a_placeholder_is_no_handle() -> None:
    spec = engine.spec_for_entry(_xyne_entry(), {})
    extracted = engine.extract({"context": {"userId": "env-api-user"}}, spec)
    assert "xyne_user_id" not in extracted.handles


@pytest.mark.parametrize("not_text", [True, False, 0, 1, 42, 3.5])
def test_a_declared_row_kind_that_is_not_text_is_no_handle(not_text: Any) -> None:
    """str(True) is 'True' and str(0) is '0': ids many people would share.
    The engine hands a row kind over as found, and its normalizer refuses
    anything that is not text — the same rule the flat reader keeps."""
    spec = engine.spec_for_entry(_xyne_entry(), {})
    extracted = engine.extract({"context": {"userId": not_text}}, spec)
    assert "xyne_user_id" not in extracted.handles


# --- resolve()'s normalization ---------------------------------------------------


def test_resolve_keeps_a_usable_row_kind_and_drops_a_placeholder() -> None:
    normalized = resolve_module._normalize(
        {"phone": "9812345678", "xyne_user_id": f" {RAVI} "}
    )
    assert normalized == {"phone": PHONE, "xyne_user_id": RAVI}
    assert resolve_module._normalize({"xyne_user_id": "null"}) == {}


def test_a_placeholder_alone_cannot_mint_a_customer() -> None:
    with pytest.raises(ValueError, match="usable handle"):
        asyncio.run(resolve_module.resolve(MERCHANT, {"xyne_user_id": "0"}))


# --- the query builders -----------------------------------------------------------


def _flat_sql(query: str) -> str:
    return " ".join(query.split())


def test_every_row_statement_is_scoped_to_the_merchant_first() -> None:
    builders = [
        queries.probe_row_handle_query(MERCHANT, "xyne_user_id", UNSCOPED, RAVI),
        queries.live_row_handles_query(MERCHANT, "c1"),
        queries.release_row_handle_query(MERCHANT, "c1", "xyne_user_id", UNSCOPED),
        queries.release_live_row_handles_query(MERCHANT, "c2"),
        queries.lock_live_customer_query(MERCHANT, "c1"),
    ]
    for query, values in builders:
        assert values[0] == MERCHANT
        assert "merchant_id = $1" in query


def test_the_probe_answers_only_an_active_customer() -> None:
    query, _ = queries.probe_row_handle_query(MERCHANT, "xyne_user_id", "", RAVI)
    assert "c.status = 'active'" in query
    assert "h.released_at IS NULL" in query
    assert "c.merchant_id = h.merchant_id" in query  # the join cannot cross tenants


def test_the_lock_proves_the_survivor_is_still_active() -> None:
    query, _ = queries.lock_live_customer_query(MERCHANT, "c1")
    assert "status = 'active'" in query and "FOR NO KEY UPDATE" in query


def test_a_release_stamps_the_wall_clock() -> None:
    """now() is the transaction's start: a row another transaction inserted
    and committed after this one began would get a release EARLIER than its
    creation, and the table's CHECK would refuse it."""
    for query, _ in (
        queries.release_row_handle_query(MERCHANT, "c1", "xyne_user_id", ""),
        queries.release_live_row_handles_query(MERCHANT, "c2"),
    ):
        assert "clock_timestamp()" in query and "now()" not in query


# --- a staple's inheritance, pure ---------------------------------------------------


def test_the_survivor_inherits_a_losers_id_only_into_an_empty_slot() -> None:
    released = [("xyne_user_id", "", RAVI), ("slack_user_id", "T024", "U012")]
    kept = {("xyne_user_id", ""): ASHA}
    assert resolve_module.plan_inherited_rows(released, kept) == [
        ("slack_user_id", "T024", "U012")
    ]


def test_two_losers_with_one_slot_leave_the_first_one_live() -> None:
    """Losers are stapled in probe order; the first fills the slot and the
    second's id stays released on its own record."""
    first = [("xyne_user_id", "", RAVI)]
    second = [("xyne_user_id", "", ASHA)]
    survivor: Dict[Tuple[str, str], str] = {}
    for kind, scope, value in resolve_module.plan_inherited_rows(first, survivor):
        survivor[(kind, scope)] = value
    assert resolve_module.plan_inherited_rows(second, survivor) == []
    assert survivor == {("xyne_user_id", ""): RAVI}


# --- 3. resolve()'s statements, on a fake connection --------------------------------


class _Recorder:
    """The asyncpg surface the identity accessor uses, recording every
    statement and answering from a script."""

    def __init__(self, answer: Callable[[str, Tuple[Any, ...]], Any]) -> None:
        self.statements: List[Tuple[str, Tuple[Any, ...]]] = []
        self._answer = answer

    async def _record(self, query: str, args: Tuple[Any, ...]) -> Any:
        self.statements.append((_flat_sql(query), args))
        return self._answer(_flat_sql(query), args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._record(query, args)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._record(query, args) or []

    async def execute(self, query: str, *args: Any) -> str:
        await self._record(query, args)
        return "OK"


NEW_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
OLD_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
YOUNG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _row(id_: uuid.UUID, first_seen: datetime, **handles: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": id_, "first_seen_at": first_seen}
    for column in queries.HANDLE_COLUMNS:
        row[column] = handles.get(column)
    return row


def _script(
    *,
    by_phone: Optional[Dict[str, Any]] = None,
    by_email: Optional[Dict[str, Any]] = None,
    by_xyne: Optional[Dict[str, Any]] = None,
    live: Optional[List[Dict[str, Any]]] = None,
    released: Optional[List[Dict[str, Any]]] = None,
    survivor_active: bool = True,
) -> Callable[[str, Tuple[Any, ...]], Any]:
    def answer(sql: str, args: Tuple[Any, ...]) -> Any:
        if sql.startswith("INSERT INTO crm_customer ("):
            return {"id": NEW_ID}
        if "FROM crm_customer_handle h JOIN" in sql:
            return by_xyne
        if sql.startswith("SELECT kind, scope, value"):
            return live or []
        if sql.endswith("RETURNING kind, scope, value"):
            return released or []
        if sql.endswith("FOR NO KEY UPDATE"):
            return {"id": args[1]} if survivor_active else None
        if "AND phone = $2" in sql:
            return by_phone
        if "AND email = $2" in sql:
            return by_email
        return None

    return answer


def _resolve(
    recorder: _Recorder, handles: Dict[str, str], evidence: str = "observed"
) -> Any:
    normalized = resolve_module._normalize(handles)
    return asyncio.run(
        resolve_module._resolve_in_txn(recorder, MERCHANT, normalized, evidence)  # type: ignore[arg-type]
    )


def _expected(*built: Tuple[str, List[Any]]) -> List[Tuple[str, Tuple[Any, ...]]]:
    return [(_flat_sql(query), tuple(values)) for query, values in built]


def _handle_writes(recorder: _Recorder) -> List[str]:
    return [
        sql
        for sql, _ in recorder.statements
        if sql.startswith(
            ("INSERT INTO crm_customer_handle", "UPDATE crm_customer_handle")
        )
    ]


def test_a_new_phone_only_customer_runs_exactly_the_old_statements() -> None:
    """Flipkart's traffic: one probe, one insert. Nothing touches 085."""
    recorder = _Recorder(_script())
    assert _resolve(recorder, {"phone": PHONE}) == NEW_ID
    assert recorder.statements == _expected(
        queries.probe_customer_query(MERCHANT, "phone", PHONE),
        queries.insert_customer_query(MERCHANT, {"phone": PHONE}),
    )


def test_a_known_phone_only_customer_runs_exactly_the_old_statements() -> None:
    recorder = _Recorder(_script(by_phone=_row(OLD_ID, T0, phone=PHONE)))
    assert _resolve(recorder, {"phone": PHONE}) == OLD_ID
    assert recorder.statements == _expected(
        queries.probe_customer_query(MERCHANT, "phone", PHONE),
        queries.apply_handles_query(str(OLD_ID), {}),
    )


def test_a_column_only_staple_adds_one_release_and_no_lock() -> None:
    """The one place an event with no row kind now runs more: after each
    merge, the loser's row handles are released (none, for a merchant with
    none). The survivor is NOT locked early — losers first, survivor last,
    the order resolve() has always locked in, so two staples meeting on the
    same records still queue instead of deadlocking."""
    recorder = _Recorder(
        _script(
            by_phone=_row(OLD_ID, T0, phone=PHONE),
            by_email=_row(YOUNG_ID, T1, email="ravi@example.com"),
        )
    )
    survivor = _resolve(recorder, {"phone": PHONE, "email": "ravi@example.com"})
    assert survivor == OLD_ID
    assert recorder.statements == _expected(
        queries.probe_customer_query(MERCHANT, "phone", PHONE),
        queries.probe_customer_query(MERCHANT, "email", "ravi@example.com"),
        queries.merge_customer_query(str(YOUNG_ID), str(OLD_ID)),
        queries.release_live_row_handles_query(MERCHANT, str(YOUNG_ID)),
        queries.apply_handles_query(str(OLD_ID), {"email": "ravi@example.com"}),
    )


def test_a_new_xyne_only_customer_gets_a_row_and_no_columns() -> None:
    """A customer this atom creates has no rows to read: probe, insert the
    customer, insert its row."""
    recorder = _Recorder(_script())
    assert _resolve(recorder, {"xyne_user_id": RAVI}) == NEW_ID
    assert recorder.statements == _expected(
        queries.probe_row_handle_query(MERCHANT, "xyne_user_id", UNSCOPED, RAVI),
        queries.insert_customer_query(MERCHANT, {}),
        queries.insert_row_handle_query(
            MERCHANT, str(NEW_ID), "xyne_user_id", UNSCOPED, RAVI
        ),
    )


def test_a_known_phone_gains_its_xyne_id_under_the_row_lock() -> None:
    """The order is the race guard: the customer's row is locked (and proven
    still active) BEFORE its rows are read, so a second resolve of the same
    person waits and then reads this one's write."""
    recorder = _Recorder(_script(by_phone=_row(OLD_ID, T0, phone=PHONE)))
    _resolve(recorder, {"phone": PHONE, "xyne_user_id": RAVI})
    assert recorder.statements == _expected(
        queries.probe_customer_query(MERCHANT, "phone", PHONE),
        queries.probe_row_handle_query(MERCHANT, "xyne_user_id", UNSCOPED, RAVI),
        queries.lock_live_customer_query(MERCHANT, str(OLD_ID)),
        queries.live_row_handles_query(MERCHANT, str(OLD_ID)),
        queries.apply_handles_query(str(OLD_ID), {}),
        queries.insert_row_handle_query(
            MERCHANT, str(OLD_ID), "xyne_user_id", UNSCOPED, RAVI
        ),
    )


def test_a_survivor_merged_away_since_the_probe_is_never_written_to() -> None:
    """A racing staple merged the customer away after our probe: the lock
    finds no active row and the atom stops before writing anything —
    resolve() re-probes (test_customer_handle_integration.py stages it)."""
    recorder = _Recorder(
        _script(by_phone=_row(OLD_ID, T0, phone=PHONE), survivor_active=False)
    )
    with pytest.raises(resolve_module._SurvivorMoved):
        _resolve(recorder, {"phone": PHONE, "xyne_user_id": RAVI})
    assert _handle_writes(recorder) == []
    assert not any(
        sql.startswith("UPDATE crm_customer ") for sql, _ in recorder.statements
    )


def test_the_same_xyne_id_again_writes_nothing() -> None:
    ravi = _row(OLD_ID, T0, phone=PHONE)
    recorder = _Recorder(
        _script(
            by_phone=ravi,
            by_xyne=ravi,
            live=[{"kind": "xyne_user_id", "scope": "", "value": RAVI}],
        )
    )
    _resolve(recorder, {"phone": PHONE, "xyne_user_id": RAVI})
    assert _handle_writes(recorder) == []


@pytest.mark.parametrize("evidence", ["observed", "declared"])
def test_a_different_xyne_id_releases_the_old_row_then_inserts(evidence: str) -> None:
    recorder = _Recorder(
        _script(
            by_phone=_row(OLD_ID, T0, phone=PHONE),
            live=[{"kind": "xyne_user_id", "scope": "", "value": ASHA}],
        )
    )
    _resolve(recorder, {"phone": PHONE, "xyne_user_id": RAVI}, evidence)
    assert recorder.statements[-2:] == _expected(
        queries.release_row_handle_query(
            MERCHANT, str(OLD_ID), "xyne_user_id", UNSCOPED
        ),
        queries.insert_row_handle_query(
            MERCHANT, str(OLD_ID), "xyne_user_id", UNSCOPED, RAVI
        ),
    )


def test_imported_data_never_displaces_a_live_xyne_id() -> None:
    recorder = _Recorder(
        _script(
            by_phone=_row(OLD_ID, T0, phone=PHONE),
            live=[{"kind": "xyne_user_id", "scope": "", "value": ASHA}],
        )
    )
    _resolve(recorder, {"phone": PHONE, "xyne_user_id": RAVI}, "imported")
    assert _handle_writes(recorder) == []


def test_a_xyne_id_held_by_another_record_staples_it_into_the_older() -> None:
    """Phone finds Ravi; the Xyne id finds a younger record of his (an
    earlier answer, before his phone was known). One person, two records:
    the younger is stapled, its row is released ON the younger (its audit
    copy), and only THEN is the survivor locked and given its own — so
    nothing lands in the survivor's slots before its lock. The letter's id
    is then already there; nothing more is written."""
    young = _row(YOUNG_ID, T1)
    recorder = _Recorder(
        _script(
            by_phone=_row(OLD_ID, T0, phone=PHONE),
            by_xyne=young,
            released=[{"kind": "xyne_user_id", "scope": "", "value": RAVI}],
        )
    )
    assert _resolve(recorder, {"phone": PHONE, "xyne_user_id": RAVI}) == OLD_ID
    assert recorder.statements[2:] == _expected(
        queries.merge_customer_query(str(YOUNG_ID), str(OLD_ID)),
        queries.release_live_row_handles_query(MERCHANT, str(YOUNG_ID)),
        queries.lock_live_customer_query(MERCHANT, str(OLD_ID)),
        queries.live_row_handles_query(MERCHANT, str(OLD_ID)),
        queries.insert_row_handle_query(
            MERCHANT, str(OLD_ID), "xyne_user_id", UNSCOPED, RAVI
        ),
        queries.apply_handles_query(str(OLD_ID), {}),
    )


def test_the_registry_order_is_the_probe_order() -> None:
    """Columns first in canon order, then the row kinds in registry order —
    deterministic, like the column probe."""
    recorder = _Recorder(_script())
    _resolve(recorder, {"xyne_user_id": RAVI, "phone": PHONE})
    first_two = [sql for sql, _ in recorder.statements[:2]]
    assert "AND phone = $2" in first_two[0]
    assert "FROM crm_customer_handle h JOIN" in first_two[1]
