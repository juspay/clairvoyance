"""Row-kind handles against a real Postgres: migration 085's table, its two
partial uniques and its guard, and resolve() driving them — including the
two races a fake connection cannot stage. Skipped unless a database with
every migration applied is offered:

    CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
        tests/crm/test_customer_handle_integration.py -v

Every test works under its own merchant id, so nothing here can collide
with another test's rows, and everything it wrote is removed after it.
"""

import asyncio
import uuid
from typing import Any, AsyncIterator, Dict, List

import asyncpg
import pytest

from app.crm.identity import resolve as resolve_module
from app.crm.identity.resolve import resolve
from app.crm.record.ingest import ingest_event
from app.crm.record.workers import run_pass
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed handle tests"
)

RAVI = "cmh2x9k1p0003qz8w5f4r7t2e"
ASHA = "cmh2xa04m0007qz8l1v6d9b3c"
XYNE = "xyne_user_id"


@pytest.fixture
async def db() -> AsyncIterator[Dict[str, Any]]:
    """The app's pool on this test's loop (asyncpg pools are loop-bound —
    see test_ingress_integration.py), a connection for asserting, and a
    fresh merchant id; every row under it is removed afterwards."""
    import app.database as database

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=6)
    previous, database.pool = database.pool, pool  # type: ignore[assignment]
    conn = await asyncpg.connect(DSN)
    merchant = f"hitest-{uuid.uuid4().hex[:10]}"
    try:
        yield {"conn": conn, "merchant": merchant}
    finally:
        # The merchant and the side merchants a test derives from it
        # ("<merchant>-b", "<merchant>-other").
        scope = merchant + "%"
        for statement in (
            "DELETE FROM crm_customer_handle WHERE merchant_id LIKE $1",
            "UPDATE crm_customer SET merged_into_id = NULL WHERE merchant_id LIKE $1",
            "DELETE FROM crm_customer WHERE merchant_id LIKE $1",
            "DELETE FROM crm_event_raw WHERE merchant_id LIKE $1",
        ):
            await conn.execute(statement, scope)
        await conn.close()
        database.pool = previous
        await pool.close()


def _phone() -> str:
    """A fresh number per call: platform_identity is cross-merchant, so a
    shared literal would leave registry rows other tests read."""
    return "+9199" + str(uuid.uuid4().int)[:8]


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.com"


async def _rows(conn: asyncpg.Connection, merchant: str) -> List[asyncpg.Record]:
    return await conn.fetch(
        """SELECT customer_id, kind, scope, value, released_at
           FROM crm_customer_handle WHERE merchant_id = $1
           ORDER BY created_at, value""",
        merchant,
    )


async def _live(conn: asyncpg.Connection, merchant: str) -> List[asyncpg.Record]:
    return [r for r in await _rows(conn, merchant) if r["released_at"] is None]


# --- resolve() on the table ---------------------------------------------------------


async def test_a_xyne_only_answer_makes_a_customer_with_one_live_row(db) -> None:
    conn, m = db["conn"], db["merchant"]
    customer = await resolve(m, {XYNE: RAVI})
    row = await conn.fetchrow(
        "SELECT phone, status FROM crm_customer WHERE id = $1", customer
    )
    assert row["phone"] is None and row["status"] == "active"
    live = await _live(conn, m)
    assert [(r["customer_id"], r["kind"], r["scope"], r["value"]) for r in live] == [
        (customer, XYNE, "", RAVI)
    ]
    assert await resolve(m, {XYNE: f"  {RAVI} "}) == customer  # found by it


async def test_the_phone_and_the_xyne_id_land_on_one_customer(db) -> None:
    conn, m = db["conn"], db["merchant"]
    phone = _phone()
    ravi = await resolve(m, {"phone": phone})
    assert await resolve(m, {"phone": phone, XYNE: RAVI}) == ravi
    assert await resolve(m, {XYNE: RAVI}) == ravi
    assert await resolve(m, {"phone": phone, XYNE: RAVI}) == ravi  # again: no-op
    assert len(await _rows(conn, m)) == 1
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM crm_customer WHERE merchant_id = $1", m
        )
        == 1
    )


