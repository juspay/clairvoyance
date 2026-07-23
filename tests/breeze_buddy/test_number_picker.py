"""
Unit tests for telephony-number selection and RBAC scoping.

_get_available_number resolves in three tiers:
  1. template.outbound_number_id pin (explicit config always wins)
  2. a number provisioned for the calling merchant (merchant_id match)
  3. the legacy shared pool (reseller_id and merchant_id both NULL) —
     the backward-compatibility guarantee for numbers shared across many
     merchants/templates.

filter_numbers_by_rbac scopes reads: admins see the fleet; everyone else
sees owned numbers plus the ids their templates pin.
"""

import os
from types import SimpleNamespace
from typing import Any, Optional

import pytest

# Router import needs JWT env at module load time; these tests never mint or
# verify tokens (same pattern as test_block_codec_visibility.py).
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-not-used-by-these-tests")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

# Importing the dispatch package first initializes worker -> managers.calls in
# the supported order; importing managers.calls directly trips the circular
# import the dispatch/__init__ docstring warns about.
import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: E402,F401  (import order)
import app.ai.voice.agents.breeze_buddy.managers.calls as calls_mod  # noqa: E402
from app.api.routers.breeze_buddy.numbers.rbac import (  # noqa: E402
    filter_numbers_by_rbac,
    require_number_in_tenant_scope,
)
from app.schemas import (  # noqa: E402
    CallProvider,
    TelephonyNumber,
    TelephonyNumberStatus,
    UserInfo,
)


def make_number(
    num_id: str,
    merchant_id: Optional[str] = None,
    reseller_id: Optional[str] = None,
    status: TelephonyNumberStatus = TelephonyNumberStatus.AVAILABLE,
) -> TelephonyNumber:
    return TelephonyNumber(
        id=num_id,
        number=f"+9180000{num_id[-4:] if len(num_id) >= 4 else num_id}",
        provider=CallProvider.PLIVO,
        status=status,
        channels=0,
        maximum_channels=4,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
    )


def make_config(merchant_id: str = "acme.myshopify.com") -> Any:
    # Duck-typed stand-in for CallExecutionConfig; typed Any so pyrefly
    # accepts it at _get_available_number call sites.
    return SimpleNamespace(
        template="order-confirmation",
        reseller_id="breeze",
        merchant_id=merchant_id,
        calling_provider=CallProvider.PLIVO,
    )


@pytest.fixture
def pool(monkeypatch):
    """Patch the two accessors _get_available_number reads from."""
    state = SimpleNamespace(numbers=[], by_id={})

    async def fake_by_id(number_id):
        return state.by_id.get(number_id)

    async def fake_by_status_and_provider(status, provider):
        return [
            n for n in state.numbers if n.status == status and n.provider == provider
        ]

    monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", fake_by_id)
    monkeypatch.setattr(
        calls_mod,
        "get_telephony_number_based_on_status_and_provider",
        fake_by_status_and_provider,
    )
    return state


@pytest.mark.asyncio
async def test_template_pin_wins_over_owned_and_shared(pool):
    pinned = make_number("pin-0001")
    owned = make_number("own-0001", merchant_id="acme.myshopify.com")
    shared = make_number("shr-0001")
    pool.numbers = [owned, shared]
    pool.by_id = {pinned.id: pinned}

    template: Any = SimpleNamespace(outbound_number_id=pinned.id)
    got = await calls_mod._get_available_number(make_config(), template)
    assert got is pinned


@pytest.mark.asyncio
async def test_merchant_owned_preferred_over_shared_pool(pool):
    owned = make_number("own-0001", merchant_id="acme.myshopify.com")
    shared = make_number("shr-0001")
    pool.numbers = [shared, owned]  # shared listed first — preference, not order

    got = await calls_mod._get_available_number(make_config(), None)
    assert got is owned


