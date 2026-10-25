"""token_registry: the registry check + revoke (2/4), enforced (4/4).

What is proven here:
- ``authenticate`` leaves short-lived tokens alone (no registry call), and
  looks every long-lived one up by hash: a live row passes, a revoked row is
  a 401, a revocation dated in the future is not yet one, and an unreadable
  registry is a 503 — never a pass;
- enforced (4/4): a long-lived token the registry doesn't hold is a 401,
  with or without a jti, and that answer isn't cached;
- a pod reads a token's row at most once per 30 s, and ``forget`` drops
  that read;
- last_seen_* is written when stale, skipped when fresh, and a failed write
  neither refuses the request nor retries on the next one; the X-Forwarded-
  For chain and the route template are what get written;
- every verifier door now goes through ``authenticate`` (the dependency,
  the websocket, feature flags, CRM s2s, both webhooks);
- the admin routes: non-admins get 403, unknown id 404, already revoked 409,
  a revoke drops this pod's cached read, a blank reason is refused.

Hermetic: the accessor functions (or the accessor's query runner) are
replaced by in-memory stand-ins. The SQL itself is proven on Postgres in
the PR's end-to-end run.
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routers.breeze_buddy.auth import handlers as auth_handlers
from app.api.routers.breeze_buddy.webhooks.breeze import services as breeze_services
from app.api.routers.breeze_buddy.webhooks.woocommerce import (
    services as woo_services,
)
from app.api.routers.feature_flags import rbac as feature_flag_rbac
from app.api.security.breeze_buddy import rbac_token
from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.crm import auth as crm_auth
from app.database.queries.breeze_buddy.token_registry import (
    list_tokens_query,
    register_token_query,
    revoke_token_query,
)
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import MerchantResponse
from app.schemas.breeze_buddy.token_registry import (
    RevokeTokenRequest,
    TokenRegistryEntry,
)
from app.schemas.breeze_buddy.users import UserTokenState

_NOW = datetime.now(timezone.utc)


def _token(
    *,
    jti: Optional[str] = None,
    lifetime: timedelta = timedelta(days=30),
    user_id: str = "admin-1",
    merchant_ids: Optional[List[str]] = None,
) -> str:
    """A real RBAC token signed with the app's key."""
    return rbac_token_manager.create_access_token_with_rbac(
        user_id=user_id,
        username="svc",
        role=UserRole.RESELLER,
        reseller_ids=["r1"],
        merchant_ids=merchant_ids if merchant_ids is not None else ["*"],
        expires_delta=lifetime,
        jti=jti,
    )


def _entry(**overrides: Any) -> TokenRegistryEntry:
    """A registry row with test defaults."""
    fields: Dict[str, Any] = dict(
        id="11111111-1111-1111-1111-111111111111",
        token_hash="a" * 64,
        kind="admin",
        source="minted",
        subject="admin-1",
        username="svc",
        role="reseller",
        reseller_ids=["r1"],
        merchant_ids=["*"],
        issued_at=_NOW - timedelta(days=1),
        expires_at=_NOW + timedelta(days=29),
        created_at=_NOW - timedelta(days=1),
        last_seen_at=_NOW,
    )
    fields.update(overrides)
    return TokenRegistryEntry(**fields)


class _Registry:
    """Stands in for the two accessor calls ``authenticate`` makes."""

    def __init__(self, entry: Optional[TokenRegistryEntry] = None) -> None:
        self.entry = entry or _entry()
        self.reads: List[str] = []
        self.seen: List[tuple] = []
        self.fail_read = False
        self.fail_seen = False
        self.unknown = False

    async def find(self, token_hash: str) -> Optional[TokenRegistryEntry]:
        """get_token_by_hash: the row, or None when ``unknown``."""
        if self.fail_read:
            raise RuntimeError("db down")
        self.reads.append(token_hash)
        return None if self.unknown else self.entry

    async def mark_seen(self, token_id, seen_from, route) -> Optional[datetime]:
        """mark_token_seen."""
        if self.fail_seen:
            raise RuntimeError("db down")
        self.seen.append((token_id, seen_from, route))
        return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _empty_cache():
    """Each test starts with no cached registry or subject reads."""
    rbac_token._registry_cache.clear()
    rbac_token._subject_cache.clear()
    yield
    rbac_token._registry_cache.clear()
    rbac_token._subject_cache.clear()