async def test_a_corrected_id_releases_the_old_one_as_history(db) -> None:
    conn, m = db["conn"], db["merchant"]
    phone = _phone()
    ravi = await resolve(m, {"phone": phone, XYNE: ASHA})  # pragathi's typo
    await resolve(m, {"phone": phone, XYNE: RAVI})  # the correction
    rows = await _rows(conn, m)
    assert [(r["value"], r["released_at"] is None) for r in rows] == [
        (ASHA, False),
        (RAVI, True),
    ]
    # The released id is free: its real owner can take it.
    asha = await resolve(m, {XYNE: ASHA})
    assert asha != ravi
    assert len(await _live(conn, m)) == 2


async def test_imported_data_does_not_replace_a_live_id(db) -> None:
    conn, m = db["conn"], db["merchant"]
    phone = _phone()
    await resolve(m, {"phone": phone, XYNE: RAVI})
    await resolve(m, {"phone": phone, XYNE: ASHA}, evidence="imported")
    assert [r["value"] for r in await _live(conn, m)] == [RAVI]


async def test_an_earlier_xyne_only_record_is_stapled_into_the_phone_record(db) -> None:
    """Ravi answered a form before his phone was known (a Xyne-only
    record), and pragathi's event later carries both. The younger record
    is stapled: its row is released ON it (the loser keeps its copy, as
    audit + undo) and the survivor attaches its own, so the Xyne id finds
    the survivor."""
    conn, m = db["conn"], db["merchant"]
    phone = _phone()
    by_phone = await resolve(m, {"phone": phone})
    by_xyne = await resolve(m, {XYNE: RAVI})
    assert by_phone != by_xyne
    survivor = await resolve(m, {"phone": phone, XYNE: RAVI})
    assert survivor == by_phone  # the older record
    loser = await conn.fetchrow(
        "SELECT status, merged_into_id FROM crm_customer WHERE id = $1", by_xyne
    )
    assert loser["status"] == "merged_away" and loser["merged_into_id"] == by_phone
    assert [(r["customer_id"], r["value"]) for r in await _live(conn, m)] == [
        (by_phone, RAVI)
    ]
    kept_by_loser = [r for r in await _rows(conn, m) if r["customer_id"] == by_xyne]
    assert [(r["value"], r["released_at"] is not None) for r in kept_by_loser] == [
        (RAVI, True)
    ]
    assert await resolve(m, {XYNE: RAVI}) == by_phone


async def test_a_staple_keeps_the_survivors_id_and_releases_the_losers(db) -> None:
    """Both records already hold a Xyne id (two workspaces) and a phone +
    email event proves them one person: the survivor keeps its own id, the
    loser's becomes history — never two live ids in one slot."""
    conn, m = db["conn"], db["merchant"]
    phone, email = _phone(), f"{uuid.uuid4().hex[:8]}@example.com"
    older = await resolve(m, {"phone": phone, XYNE: RAVI})
    younger = await resolve(m, {"email": email, XYNE: ASHA})
    assert await resolve(m, {"phone": phone, "email": email}) == older
    rows = {r["value"]: r for r in await _rows(conn, m)}
    assert rows[RAVI]["customer_id"] == older and rows[RAVI]["released_at"] is None
    assert (
        rows[ASHA]["customer_id"] == younger and rows[ASHA]["released_at"] is not None
    )


async def test_the_same_xyne_id_under_two_merchants_is_two_customers(db) -> None:
    m = db["merchant"]
    assert await resolve(m, {XYNE: RAVI}) != await resolve(f"{m}-b", {XYNE: RAVI})


async def test_a_phone_only_customer_never_touches_the_table(db) -> None:
    conn, m = db["conn"], db["merchant"]
    await resolve(m, {"phone": _phone()})
    assert await _rows(conn, m) == []


# --- the races ------------------------------------------------------------------------


async def test_one_id_raced_onto_two_phones_converges_on_one_person(db) -> None:
    """Two letters at the same instant give the SAME Xyne id to two
    different phones. The value index refuses the second insert; its retry
    finds the id's owner and applies the ADR 0021 ladder there: the later
    phone replaces the earlier on that ONE customer (the old one kept in the
    handle history) — what two racing letters sharing one email and
    carrying two phones do today. Never two owners, never two customers.
    Twenty rounds."""
    conn, m = db["conn"], db["merchant"]
    for round_ in range(20):
        xyne = f"race{round_:02d}{uuid.uuid4().hex[:12]}"
        phones = [_phone(), _phone()]
        a, b = await asyncio.gather(
            resolve(m, {"phone": phones[0], XYNE: xyne}),
            resolve(m, {"phone": phones[1], XYNE: xyne}),
        )
        survivor = await resolve(m, {XYNE: xyne})
        assert survivor in (a, b)
        owners = await conn.fetch(
            """SELECT customer_id FROM crm_customer_handle
               WHERE merchant_id = $1 AND value = $2 AND released_at IS NULL""",
            m,
            xyne,
        )
        assert [r["customer_id"] for r in owners] == [survivor]
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM crm_customer WHERE merchant_id = $1"
                " AND phone = ANY($2::text[])",
                m,
                phones,
            )
            == 1
        )