@pytest.mark.asyncio
async def test_shared_pool_fallback_unchanged_when_nothing_owned(pool):
    """Numbers shared across many merchants keep working (backward compat)."""
    other = make_number("own-0002", merchant_id="someone-else.myshopify.com")
    umbrella = make_number("umb-0001", reseller_id="breeze")
    shared = make_number("shr-0001")
    pool.numbers = [other, umbrella, shared]

    got = await calls_mod._get_available_number(make_config(), None)
    assert got is shared


@pytest.mark.asyncio
async def test_another_merchants_number_is_never_picked(pool):
    pool.numbers = [make_number("own-0002", merchant_id="someone-else.myshopify.com")]

    got = await calls_mod._get_available_number(make_config(), None)
    assert got is None


def make_user(role: str, merchants=None, resellers=None) -> UserInfo:
    return UserInfo(
        id="u-1",
        username="probe",
        role=role,
        merchant_ids=merchants or [],
        reseller_ids=resellers or [],
    )


def test_rbac_admin_sees_fleet():
    numbers = [make_number("a"), make_number("b", merchant_id="m1")]
    assert filter_numbers_by_rbac(numbers, make_user("admin")) == numbers


def test_rbac_merchant_sees_owned_and_pinned_only():
    owned = make_number("own-0001", merchant_id="m1")
    pinned_shared = make_number("shr-0001")
    other_shared = make_number("shr-0002")
    other_owned = make_number("own-0002", merchant_id="m2")
    visible = filter_numbers_by_rbac(
        [owned, pinned_shared, other_shared, other_owned],
        make_user("merchant", merchants=["m1"]),
        pinned_number_ids=[pinned_shared.id],
    )
    assert visible == [owned, pinned_shared]


def test_rbac_reseller_sees_umbrella_owned():
    umbrella = make_number("umb-0001", reseller_id="r1")
    foreign = make_number("umb-0002", reseller_id="r2")
    visible = filter_numbers_by_rbac(
        [umbrella, foreign],
        make_user("reseller", resellers=["r1"], merchants=["*"]),
    )
    assert visible == [umbrella]


def test_rbac_wildcards_do_not_leak_the_fleet():
    """A reseller JWT carrying merchant_ids=['*'] must not see everything."""
    shared = make_number("shr-0001")
    visible = filter_numbers_by_rbac(
        [shared], make_user("reseller", resellers=["r1"], merchants=["*"])
    )
    assert visible == []


def test_rbac_merchant_umbrella_membership_grants_no_umbrella_numbers():
    """
    Merchant JWTs carry their umbrella in reseller_ids (workspace membership,
    set by the console's member flow) — that must NOT expose umbrella-owned
    numbers or sibling merchants' numbers, which share the same reseller_id.
    """
    own = make_number("own-0001", merchant_id="m1", reseller_id="r1")
    umbrella = make_number("umb-0001", reseller_id="r1")
    sibling = make_number("own-0002", merchant_id="m2", reseller_id="r1")
    visible = filter_numbers_by_rbac(
        [own, umbrella, sibling],
        make_user("merchant", merchants=["m1"], resellers=["r1"]),
    )
    assert visible == [own]


def test_rbac_reseller_sees_merchant_owned_numbers_under_umbrella():
    """Downward visibility: a reseller sees their merchants' numbers."""
    merchant_owned = make_number("own-0001", merchant_id="m1", reseller_id="r1")
    foreign_owned = make_number("own-0002", merchant_id="m9", reseller_id="r2")
    visible = filter_numbers_by_rbac(
        [merchant_owned, foreign_owned],
        make_user("reseller", resellers=["r1"], merchants=["*"]),
    )
    assert visible == [merchant_owned]


# ── template-pin tenant scoping (require_number_in_tenant_scope) ──