@pytest.fixture
def registry(monkeypatch) -> _Registry:
    """An in-memory registry behind ``authenticate``."""
    fake = _Registry()
    monkeypatch.setattr(rbac_token, "get_token_by_hash", fake.find)
    monkeypatch.setattr(rbac_token, "mark_token_seen", fake.mark_seen)

    async def active_user(user_id):
        """Every token's user is active, with no login cut-off (3/4)."""
        return UserTokenState(is_active=True)

    async def active_merchant(merchant_id):
        """Every token's merchant is active (3/4)."""
        return MerchantResponse(merchant_id=merchant_id, is_active=True)

    monkeypatch.setattr(rbac_token, "get_user_token_state", active_user)
    monkeypatch.setattr(
        rbac_token, "get_merchant_by_merchant_identifier", active_merchant
    )
    return fake


def _connection(
    xff: Optional[str] = None, peer: str = "10.0.0.9", route: str = "/leads"
) -> Any:
    """A request-shaped object: headers, client, scope route, url."""
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=peer),
        scope={"route": SimpleNamespace(path=route)},
        url=SimpleNamespace(path="/raw/path/123"),
    )


# --- which tokens the registry sees -------------------------------------


@pytest.mark.parametrize(
    "lifetime", [timedelta(minutes=30), timedelta(hours=1)], ids=["30m", "1h"]
)
async def test_a_short_lived_token_without_jti_never_reaches_the_registry(
    registry, lifetime
) -> None:
    """Login/launch/signup-length tokens cost no DB read and leave no row."""
    user = await rbac_token_manager.authenticate(_token(lifetime=lifetime))
    assert user.username == "svc"
    assert registry.reads == []


async def test_a_short_lived_token_with_a_jti_is_still_checked(registry) -> None:
    """A jti always means a registered token, whatever its lifetime."""
    await rbac_token_manager.authenticate(
        _token(jti=registry.entry.id, lifetime=timedelta(minutes=30))
    )
    assert len(registry.reads) == 1


async def test_a_long_lived_token_without_jti_is_checked(registry) -> None:
    """Tokens minted before the registry are looked up too (by hash)."""
    token = _token(lifetime=timedelta(days=2))
    await rbac_token_manager.authenticate(token)
    assert registry.reads == [rbac_token.token_hash_of(token)]


async def test_a_token_without_iat_is_treated_as_long_lived(registry) -> None:
    """No iat → its lifetime can't be told → it goes to the registry."""
    token = pyjwt.encode(
        {
            "sub": "admin-1",
            "username": "svc",
            "role": "reseller",
            "reseller_ids": ["r1"],
            "merchant_ids": ["*"],
            "exp": int((_NOW + timedelta(minutes=10)).timestamp()),
        },
        rbac_token_manager.jwt_manager.secret_key,
        algorithm=rbac_token_manager.jwt_manager.algorithm,
    )
    await rbac_token_manager.authenticate(token)
    assert len(registry.reads) == 1


# --- the decision ---------------------------------------------------------


async def test_a_live_row_passes(registry) -> None:
    """Not revoked → the verifier's own answer comes back."""
    user = await rbac_token_manager.authenticate(_token())
    assert user.reseller_ids == ["r1"]


async def test_a_revoked_token_is_401(registry) -> None:
    """Revoked a second ago → refused like any bad token."""
    registry.entry = _entry(
        revoked_at=_NOW - timedelta(seconds=1),
        revoked_by="admin-1",
        revoke_reason="leaked",
    )
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(_token())
    assert caught.value.status_code == 401


async def test_a_revocation_dated_later_is_not_yet_one(registry) -> None:
    """Revoked is ``revoked_at <= now()``: a future stamp still passes."""
    registry.entry = _entry(
        revoked_at=_NOW + timedelta(hours=1),
        revoked_by="admin-1",
        revoke_reason="rotating",
    )
    await rbac_token_manager.authenticate(_token())


async def test_an_unreadable_registry_is_503_never_a_pass(registry) -> None:
    """Fail closed — but as 503, so a caller doesn't discard a good token."""
    registry.fail_read = True
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(_token())
    assert caught.value.status_code == 503