async def test_two_ids_raced_onto_one_person_leave_exactly_one_live(db) -> None:
    """Two letters at the same instant give ONE person two different Xyne
    ids: one live id, the other released, never two live. (The slot index
    alone guarantees "never two"; that the second resolve waits on the
    lock and READS the first one's row is pinned by the statement order in
    test_customer_handles.py and by the staged races below.) Twenty
    rounds."""
    conn, m = db["conn"], db["merchant"]
    for _ in range(20):
        phone = _phone()
        person = await resolve(m, {"phone": phone})
        first, second = (f"r{uuid.uuid4().hex[:16]}" for _ in range(2))
        await asyncio.gather(
            resolve(m, {"phone": phone, XYNE: first}),
            resolve(m, {"phone": phone, XYNE: second}),
        )
        live = await conn.fetch(
            """SELECT value FROM crm_customer_handle
               WHERE merchant_id = $1 AND customer_id = $2 AND released_at IS NULL""",
            m,
            person,
        )
        assert len(live) == 1 and live[0]["value"] in (first, second)


async def test_a_resolve_racing_the_staple_of_its_customer_retries_instead_of_writing_onto_it(
    db,
) -> None:
    """A staples L into S and has not committed; B resolves L's email with a
    new Xyne id, probed L while it was still active, and waits on L's lock.
    When A commits, B's lock finds L merged away: the atom stops before
    writing and resolve() re-probes, so the id lands on S. Without the
    lock-first guard it landed on the merged-away L, where no probe can find
    it — and every later letter carrying it failed on the unique index."""
    conn, m = db["conn"], db["merchant"]
    phone, email, x3 = _phone(), _email(), f"wedge{uuid.uuid4().hex[:10]}"
    s = await resolve(m, {"phone": phone})
    await resolve(m, {"email": email})
    staple = await asyncpg.connect(DSN)
    try:
        tx = staple.transaction()
        await tx.start()
        await resolve_module._resolve_in_txn(
            staple, m, {"phone": phone, "email": email}, "observed"  # type: ignore[arg-type]
        )
        racer = asyncio.create_task(resolve(m, {"email": email, XYNE: x3}))
        await asyncio.sleep(0.3)
        assert not racer.done()  # waiting on L's lock
        await tx.commit()
        assert await asyncio.wait_for(racer, 10) == s
    finally:
        await staple.close()
    holder = await conn.fetchrow(
        """SELECT h.customer_id, c.status FROM crm_customer_handle h
           JOIN crm_customer c ON c.id = h.customer_id
           WHERE h.merchant_id = $1 AND h.value = $2 AND h.released_at IS NULL""",
        m,
        x3,
    )
    assert holder["customer_id"] == s and holder["status"] == "active"
    assert await resolve(m, {XYNE: x3}) == s


async def test_a_staple_waits_for_the_survivors_lock_instead_of_deadlocking(
    db,
) -> None:
    """B holds S's lock and is about to give S its own Xyne id; A staples L
    (holding X1) into S. Without the lock-first guard A put X1 into S's slot
    ahead of S's lock, B's insert then waited on A's slot while A waited on
    B's lock — a deadlock, raised straight to the caller. Now A waits for
    S's lock before touching any row; B finishes; A finds the slot filled
    and X1 stays released on L."""
    conn, m = db["conn"], db["merchant"]
    phone, email = _phone(), _email()
    x1, x2 = f"dla{uuid.uuid4().hex[:10]}", f"dlb{uuid.uuid4().hex[:10]}"
    s = await resolve(m, {"phone": phone})
    loser = await resolve(m, {"email": email, XYNE: x1})
    holder = await asyncpg.connect(DSN)
    try:
        tx = holder.transaction()
        await tx.start()
        # The lock a resolve of S holds (apply_handles' UPDATE takes the same):
        # it does not block A's foreign-key checks, only A's own S lock.
        await holder.execute(
            "SELECT id FROM crm_customer WHERE id = $1 FOR NO KEY UPDATE", s
        )
        staple = asyncio.create_task(resolve(m, {"phone": phone, "email": email}))
        await asyncio.sleep(0.3)
        assert not staple.done()  # waiting on S's lock, nothing written yet
        await holder.execute(
            """INSERT INTO crm_customer_handle (merchant_id, customer_id, kind, value)
               VALUES ($1, $2, $3, $4)""",
            m,
            s,
            XYNE,
            x2,
        )
        await tx.commit()
        assert await asyncio.wait_for(staple, 10) == s
    finally:
        await holder.close()
    rows = {r["value"]: r for r in await _rows(conn, m)}
    assert rows[x2]["customer_id"] == s and rows[x2]["released_at"] is None
    assert rows[x1]["customer_id"] == loser and rows[x1]["released_at"] is not None