def test_pin_scope_allows_shared_own_merchant_and_own_umbrella():
    require_number_in_tenant_scope(
        make_number("shr-0001"), template_reseller_id="r1", template_merchant_id="m1"
    )
    require_number_in_tenant_scope(
        make_number("own-0001", merchant_id="m1"),
        template_reseller_id="r1",
        template_merchant_id="m1",
    )
    require_number_in_tenant_scope(
        make_number("umb-0001", reseller_id="r1"),
        template_reseller_id="r1",
        template_merchant_id="m1",
    )


def test_pin_scope_rejects_other_merchants_number():
    import pytest as _pytest
    from fastapi import HTTPException

    with _pytest.raises(HTTPException) as exc:
        require_number_in_tenant_scope(
            make_number("own-0002", merchant_id="m2"),
            template_reseller_id="r1",
            template_merchant_id="m1",
        )
    assert exc.value.status_code == 400


def test_pin_scope_rejects_other_umbrellas_number():
    import pytest as _pytest
    from fastapi import HTTPException

    with _pytest.raises(HTTPException) as exc:
        require_number_in_tenant_scope(
            make_number("umb-0002", reseller_id="r2"),
            template_reseller_id="r1",
            template_merchant_id="m1",
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_grandfathered_cross_merchant_pin_still_dials(pool, monkeypatch):
    """Existing cross-merchant pins keep working — logged at ERROR level so
    the ops alerting surfaces them, but the call is never blocked."""
    foreign = make_number("own-0002", merchant_id="someone-else.myshopify.com")
    pool.by_id = {foreign.id: foreign}

    errors: list = []
    real_logger = calls_mod.logger
    monkeypatch.setattr(
        calls_mod,
        "logger",
        SimpleNamespace(
            error=lambda msg, *a, **k: errors.append(str(msg)),
            info=real_logger.info,
            warning=real_logger.warning,
        ),
    )

    template: Any = SimpleNamespace(outbound_number_id=foreign.id)
    got = await calls_mod._get_available_number(make_config(), template)
    assert got is foreign
    assert any("grandfathered cross-merchant pin" in e for e in errors)
    # PII stays out of logs: the error carries the number id, not the number
    assert not any(foreign.number in e for e in errors)


# ── PATCH ownership reassignment (update_telephony_number_query) ──


def test_update_query_reassignment_rewrites_both_ownership_columns():
    """Umbrella-only reassignment must null the stale merchant_id."""
    from app.database.queries.breeze_buddy.telephony_number import (
        update_telephony_number_query,
    )

    text, values = update_telephony_number_query(
        "num-1", reseller_id="r1", merchant_id=None, set_ownership=True
    )
    assert '"reseller_id" = $2' in text
    assert '"merchant_id" = $3' in text
    assert values == ["num-1", "r1", None]


def test_update_query_without_ownership_flags_leaves_ownership_untouched():
    from app.database.queries.breeze_buddy.telephony_number import (
        update_telephony_number_query,
    )

    text, values = update_telephony_number_query("num-1", maximum_channels=8)
    assert '"reseller_id"' not in text
    assert '"merchant_id"' not in text
    assert values == ["num-1", 8]


# ── pinned-ids accessor: UUID → str coercion (regression) ──


@pytest.mark.asyncio
async def test_pinned_ids_are_strs_even_though_column_is_uuid(monkeypatch):
    """
    template.outbound_number_id is a UUID column (asyncpg decodes uuid.UUID),
    telephony_numbers.id is VARCHAR (str). Without str() coercion in the
    accessor the RBAC set lookups never match — every template pin was
    silently dropped from visibility. Regression for exactly that.
    """
    import uuid

    import app.database.accessor.breeze_buddy.telephony_number as accessor_mod

    pin = uuid.uuid4()

    async def fake_query(query_text, values):
        return [{"outbound_number_id": pin}, {"outbound_number_id": None}]

    monkeypatch.setattr(accessor_mod, "run_parameterized_query", fake_query)

    ids = await accessor_mod.get_template_pinned_number_ids(["m1"], [])
    assert ids == [str(pin)]
    assert all(isinstance(i, str) for i in ids)

    # and the id round-trips through the visibility rule against a str number id
    shared = make_number(str(pin))
    visible = filter_numbers_by_rbac(
        [shared], make_user("merchant", merchants=["m1"]), pinned_number_ids=ids
    )
    assert visible == [shared]


# ── analytics tenant scoping (get_telephony_numbers_analytics) ──


def make_analytics_record(num_id: str, merchant_id=None, reseller_id=None) -> dict:
    return {
        "id": num_id,
        "number": f"+9180000{num_id[-4:]}",
        "provider": "PLIVO",
        "status": "AVAILABLE",
        "channels": 0,
        "maximum_channels": 4,
        "merchant_id": merchant_id,
        "reseller_id": reseller_id,
        "total_calls": 3,
        "calls_picked": 2,
        "calls_no_answer": 1,
    }


@pytest.mark.asyncio
async def test_telephony_analytics_scoped_like_numbers_endpoint(monkeypatch):
    """Non-admins only see owned/pinned numbers; admins keep the fleet."""
    import app.api.routers.breeze_buddy.analytics.handlers as analytics_handlers

    records = [
        make_analytics_record("own-0001", merchant_id="m1", reseller_id="r1"),
        make_analytics_record("for-0001", merchant_id="m2", reseller_id="r1"),
        make_analytics_record("umb-0001", reseller_id="r1"),
        make_analytics_record("pin-0001"),
        make_analytics_record("shr-0001"),
    ]

    async def fake_db(filters):
        return records

    async def fake_pinned(merchant_ids, reseller_ids):
        return ["pin-0001"]

    monkeypatch.setattr(
        analytics_handlers, "get_telephony_numbers_analytics_from_db", fake_db
    )
    monkeypatch.setattr(
        analytics_handlers, "get_template_pinned_number_ids", fake_pinned
    )

    # merchant JWT carries the umbrella id (workspace membership) — still no
    # umbrella-owned or sibling-merchant rows in the results
    scoped = await analytics_handlers.get_telephony_numbers_analytics(
        {}, {}, make_user("merchant", merchants=["m1"], resellers=["r1"])
    )
    assert [r["id"] for r in scoped["results"]] == ["own-0001", "pin-0001"]

    umbrella_wide = await analytics_handlers.get_telephony_numbers_analytics(
        {}, {}, make_user("reseller", resellers=["r1"], merchants=["*"])
    )
    assert [r["id"] for r in umbrella_wide["results"]] == [
        "own-0001",
        "for-0001",
        "umb-0001",
        "pin-0001",
    ]

    fleet = await analytics_handlers.get_telephony_numbers_analytics(
        {}, {}, make_user("admin")
    )
    assert len(fleet["results"]) == len(records)


# ── workspace view-as narrowing (narrow_numbers_to_workspace) ──


def test_workspace_narrowing_matches_merchant_view():
    """Admin fleet narrowed to a workspace = that merchant's exact view."""
    from app.api.routers.breeze_buddy.numbers.rbac import narrow_numbers_to_workspace

    owned = make_number("own-0001", merchant_id="m1", reseller_id="r1")
    pinned_shared = make_number("shr-0001")
    sibling = make_number("own-0002", merchant_id="m2", reseller_id="r1")
    umbrella = make_number("umb-0001", reseller_id="r1")

    narrowed = narrow_numbers_to_workspace(
        [owned, pinned_shared, sibling, umbrella],
        "m1",
        pinned_number_ids=[pinned_shared.id],
    )
    assert narrowed == [owned, pinned_shared]


def test_workspace_narrowing_never_widens():
    """Passing a foreign merchant_id against an already-scoped list only shrinks it."""
    from app.api.routers.breeze_buddy.numbers.rbac import narrow_numbers_to_workspace

    own_scope = [make_number("own-0001", merchant_id="m1")]
    assert narrow_numbers_to_workspace(own_scope, "m2", pinned_number_ids=[]) == []


# ── cross-umbrella grandfathered pin alert ──


@pytest.mark.asyncio
async def test_grandfathered_cross_umbrella_pin_errors_and_dials(pool, monkeypatch):
    foreign_umbrella = make_number("umb-0002", reseller_id="another-umbrella")
    pool.by_id = {foreign_umbrella.id: foreign_umbrella}

    errors: list = []
    real_logger = calls_mod.logger
    monkeypatch.setattr(
        calls_mod,
        "logger",
        SimpleNamespace(
            error=lambda msg, *a, **k: errors.append(str(msg)),
            info=real_logger.info,
            warning=real_logger.warning,
        ),
    )

    template: Any = SimpleNamespace(outbound_number_id=foreign_umbrella.id)
    got = await calls_mod._get_available_number(make_config(), template)
    assert got is foreign_umbrella  # never blocks
    assert any("grandfathered cross-umbrella pin" in e for e in errors)


@pytest.mark.asyncio
async def test_own_umbrella_pin_stays_quiet(pool, monkeypatch):
    """Umbrella-owned number pinned by a template under the SAME umbrella: no alert."""
    own_umbrella = make_number("umb-0001", reseller_id="breeze")
    pool.by_id = {own_umbrella.id: own_umbrella}

    errors: list = []
    real_logger = calls_mod.logger
    monkeypatch.setattr(
        calls_mod,
        "logger",
        SimpleNamespace(
            error=lambda msg, *a, **k: errors.append(str(msg)),
            info=real_logger.info,
            warning=real_logger.warning,
        ),
    )

    template: Any = SimpleNamespace(outbound_number_id=own_umbrella.id)
    got = await calls_mod._get_available_number(make_config(), template)
    assert got is own_umbrella
    assert errors == []


@pytest.mark.asyncio
async def test_no_number_available_logs_error(pool, monkeypatch):
    """A call that cannot dial for lack of numbers must reach the alerting."""
    pool.numbers = []

    errors: list = []
    real_logger = calls_mod.logger
    monkeypatch.setattr(
        calls_mod,
        "logger",
        SimpleNamespace(
            error=lambda msg, *a, **k: errors.append(str(msg)),
            info=real_logger.info,
            warning=real_logger.warning,
        ),
    )

    got = await calls_mod._get_available_number(make_config(), None)
    assert got is None
    assert any("No outbound number found" in e for e in errors)


@pytest.mark.asyncio
async def test_admin_workspace_filter_narrows_analytics_rows(monkeypatch):
    """The console workspace switcher (filters.merchant_id) narrows an
    admin's analytics rows to that workspace's exact view."""
    import app.api.routers.breeze_buddy.analytics.handlers as analytics_handlers

    records = [
        make_analytics_record("own-0001", merchant_id="skybags", reseller_id="r1"),
        make_analytics_record("for-0001", merchant_id="m2", reseller_id="r1"),
        make_analytics_record("umb-0001", reseller_id="r1"),
        make_analytics_record("pin-0001"),
    ]

    async def fake_db(filters):
        return records

    async def fake_pinned(merchant_ids, reseller_ids):
        return ["pin-0001"] if "skybags" in merchant_ids else []

    monkeypatch.setattr(
        analytics_handlers, "get_telephony_numbers_analytics_from_db", fake_db
    )
    monkeypatch.setattr(
        analytics_handlers, "get_template_pinned_number_ids", fake_pinned
    )

    scoped = await analytics_handlers.get_telephony_numbers_analytics(
        {"merchant_id": "skybags"}, {}, make_user("admin")
    )
    assert [r["id"] for r in scoped["results"]] == ["own-0001", "pin-0001"]

    unscoped = await analytics_handlers.get_telephony_numbers_analytics(
        {}, {}, make_user("admin")
    )
    assert len(unscoped["results"]) == len(records)


def test_umbrella_narrowing_matches_reseller_view():
    from app.api.routers.breeze_buddy.numbers.rbac import narrow_numbers_to_umbrella

    umbrella = make_number("umb-0001", reseller_id="r1")
    under = make_number("own-0001", merchant_id="m1", reseller_id="r1")
    foreign = make_number("umb-0002", reseller_id="r2")
    pinned_shared = make_number("shr-0001")

    narrowed = narrow_numbers_to_umbrella(
        [umbrella, under, foreign, pinned_shared],
        "r1",
        pinned_number_ids=[pinned_shared.id],
    )
    assert narrowed == [umbrella, under, pinned_shared]


# ── view-as gate: cross-tenant asks are structurally denied ──


def test_may_view_as_matrix():
    from app.api.routers.breeze_buddy.numbers.rbac import may_view_as

    admin = make_user("admin")
    merchant = make_user("merchant", merchants=["m1"], resellers=["r1"])
    reseller = make_user("reseller", merchants=["m1", "m2"], resellers=["r1"])
    platform = make_user("reseller", merchants=["*"], resellers=["r1"])

    assert may_view_as(admin, workspace_merchant_id="anything")
    assert may_view_as(merchant, workspace_merchant_id="m1")
    assert not may_view_as(merchant, workspace_merchant_id="m2")
    assert not may_view_as(merchant, workspace_reseller_id="r2")
    assert may_view_as(reseller, workspace_merchant_id="m2")
    assert may_view_as(reseller, workspace_reseller_id="r1")
    assert not may_view_as(reseller, workspace_reseller_id="r2")
    assert may_view_as(platform, workspace_merchant_id="m9")  # '*' = unrestricted


@pytest.mark.asyncio
async def test_merchant_cannot_view_as_foreign_workspace_endpoint(monkeypatch):
    """Endpoint-level: a merchant passing another merchant's id gets [] —
    by explicit gate, not by filter ordering."""
    import app.api.routers.breeze_buddy.numbers as numbers_router

    foreign = make_number("own-0002", merchant_id="m2")
    own = make_number("own-0001", merchant_id="m1")

    async def fake_list(provider, status_filter, current_user):
        return [own, foreign]

    async def fake_pins(merchant_ids, reseller_ids):
        return []

    monkeypatch.setattr(numbers_router, "list_numbers_handler", fake_list)
    monkeypatch.setattr(numbers_router, "get_template_pinned_number_ids", fake_pins)

    got = await numbers_router.list_telephony_numbers(
        provider=None,
        status=None,
        merchant_id="m2",
        reseller_id=None,
        current_user=make_user("merchant", merchants=["m1"]),
    )
    assert got == []


@pytest.mark.asyncio
async def test_analytics_ignores_out_of_scope_view_as(monkeypatch):
    """Handler-level: an out-of-scope view-as value must not narrow to the
    foreign workspace — the caller keeps their own scoped view."""
    import app.api.routers.breeze_buddy.analytics.handlers as analytics_handlers

    records = [
        make_analytics_record("own-0001", merchant_id="m1", reseller_id="r1"),
        make_analytics_record("for-0001", merchant_id="m2", reseller_id="r1"),
    ]

    async def fake_db(filters):
        return records

    async def fake_pinned(merchant_ids, reseller_ids):
        return []

    monkeypatch.setattr(
        analytics_handlers, "get_telephony_numbers_analytics_from_db", fake_db
    )
    monkeypatch.setattr(
        analytics_handlers, "get_template_pinned_number_ids", fake_pinned
    )

    out = await analytics_handlers.get_telephony_numbers_analytics(
        {"merchant_id": "m2"}, {}, make_user("merchant", merchants=["m1"])
    )
    # gate rejects the foreign ask; result = caller's own scope, NOT m2's view
    assert [r["id"] for r in out["results"]] == ["own-0001"]