async def test_a_bad_token_is_refused_before_the_registry(registry) -> None:
    """The signature check still comes first."""
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(_token() + "x")
    assert caught.value.status_code == 401
    assert registry.reads == []


# --- an unknown token (enforced, 4/4) -----------------------------------------


@pytest.mark.parametrize(
    "jti", [None, "22222222-2222-2222-2222-222222222222"], ids=["no-jti", "jti"]
)
async def test_an_unregistered_long_lived_token_is_401(registry, jti) -> None:
    """A long-lived token the registry doesn't hold is refused, and nothing
    is stamped for it."""
    registry.unknown = True
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(_token(jti=jti))
    assert caught.value.status_code == 401
    assert registry.seen == []


async def test_an_unknown_token_is_not_cached(registry) -> None:
    """Not found isn't remembered: a token minted (or rotated) on another
    pod a moment ago is found on its next use."""
    registry.unknown = True
    token = _token()
    with pytest.raises(HTTPException):
        await rbac_token_manager.authenticate(token)
    registry.unknown = False
    await rbac_token_manager.authenticate(token)
    assert len(registry.reads) == 2


# --- the 30-second read -----------------------------------------------------


async def test_a_pod_reads_a_token_once_per_cache_window(registry, monkeypatch) -> None:
    """Two requests in the window → one read; after it → a fresh read."""
    token = _token()
    await rbac_token_manager.authenticate(token)
    await rbac_token_manager.authenticate(token)
    assert len(registry.reads) == 1

    later = time.monotonic() + rbac_token._REGISTRY_CACHE_SECONDS + 1
    monkeypatch.setattr(rbac_token.time, "monotonic", lambda: later)
    await rbac_token_manager.authenticate(token)
    assert len(registry.reads) == 2


async def test_a_revoke_lands_when_the_window_ends(registry, monkeypatch) -> None:
    """The longest another pod keeps accepting a revoked token is the window."""
    token = _token()
    await rbac_token_manager.authenticate(token)
    registry.entry = _entry(
        revoked_at=_NOW, revoked_by="admin-1", revoke_reason="leaked"
    )
    await rbac_token_manager.authenticate(token)  # still the cached read

    later = time.monotonic() + rbac_token._REGISTRY_CACHE_SECONDS + 1
    monkeypatch.setattr(rbac_token.time, "monotonic", lambda: later)
    with pytest.raises(HTTPException):
        await rbac_token_manager.authenticate(token)


async def test_forget_drops_this_pods_read(registry) -> None:
    """After a revoke on this pod, the next request reads again."""
    token = _token()
    await rbac_token_manager.authenticate(token)
    rbac_token_manager.forget(rbac_token.token_hash_of(token))
    await rbac_token_manager.authenticate(token)
    assert len(registry.reads) == 2


# --- last seen ----------------------------------------------------------------


async def test_a_stale_last_seen_is_stamped_with_chain_and_route(registry) -> None:
    """Never seen → one stamp, the XFF chain as received, the route template."""
    registry.entry = _entry(last_seen_at=None)
    await rbac_token_manager.authenticate(
        _token(), _connection(xff="203.0.113.7, 130.211.0.1", route="/leads/{id}")
    )
    assert registry.seen == [
        (registry.entry.id, "203.0.113.7, 130.211.0.1", "/leads/{id}")
    ]


async def test_without_a_chain_the_direct_peer_is_stamped(registry) -> None:
    """No X-Forwarded-For → the peer address."""
    registry.entry = _entry(last_seen_at=None)
    await rbac_token_manager.authenticate(_token(), _connection(peer="10.1.2.3"))
    assert registry.seen[0][1] == "10.1.2.3"


async def test_seen_fields_are_capped(registry) -> None:
    """A huge header is cut to 200 characters."""
    registry.entry = _entry(last_seen_at=None)
    await rbac_token_manager.authenticate(_token(), _connection(xff="9" * 5000))
    assert len(registry.seen[0][1]) == 200


async def test_a_fresh_last_seen_is_not_rewritten(registry) -> None:
    """Seen a minute ago → no write."""
    registry.entry = _entry(last_seen_at=_NOW - timedelta(minutes=1))
    await rbac_token_manager.authenticate(_token(), _connection())
    assert registry.seen == []