async def _race_two_staples(
    db: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    xyne: Dict[str, str],
) -> Dict[str, Any]:
    """Three records of one person, oldest first: Z, S, L1 — each holding the
    Xyne id ``xyne`` gives it, if any. A's letter proves S and L1 one person
    (survivor S); B's proves all three (survivor Z, losers L1 then S). A is
    paused right after its FIRST lock, B starts, then A is released; each
    side commits as soon as it finishes. Returns the three ids."""
    m = db["merchant"]
    ref, email = f"z{uuid.uuid4().hex[:10]}", _email()
    phone, igsid = _phone(), f"ig{uuid.uuid4().hex[:10]}"
    z = await resolve(m, {"external_ref": ref, **_xyne(xyne, "z")})
    s = await resolve(m, {"email": email, **_xyne(xyne, "s")})
    l1 = await resolve(m, {"phone": phone, "igsid": igsid, **_xyne(xyne, "l1")})
    a_conn = await asyncpg.connect(DSN)
    first_lock, go = asyncio.Event(), asyncio.Event()

    def pause_after_first_lock(original: Any) -> Any:
        async def wrapped(conn_: Any, *args: Any) -> Any:
            result = await original(conn_, *args)
            if conn_ is a_conn and not first_lock.is_set():
                first_lock.set()
                await go.wait()
            return result

        return wrapped

    for name in ("merge_customer", "lock_live_customer"):
        monkeypatch.setattr(
            resolve_module.accessor,
            name,
            pause_after_first_lock(getattr(resolve_module.accessor, name)),
        )
    try:
        tx = a_conn.transaction()
        await tx.start()
        a = asyncio.create_task(
            resolve_module._resolve_in_txn(
                a_conn, m, {"email": email, "igsid": igsid}, "observed"  # type: ignore[arg-type]
            )
        )
        await asyncio.wait_for(first_lock.wait(), 10)
        b = asyncio.create_task(
            resolve(m, {"phone": phone, "email": email, "external_ref": ref})
        )
        await asyncio.sleep(0.3)
        assert not b.done()  # queued behind A
        go.set()
        assert await asyncio.wait_for(a, 10) == s
        await tx.commit()
        assert await asyncio.wait_for(b, 10) == z
    finally:
        await a_conn.close()
    statuses = {
        r["id"]: r["status"]
        for r in await db["conn"].fetch(
            "SELECT id, status FROM crm_customer WHERE merchant_id = $1", m
        )
    }
    assert statuses == {z: "active", s: "merged_away", l1: "merged_away"}
    return {"z": z, "s": s, "l1": l1}


def _xyne(xyne: Dict[str, str], record: str) -> Dict[str, str]:
    return {XYNE: xyne[record]} if record in xyne else {}