async def test_a_last_seen_older_than_the_interval_is_rewritten(registry) -> None:
    """Seen six minutes ago → one write."""
    registry.entry = _entry(last_seen_at=_NOW - timedelta(minutes=6))
    await rbac_token_manager.authenticate(_token(), _connection())
    assert len(registry.seen) == 1


async def test_a_failed_seen_write_neither_refuses_nor_retries(registry) -> None:
    """Bookkeeping: the request passes, and the next one doesn't try again."""
    registry.entry = _entry(last_seen_at=None)
    registry.fail_seen = True
    token = _token()
    await rbac_token_manager.authenticate(token, _connection())
    registry.fail_seen = False
    await rbac_token_manager.authenticate(token, _connection())
    assert registry.seen == []


# --- the SQL builders -----------------------------------------------------------


def test_the_mint_insert_never_tolerates_a_conflict() -> None:
    """A duplicate hash or id must fail the mint, never pass silently."""
    query, _ = register_token_query(
        token_id="x",
        token_hash="a" * 64,
        kind="admin",
        source="minted",
        subject="s",
        username="u",
        role="admin",
        reseller_ids=["*"],
        merchant_ids=["*"],
        created_by=None,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
    )
    assert "ON CONFLICT" not in query


def test_revoke_clears_only_a_stored_token_with_the_same_hash() -> None:
    """The merchant row is matched by the token's own merchants and hash."""
    query, values = revoke_token_query("x", "admin-1", "leaked")
    assert "revoked_at IS NULL" in query
    assert "m.merchant_id = ANY(r.merchant_ids)" in query
    assert (
        "encode(sha256(convert_to(m.s2s_token, 'UTF8')), 'hex') = r.token_hash" in query
    )
    assert values == ["x", "admin-1", "leaked"]


def test_the_list_hides_revoked_rows_unless_asked() -> None:
    """include_revoked is the third parameter and gates the revoked filter."""
    query, values = list_tokens_query(
        kind="merchant",
        merchant_id="shop-1",
        include_revoked=False,
        limit=50,
        offset=0,
    )
    assert "($3 OR revoked_at IS NULL OR revoked_at > now())" in query
    assert values == ["merchant", "shop-1", False, 50, 0]


# --- every verifier door goes through authenticate ----------------------------


class _Spy:
    """Records what authenticate was called with."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def __call__(self, token, connection=None):
        """Accept everything as an admin."""
        self.calls.append((token, connection))
        return UserInfo(
            id="u",
            username="u",
            role=UserRole.ADMIN,
            reseller_ids=["*"],
            merchant_ids=["*"],
        )


@pytest.fixture
def spy(monkeypatch) -> _Spy:
    """authenticate replaced by a recorder."""
    recorder = _Spy()
    monkeypatch.setattr(rbac_token_manager, "authenticate", recorder)
    return recorder


async def test_the_main_dependency_hands_its_request_to_authenticate(spy) -> None:
    """get_current_user_with_rbac → authenticate(token, request)."""
    request = _connection()
    await rbac_token.get_current_user_with_rbac(
        request, cast(Any, SimpleNamespace(credentials="tok"))
    )
    assert spy.calls == [("tok", request)]


async def test_the_feature_flag_dependency_goes_through_authenticate(spy) -> None:
    """feature_flags/rbac.py no longer verifies on its own."""
    request = _connection()
    await feature_flag_rbac.get_current_user_with_role(
        request, cast(Any, SimpleNamespace(credentials="tok"))
    )
    assert spy.calls == [("tok", request)]


async def test_the_crm_s2s_caller_goes_through_authenticate(spy, monkeypatch) -> None:
    """crm/auth.py verify_s2s_caller: a wildcard caller, one authenticate."""

    async def exists(merchant_id):
        """The merchant exists."""
        return True

    monkeypatch.setattr(crm_auth, "check_merchant_identifier_exists", exists)
    request: Any = SimpleNamespace(headers={"x-s2s-token": "tok"})
    assert await crm_auth.verify_s2s_caller("shop-1", request) == "shop-1"
    assert spy.calls == [("tok", request)]


@pytest.mark.parametrize("door", ["woocommerce", "breeze"])
async def test_both_webhook_doors_check_the_stored_token(
    spy, monkeypatch, door
) -> None:
    """The stored per-merchant token goes through authenticate, so a revoked
    one stops its webhooks even before the row is cleared."""
    module = woo_services if door == "woocommerce" else breeze_services
    handle = (
        woo_services.handle_woocommerce
        if door == "woocommerce"
        else breeze_services.handle_breeze
    )

    async def stored(merchant_id):
        """The merchant's stored token."""
        return "stored-token"

    monkeypatch.setattr(module, "get_merchant_s2s_token", stored)
    if door == "woocommerce":
        monkeypatch.setattr(
            woo_services, "calculate_hmac_sha256", lambda body, key: "sig"
        )
        headers = {"x-wc-webhook-signature": "sig"}
    else:
        headers = {"x-breeze-token": "stored-token"}

    async def body():
        """An empty delivery."""
        return b"{}"

    request: Any = SimpleNamespace(
        query_params={}, headers=headers, body=body, url=SimpleNamespace(path="/")
    )
    with pytest.raises(HTTPException):  # no template_id → 400, after auth
        await handle("orders/created", "shop-1", request)
    assert spy.calls == [("stored-token", request)]


# --- the admin routes -------------------------------------------------------------

_ADMIN = UserInfo(
    id="admin-1",
    username="admin",
    role=UserRole.ADMIN,
    reseller_ids=["*"],
    merchant_ids=["*"],
)
_RESELLER = UserInfo(
    id="r-1",
    username="reseller",
    role=UserRole.RESELLER,
    reseller_ids=["r1"],
    merchant_ids=["*"],
)


async def test_non_admins_can_neither_list_nor_revoke() -> None:
    """A reseller (including a narrowed S2S token) gets 403 on both."""
    with pytest.raises(HTTPException) as listing:
        await auth_handlers.list_tokens_handler(_RESELLER, None, None, False, 10, 0)
    with pytest.raises(HTTPException) as revoking:
        await auth_handlers.revoke_token_handler(
            "x", RevokeTokenRequest(reason="r"), _RESELLER
        )
    assert listing.value.status_code == revoking.value.status_code == 403


def _revoke_answers(
    monkeypatch, revoked: Optional[tuple], existing: Optional[TokenRegistryEntry]
) -> None:
    """The accessor answers revoke_token, then get_token_by_id."""

    async def revoke(token_id, revoked_by, reason):
        """revoke_token."""
        return revoked

    async def by_id(token_id):
        """get_token_by_id."""
        return existing

    monkeypatch.setattr(auth_handlers.token_accessors, "revoke_token", revoke)
    monkeypatch.setattr(auth_handlers.token_accessors, "get_token_by_id", by_id)


async def test_revoking_an_unknown_id_is_404(monkeypatch) -> None:
    """No row by that id."""
    _revoke_answers(monkeypatch, None, None)
    with pytest.raises(HTTPException) as caught:
        await auth_handlers.revoke_token_handler(
            "x", RevokeTokenRequest(reason="r"), _ADMIN
        )
    assert caught.value.status_code == 404


async def test_revoking_twice_is_409(monkeypatch) -> None:
    """The row exists but is already revoked."""
    _revoke_answers(
        monkeypatch,
        None,
        _entry(revoked_at=_NOW, revoked_by="admin-1", revoke_reason="r"),
    )
    with pytest.raises(HTTPException) as caught:
        await auth_handlers.revoke_token_handler(
            "x", RevokeTokenRequest(reason="r"), _ADMIN
        )
    assert caught.value.status_code == 409


async def test_a_revoke_drops_this_pods_cached_read(monkeypatch) -> None:
    """The next request on this pod reads the revoked row at once."""
    entry = _entry(revoked_at=_NOW, revoked_by="admin-1", revoke_reason="leaked")
    rbac_token._registry_cache[entry.token_hash] = (time.monotonic(), _entry())
    _revoke_answers(monkeypatch, (entry, 1), entry)

    response = await auth_handlers.revoke_token_handler(
        entry.id, RevokeTokenRequest(reason="leaked"), _ADMIN
    )
    assert entry.token_hash not in rbac_token._registry_cache
    assert response.merchant_tokens_cleared == 1
    assert response.token.revoke_reason == "leaked"


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_revoke_needs_a_reason(reason) -> None:
    """Blank reasons are refused before the handler runs."""
    with pytest.raises(ValidationError):
        RevokeTokenRequest(reason=reason)