async def test_two_staples_with_different_survivors_queue_instead_of_deadlocking(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No record holds a Xyne id. Locking losers first and the survivor last
    — the order resolve() has always used — A's first lock is L1, so B
    queues behind it. Locking the survivor first, A would hold S while B
    held L1 and wanted S: a deadlock. Both letters land, as one person
    under Z, and the table is never touched."""
    await _race_two_staples(db, monkeypatch, {})
    assert await _rows(db["conn"], db["merchant"]) == []


async def test_two_staples_carry_a_losers_xyne_id_through_both_merges(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only L1 holds a Xyne id, X1. A merges L1 into S: L1's X1 is released
    on L1 and S gets a copy. B, queued behind A, then merges S into Z: S's
    copy is released on S and Z gets its own. One live X1, on Z; two
    released copies, one per merged record; and X1 finds Z."""
    conn, m = db["conn"], db["merchant"]
    x1 = f"x1{uuid.uuid4().hex[:10]}"
    ids = await _race_two_staples(db, monkeypatch, {"l1": x1})
    rows = [
        (r["customer_id"], r["value"], r["released_at"] is None)
        for r in await _rows(conn, m)
    ]
    assert sorted(rows, key=str) == sorted(
        [(ids["l1"], x1, False), (ids["s"], x1, False), (ids["z"], x1, True)], key=str
    )
    assert await resolve(m, {XYNE: x1}) == ids["z"]


async def test_two_staples_keep_the_survivors_own_xyne_id(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three hold a Xyne id: XZ, XS, X1. Each survivor keeps its own:
    A leaves X1 released on L1 (S already has XS); B leaves XS released on S
    (Z already has XZ). One live id — XZ on Z — and every other id still on
    its own record as history."""
    conn, m = db["conn"], db["merchant"]
    xz, xs, x1 = (f"{k}{uuid.uuid4().hex[:10]}" for k in ("xz", "xs", "x1"))
    ids = await _race_two_staples(db, monkeypatch, {"z": xz, "s": xs, "l1": x1})
    rows = {
        (r["customer_id"], r["value"]): r["released_at"] is None
        for r in await _rows(conn, m)
    }
    assert rows == {
        (ids["z"], xz): True,
        (ids["s"], xs): False,
        (ids["l1"], x1): False,
    }
    assert await resolve(m, {XYNE: xz}) == ids["z"]


async def test_a_staple_of_three_records_leaves_one_live_id_in_the_slot(db) -> None:
    """Three records of one person: S (phone, no Xyne id), L1 (email, X1),
    L2 (igsid, X2). One letter carries all three: both are stapled into S in
    probe order, so S inherits X1 and X2 stays released on L2. One live id;
    every id still on record."""
    conn, m = db["conn"], db["merchant"]
    phone, email, igsid = _phone(), _email(), f"ig{uuid.uuid4().hex[:10]}"
    x1, x2 = f"m1{uuid.uuid4().hex[:10]}", f"m2{uuid.uuid4().hex[:10]}"
    s = await resolve(m, {"phone": phone})
    l1 = await resolve(m, {"email": email, XYNE: x1})
    l2 = await resolve(m, {"igsid": igsid, XYNE: x2})
    assert await resolve(m, {"phone": phone, "email": email, "igsid": igsid}) == s
    assert [(r["customer_id"], r["value"]) for r in await _live(conn, m)] == [(s, x1)]
    released = {
        (r["customer_id"], r["value"])
        for r in await _rows(conn, m)
        if r["released_at"] is not None
    }
    assert released == {(l1, x1), (l2, x2)}


async def test_a_staple_on_imported_data_still_carries_the_losers_id(db) -> None:
    """A staple is not an overwrite: an empty slot is filled whatever the
    evidence (ADR 0021: attach when free), so bulk data that proves two
    records one person keeps the loser's Xyne id reachable."""
    conn, m = db["conn"], db["merchant"]
    phone, email = _phone(), _email()
    s = await resolve(m, {"phone": phone})
    await resolve(m, {"email": email, XYNE: RAVI})
    assert await resolve(m, {"phone": phone, "email": email}, evidence="imported") == s
    assert [(r["customer_id"], r["value"]) for r in await _live(conn, m)] == [(s, RAVI)]


# --- the table's own guarantees -------------------------------------------------------


async def test_the_slot_index_refuses_a_second_live_id_whatever_the_code_does(
    db,
) -> None:
    conn, m = db["conn"], db["merchant"]
    customer = await resolve(m, {XYNE: RAVI})
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            """INSERT INTO crm_customer_handle (merchant_id, customer_id, kind, value)
               VALUES ($1, $2, $3, $4)""",
            m,
            customer,
            XYNE,
            ASHA,
        )


async def test_a_handle_cannot_point_at_another_merchants_customer(db) -> None:
    conn, m = db["conn"], db["merchant"]
    customer = await resolve(m, {XYNE: RAVI})
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            """INSERT INTO crm_customer_handle (merchant_id, customer_id, kind, value)
               VALUES ($1, $2, $3, $4)""",
            f"{m}-other",
            customer,
            XYNE,
            ASHA,
        )


@pytest.mark.parametrize(
    "kind,value",
    [
        ("Xyne", "a"),
        ("1xyne", "a"),
        ("xyne id", "a"),
        (XYNE, ""),
        (XYNE, " a"),
        (XYNE, "a" * 257),
    ],
)
async def test_the_format_checks_refuse_a_malformed_row(
    db, kind: str, value: str
) -> None:
    conn, m = db["conn"], db["merchant"]
    customer = await resolve(m, {"phone": _phone()})
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """INSERT INTO crm_customer_handle (merchant_id, customer_id, kind, value)
               VALUES ($1, $2, $3, $4)""",
            m,
            customer,
            kind,
            value,
        )


@pytest.mark.parametrize(
    "assignment",
    [
        "value = 'other'",
        "customer_id = gen_random_uuid()",
        "kind = 'other_kind'",
        "scope = 'x'",
        "created_at = now() - interval '1 day'",
    ],
)
async def test_what_a_row_says_never_changes(db, assignment: str) -> None:
    conn, m = db["conn"], db["merchant"]
    await resolve(m, {XYNE: RAVI})
    with pytest.raises(asyncpg.RaiseError, match="never change"):
        await conn.execute(
            f"UPDATE crm_customer_handle SET {assignment} WHERE merchant_id = $1", m
        )


async def test_a_released_row_is_history_and_cannot_change(db) -> None:
    conn, m = db["conn"], db["merchant"]
    phone = _phone()
    await resolve(m, {"phone": phone, XYNE: ASHA})
    await resolve(m, {"phone": phone, XYNE: RAVI})
    for assignment in ("released_at = NULL", "released_at = clock_timestamp()"):
        with pytest.raises(asyncpg.RaiseError, match="history"):
            await conn.execute(
                f"""UPDATE crm_customer_handle SET {assignment}
                    WHERE merchant_id = $1 AND released_at IS NOT NULL""",
                m,
            )


# --- the event path, end to end -----------------------------------------------------


async def test_an_event_with_a_xyne_id_reaches_the_table_through_the_worker(db) -> None:
    """The way pragathi will send it: the flat shape on /ingest/events,
    the event worker's pass, resolve(). A second, Xyne-only event (the way a
    form answer will arrive) is stamped with the same customer."""
    conn, m = db["conn"], db["merchant"]
    phone = _phone()
    first = await ingest_event(
        merchant_id=m,
        source="lead-api",
        topic="interview.requested",
        external_id=f"req-{uuid.uuid4().hex[:8]}",
        payload={"customer_mobile_number": phone, XYNE: RAVI, "role": "Frontend"},
    )
    second = await ingest_event(
        merchant_id=m,
        source="lead-api",
        topic="interview.answered",
        external_id=f"ans-{uuid.uuid4().hex[:8]}",
        payload={XYNE: RAVI},
    )
    for _ in range(5):
        if not await run_pass(50):
            break
    stamped = await conn.fetch(
        """SELECT id, customer_id, quarantine_reason FROM crm_event_raw
           WHERE merchant_id = $1 ORDER BY received_at""",
        m,
    )
    assert [r["quarantine_reason"] for r in stamped] == [None, None]
    by_id = {str(r["id"]): r["customer_id"] for r in stamped}
    assert by_id[str(first)] is not None and by_id[str(first)] == by_id[str(second)]
    customer = await conn.fetchrow(
        "SELECT phone FROM crm_customer WHERE id = $1", by_id[str(first)]
    )
    assert customer["phone"] == phone
    assert [(r["customer_id"], r["value"]) for r in await _live(conn, m)] == [
        (by_id[str(first)], RAVI)
    ]


async def test_an_event_with_only_a_placeholder_id_is_quarantined_not_stored(
    db,
) -> None:
    conn, m = db["conn"], db["merchant"]
    await ingest_event(
        merchant_id=m,
        source="lead-api",
        topic="interview.answered",
        external_id=f"ans-{uuid.uuid4().hex[:8]}",
        payload={XYNE: "env-api-user"},
    )
    for _ in range(5):
        if not await run_pass(50):
            break
    reason = await conn.fetchval(
        "SELECT quarantine_reason FROM crm_event_raw WHERE merchant_id = $1", m
    )
    assert reason is not None
    assert await _rows(conn, m) == []
