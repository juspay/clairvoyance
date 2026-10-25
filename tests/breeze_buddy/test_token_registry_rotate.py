"""Rotation, the login cut-off and deactivation (token registry 3/4).

What is proven here:
- every token's subject is checked: an inactive user or merchant is a 401
  (login and long-lived tokens alike); a subject that doesn't exist is a 401
  too (enforced, 4/4); an unreadable subject is a 503;
  a pod reads a subject once per window and ``forget_subject`` drops it;
- the login cut-off: a short-lived token issued before the user's
  ``tokens_valid_after`` is a 401 (the same second included); one issued
  after passes; long-lived tokens are not cut off by it;
- deactivating a user stamps the cut-off in the same UPDATE, re-activating
  doesn't; the handlers drop this pod's read of a deactivated user or
  merchant; revoke-sessions is admin-only, 404s an unknown user, and drops
  the read;
- rotate: admin only; 404 / 409 (revoked or rotating, admin user gone, lost
  race) / 422 (grace asked for a merchant token); the successor keeps the
  subject, role, scopes and lifetime (capped), carries a new jti and the
  owner_id the mint would give it (the admin user's today; a merchant
  token's minter); admin grace defaults to 24 h, merchant tokens switch at
  once; the old token's cached read is dropped;
- the rotate statement and the request/response shapes.

Hermetic: accessor functions are replaced by in-memory stand-ins. The SQL
(one statement, the FK, the merchant swap) is proven on Postgres in the
PR's end-to-end run.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routers.breeze_buddy.auth import handlers as auth_handlers
from app.api.routers.breeze_buddy.merchants import handlers as merchant_handlers
from app.api.routers.breeze_buddy.users import handlers as user_handlers
from app.api.security.breeze_buddy import rbac_token
from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.database.accessor.breeze_buddy import token_registry as registry_module
from app.database.decoder.breeze_buddy.users import decode_user_token_state
from app.database.queries.breeze_buddy.token_registry import rotate_token_query
from app.database.queries.breeze_buddy.users import update_user_query
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import MerchantResponse, MerchantUpdate
from app.schemas.breeze_buddy.token_registry import (
    RotateTokenRequest,
    TokenRegistryEntry,
)
from app.schemas.breeze_buddy.users import UserResponse, UserTokenState, UserUpdate

_NOW = datetime.now(timezone.utc)

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


def _claims(token: str) -> Dict[str, Any]:
    """A token's claims (no verify: the test minted it)."""
    return pyjwt.decode(token, options={"verify_signature": False})


def _login_token(user_id: str = "user-1", issued: Optional[datetime] = None) -> str:
    """A short-lived (login-length) token, optionally issued at ``issued``."""
    if issued is None:
        return rbac_token_manager.create_access_token_with_rbac(
            user_id=user_id,
            username="someone",
            role=UserRole.MERCHANT,
            reseller_ids=["r1"],
            merchant_ids=["shop-1"],
        )
    return pyjwt.encode(
        {
            "sub": user_id,
            "username": "someone",
            "role": "merchant",
            "reseller_ids": ["r1"],
            "merchant_ids": ["shop-1"],
            "iat": int(issued.timestamp()),
            "exp": int((issued + timedelta(minutes=30)).timestamp()),
        },
        rbac_token_manager.jwt_manager.secret_key,
        algorithm=rbac_token_manager.jwt_manager.algorithm,
    )


def _long_token(user_id: str = "user-1", merchant_ids=None) -> str:
    """A long-lived token (30 days, no jti)."""
    return rbac_token_manager.create_access_token_with_rbac(
        user_id=user_id,
        username="svc",
        role=UserRole.RESELLER,
        reseller_ids=["r1"],
        merchant_ids=merchant_ids or ["*"],
        expires_delta=timedelta(days=30),
    )


def _entry(**overrides: Any) -> TokenRegistryEntry:
    """A registry row with test defaults (an admin token, live)."""
    fields: Dict[str, Any] = dict(
        id="11111111-1111-1111-1111-111111111111",
        token_hash="a" * 64,
        kind="admin",
        source="minted",
        subject="user-1",
        username="svc",
        role="reseller",
        reseller_ids=["r1"],
        merchant_ids=["*"],
        created_by="admin-1",
        issued_at=_NOW - timedelta(days=10),
        expires_at=_NOW + timedelta(days=80),
        created_at=_NOW - timedelta(days=10),
        last_seen_at=_NOW,
    )
    fields.update(overrides)
    return TokenRegistryEntry(**fields)


class _Subjects:
    """Stands in for the user and merchant lookups the verifier makes."""

    def __init__(self) -> None:
        self.users: Dict[str, Optional[UserTokenState]] = {}
        self.merchants: Dict[str, Optional[MerchantResponse]] = {}
        self.reads: List[str] = []
        self.fail = False

    async def user(self, user_id):
        """get_user_token_state."""
        if self.fail:
            raise RuntimeError("db down")
        self.reads.append(user_id)
        return self.users.get(user_id, UserTokenState(is_active=True))

    async def merchant(self, merchant_id):
        """get_merchant_by_merchant_identifier."""
        if self.fail:
            raise RuntimeError("db down")
        self.reads.append(f"merchant:{merchant_id}")
        return self.merchants.get(
            merchant_id, MerchantResponse(merchant_id=merchant_id, is_active=True)
        )


@pytest.fixture(autouse=True)
def _empty_caches():
    """Each test starts with no cached reads."""
    rbac_token._registry_cache.clear()
    rbac_token._subject_cache.clear()
    yield
    rbac_token._registry_cache.clear()
    rbac_token._subject_cache.clear()


@pytest.fixture
def subjects(monkeypatch) -> _Subjects:
    """In-memory subjects, and a registry where every token is live."""
    fake = _Subjects()
    monkeypatch.setattr(rbac_token, "get_user_token_state", fake.user)
    monkeypatch.setattr(
        rbac_token, "get_merchant_by_merchant_identifier", fake.merchant
    )

    async def live(token_hash):
        """get_token_by_hash: a live row."""
        return _entry(token_hash=token_hash)

    async def seen(token_id, seen_from, route):
        """mark_token_seen."""
        return _NOW

    monkeypatch.setattr(rbac_token, "get_token_by_hash", live)
    monkeypatch.setattr(rbac_token, "mark_token_seen", seen)
    return fake


# --- the subject must be active -----------------------------------------------


@pytest.mark.parametrize(
    "token_of", [_login_token, _long_token], ids=["login", "admin"]
)
async def test_an_inactive_user_s_tokens_are_refused(subjects, token_of) -> None:
    """Deactivation stops every token of the user, login and admin alike."""
    subjects.users["user-1"] = UserTokenState(is_active=False)
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(token_of())
    assert caught.value.status_code == 401


async def test_an_active_user_passes(subjects) -> None:
    """The ordinary case: one read, then through."""
    user = await rbac_token_manager.authenticate(_login_token())
    assert user.id == "user-1"
    assert subjects.reads == ["user-1"]


@pytest.mark.parametrize(
    "lifetime",
    [timedelta(minutes=60), timedelta(days=3650)],
    ids=["launch", "merchant"],
)
async def test_an_inactive_merchant_s_tokens_are_refused(subjects, lifetime) -> None:
    """``merchant:<id>`` subjects (per-merchant and launch tokens) read the
    merchant's own is_active."""
    subjects.merchants["shop-1"] = MerchantResponse(
        merchant_id="shop-1", is_active=False
    )
    token = rbac_token_manager.create_access_token_with_rbac(
        user_id="merchant:shop-1",
        username="merchant-shop-1",
        role=UserRole.MERCHANT,
        reseller_ids=["r1"],
        merchant_ids=["shop-1"],
        expires_delta=lifetime,
    )
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(token)
    assert caught.value.status_code == 401
    assert subjects.reads == ["merchant:shop-1"]


@pytest.mark.parametrize(
    "token_of", [_login_token, _long_token], ids=["login", "admin"]
)
async def test_a_subject_that_does_not_exist_is_refused(subjects, token_of) -> None:
    """Deleted user: its tokens are refused (enforced, 4/4)."""
    subjects.users["user-1"] = None
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(token_of())
    assert caught.value.status_code == 401


async def test_an_unreadable_subject_is_503(subjects) -> None:
    """Fail closed, as the registry does."""
    subjects.fail = True
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(_login_token())
    assert caught.value.status_code == 503


async def test_a_subject_is_read_once_per_window(subjects, monkeypatch) -> None:
    """Two requests → one read; past the window → a fresh one."""
    token = _login_token()
    await rbac_token_manager.authenticate(token)
    await rbac_token_manager.authenticate(token)
    assert subjects.reads == ["user-1"]

    later = time.monotonic() + rbac_token._REGISTRY_CACHE_SECONDS + 1
    monkeypatch.setattr(rbac_token.time, "monotonic", lambda: later)
    await rbac_token_manager.authenticate(token)
    assert subjects.reads == ["user-1", "user-1"]


async def test_forget_subject_drops_this_pods_read(subjects) -> None:
    """After a deactivation on this pod, the next request reads again."""
    token = _login_token()
    await rbac_token_manager.authenticate(token)
    rbac_token_manager.forget_subject("user-1")
    await rbac_token_manager.authenticate(token)
    assert subjects.reads == ["user-1", "user-1"]


@pytest.mark.parametrize(
    "stored, active", [(True, True), (False, False), (None, False)]
)
def test_a_null_is_active_reads_as_inactive(stored, active) -> None:
    """The decoder fails closed on NULL, the same as the login check."""
    state = decode_user_token_state({"is_active": stored, "tokens_valid_after": None})
    assert state.is_active is active


# --- the login cut-off ------------------------------------------------------------


async def test_a_login_token_issued_before_the_cut_off_is_refused(subjects) -> None:
    """Issued a minute before tokens_valid_after → 401."""
    subjects.users["user-1"] = UserTokenState(is_active=True, tokens_valid_after=_NOW)
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.authenticate(
            _login_token(issued=_NOW - timedelta(minutes=1))
        )
    assert caught.value.status_code == 401


async def test_a_login_token_issued_after_the_cut_off_passes(subjects) -> None:
    """Signed in again after the cut-off → through."""
    subjects.users["user-1"] = UserTokenState(
        is_active=True, tokens_valid_after=_NOW - timedelta(minutes=5)
    )
    await rbac_token_manager.authenticate(_login_token(issued=_NOW))


async def test_a_login_in_the_same_second_as_the_cut_off_is_refused(
    subjects,
) -> None:
    """iat is whole seconds: the same second reads as before (fail closed)."""
    cut = _NOW.replace(microsecond=500_000)
    subjects.users["user-1"] = UserTokenState(is_active=True, tokens_valid_after=cut)
    with pytest.raises(HTTPException):
        await rbac_token_manager.authenticate(_login_token(issued=cut))


async def test_a_long_lived_token_is_not_cut_off(subjects) -> None:
    """Service tokens are revoked one by one, never by the login cut-off."""
    subjects.users["user-1"] = UserTokenState(
        is_active=True, tokens_valid_after=_NOW + timedelta(days=1)
    )
    await rbac_token_manager.authenticate(_long_token())


# --- deactivation and revoke-sessions ------------------------------------------------


@pytest.mark.parametrize(
    "is_active, stamps", [(False, True), (True, False), (None, False)]
)
def test_only_deactivation_stamps_the_cut_off(is_active, stamps) -> None:
    """is_active=false sets tokens_valid_after in the same UPDATE."""
    query, _ = update_user_query(user_id="u", email="e@x.test", is_active=is_active)
    assert ("tokens_valid_after = now()" in query) is stamps


def _target_user(**overrides: Any) -> UserResponse:
    """The user being updated."""
    fields: Dict[str, Any] = dict(
        id="user-1", username="someone", role=UserRole.MERCHANT, merchant_ids=["shop-1"]
    )
    fields.update(overrides)
    return UserResponse(**fields)


@pytest.mark.parametrize(
    "is_active, forgotten", [(False, True), (True, True), (None, False)]
)
async def test_changing_a_users_active_flag_drops_this_pods_read(
    monkeypatch, is_active, forgotten
) -> None:
    """update_user_handler forgets the user whenever is_active changes — a
    re-activated user must not wait out a cached "inactive"."""
    rbac_token._subject_cache["user-1"] = (
        time.monotonic(),
        rbac_token._Subject(True, True, None),
    )

    async def get_user(user_id):
        """The target user."""
        return _target_user()

    async def allowed(current_user, target):
        """Access granted."""
        return None

    async def update(**kwargs):
        """The update succeeds."""
        return _target_user(is_active=is_active is not False)

    monkeypatch.setattr(user_handlers.user_accessors, "get_user_by_id", get_user)
    monkeypatch.setattr(user_handlers, "_check_update_delete_access", allowed)
    monkeypatch.setattr(user_handlers.user_accessors, "update_user", update)

    await user_handlers.update_user_handler(
        "user-1", UserUpdate(is_active=is_active), _ADMIN
    )
    assert ("user-1" not in rbac_token._subject_cache) is forgotten


@pytest.mark.parametrize(
    "is_active, forgotten", [(False, True), (True, True), (None, False)]
)
async def test_changing_a_merchants_active_flag_drops_this_pods_read(
    monkeypatch, is_active, forgotten
) -> None:
    """update_merchant_handler forgets ``merchant:<id>`` whenever is_active
    changes."""
    rbac_token._subject_cache["merchant:shop-1"] = (
        time.monotonic(),
        rbac_token._Subject(True, True, None),
    )
    shop = MerchantResponse(merchant_id="shop-1", reseller_id="r1")

    async def get_merchant(merchant_id):
        """The merchant."""
        return shop

    async def update(**kwargs):
        """The update succeeds."""
        return shop

    accessors = merchant_handlers.merchant_accessors
    monkeypatch.setattr(accessors, "get_merchant_by_merchant_identifier", get_merchant)
    monkeypatch.setattr(accessors, "update_merchant", update)

    await merchant_handlers.update_merchant_handler(
        "shop-1", MerchantUpdate(is_active=is_active), _ADMIN
    )
    assert ("merchant:shop-1" not in rbac_token._subject_cache) is forgotten


async def test_revoke_sessions_is_admin_only() -> None:
    """A reseller gets 403."""
    with pytest.raises(HTTPException) as caught:
        await user_handlers.revoke_user_sessions_handler("user-1", _RESELLER)
    assert caught.value.status_code == 403


async def test_revoke_sessions_for_an_unknown_user_is_404(monkeypatch) -> None:
    """No such user → 404."""

    async def stamp(user_id):
        """revoke_user_sessions: no row."""
        return None

    monkeypatch.setattr(user_handlers.user_accessors, "revoke_user_sessions", stamp)
    with pytest.raises(HTTPException) as caught:
        await user_handlers.revoke_user_sessions_handler("nobody", _ADMIN)
    assert caught.value.status_code == 404


async def test_revoke_sessions_stamps_and_drops_this_pods_read(monkeypatch) -> None:
    """The stamp comes back, and the cached read of the user is gone."""
    rbac_token._subject_cache["user-1"] = (
        time.monotonic(),
        rbac_token._Subject(True, True, None),
    )

    async def stamp(user_id):
        """revoke_user_sessions: stamped now."""
        return _NOW

    monkeypatch.setattr(user_handlers.user_accessors, "revoke_user_sessions", stamp)
    response = await user_handlers.revoke_user_sessions_handler("user-1", _ADMIN)
    assert response.tokens_valid_after == _NOW
    assert "user-1" not in rbac_token._subject_cache


# --- rotate ---------------------------------------------------------------------------


class _Rotation:
    """Stands in for the accessors the rotate handler calls."""

    def __init__(self, old: Optional[TokenRegistryEntry]) -> None:
        self.old = old
        self.owner: Optional[UserResponse] = _target_user(
            id="user-1",
            email="svc@example.test",
            owner_id="owner-9",
            role=UserRole.ADMIN,
        )
        self.calls: List[Dict[str, Any]] = []
        self.race = False

    async def get_token(self, token_id):
        """get_token_by_id."""
        return self.old

    async def get_owner(self, user_id):
        """get_user_by_id."""
        return self.owner

    async def rotate(self, **kwargs):
        """rotate_token: the old row as rotated, the new row, 1 swapped."""
        self.calls.append(kwargs)
        if self.race:
            return None
        assert self.old is not None
        replaced = self.old.model_copy(
            update={
                "revoked_at": _NOW + timedelta(hours=kwargs["grace_hours"]),
                "revoked_by": kwargs["rotated_by"],
                "revoke_reason": "rotated",
                "replaced_by": kwargs["new_id"],
            }
        )
        new = self.old.model_copy(
            update={
                "id": kwargs["new_id"],
                "token_hash": kwargs["new_token_hash"],
                "issued_at": kwargs["issued_at"],
                "expires_at": kwargs["expires_at"],
            }
        )
        return replaced, new, 1


def _rotation(monkeypatch, old: Optional[TokenRegistryEntry]) -> _Rotation:
    """Wire a _Rotation behind the handler."""
    fake = _Rotation(old)
    monkeypatch.setattr(
        auth_handlers.token_accessors, "get_token_by_id", fake.get_token
    )
    monkeypatch.setattr(auth_handlers.token_accessors, "rotate_token", fake.rotate)
    monkeypatch.setattr(auth_handlers, "get_user_by_id", fake.get_owner)
    return fake


async def _rotate(grace: Optional[int] = None, caller: UserInfo = _ADMIN):
    """Call the handler."""
    return await auth_handlers.rotate_token_handler(
        "11111111-1111-1111-1111-111111111111",
        RotateTokenRequest(grace_hours=grace),
        caller,
    )


async def test_rotate_is_admin_only(monkeypatch) -> None:
    """A reseller gets 403 before anything is read."""
    _rotation(monkeypatch, _entry())
    with pytest.raises(HTTPException) as caught:
        await _rotate(caller=_RESELLER)
    assert caught.value.status_code == 403


async def test_rotating_an_unknown_id_is_404(monkeypatch) -> None:
    """No such row."""
    _rotation(monkeypatch, None)
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 404


@pytest.mark.parametrize(
    "revoked_at",
    [_NOW - timedelta(hours=1), _NOW + timedelta(hours=1)],
    ids=["revoked", "rotating"],
)
async def test_rotating_a_revoked_or_rotating_token_is_409(
    monkeypatch, revoked_at
) -> None:
    """Nothing is minted for a token already on its way out."""
    fake = _rotation(
        monkeypatch,
        _entry(revoked_at=revoked_at, revoked_by="admin-1", revoke_reason="x"),
    )
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 409
    assert fake.calls == []


async def test_an_admin_successor_keeps_identity_scopes_and_lifetime(
    monkeypatch,
) -> None:
    """Same subject, role, scopes and lifetime; a new jti; the owner_id and
    email the admin mint would give it today."""
    old = _entry()
    _rotation(monkeypatch, old)
    response = await _rotate()

    claims = _claims(response.token)
    assert claims["sub"] == old.subject
    assert claims["username"] == old.username
    assert claims["role"] == "reseller"
    assert claims["reseller_ids"] == old.reseller_ids
    assert claims["merchant_ids"] == old.merchant_ids
    assert claims["owner_id"] == "owner-9"
    assert claims["email"] == "svc@example.test"
    assert claims["jti"] == response.new.id != old.id
    assert claims["exp"] - claims["iat"] == int(
        (old.expires_at - old.issued_at).total_seconds()
    )
    assert response.new.token_hash == rbac_token.token_hash_of(response.token)


async def test_an_admin_rotation_defaults_to_a_24_hour_grace(monkeypatch) -> None:
    """No grace asked → 24 h; an explicit one is used as given."""
    fake = _rotation(monkeypatch, _entry())
    await _rotate()
    assert fake.calls[0]["grace_hours"] == 24
    fake.old = _entry()
    await _rotate(grace=2)
    assert fake.calls[1]["grace_hours"] == 2
    assert fake.calls[1]["rotated_by"] == "admin-1"


async def test_a_successor_never_outlives_its_doors_cap(monkeypatch) -> None:
    """A 1000-day admin token (minted before the caps) rotates into 365 days."""
    _rotation(
        monkeypatch,
        _entry(
            issued_at=_NOW - timedelta(days=1), expires_at=_NOW + timedelta(days=999)
        ),
    )
    claims = _claims((await _rotate()).token)
    assert claims["exp"] - claims["iat"] == 365 * 24 * 3600


async def test_an_admin_token_whose_user_is_gone_is_409(monkeypatch) -> None:
    """Its owner_id can't be rebuilt — mint a new token instead."""
    fake = _rotation(monkeypatch, _entry())
    fake.owner = None
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 409
    assert fake.calls == []


def _merchant_entry(**overrides: Any) -> TokenRegistryEntry:
    """A live per-merchant token row."""
    fields: Dict[str, Any] = dict(
        kind="merchant",
        subject="merchant:shop-1",
        username="merchant-shop-1",
        role="merchant",
        reseller_ids=["r1"],
        merchant_ids=["shop-1"],
        created_by="minter-7",
    )
    fields.update(overrides)
    return _entry(**fields)


async def test_a_merchant_token_switches_at_once(monkeypatch) -> None:
    """Grace 0; owner_id is the minter; no email."""
    fake = _rotation(monkeypatch, _merchant_entry())
    response = await _rotate()
    claims = _claims(response.token)
    assert fake.calls[0]["grace_hours"] == 0
    assert fake.calls[0]["new_token"] == response.token
    assert claims["owner_id"] == "minter-7"
    assert claims["email"] is None
    assert response.merchant_tokens_swapped == 1


async def test_a_backfilled_merchant_token_takes_the_rotator_as_owner(
    monkeypatch,
) -> None:
    """No minter on record → the admin rotating it."""
    _rotation(monkeypatch, _merchant_entry(created_by=None, source="backfilled"))
    claims = _claims((await _rotate()).token)
    assert claims["owner_id"] == "admin-1"


async def test_a_grace_for_a_merchant_token_is_422(monkeypatch) -> None:
    """Its doors compare against the stored value — no grace can apply."""
    fake = _rotation(monkeypatch, _merchant_entry())
    with pytest.raises(HTTPException) as caught:
        await _rotate(grace=4)
    assert caught.value.status_code == 422
    assert fake.calls == []


async def test_a_lost_race_is_409_and_returns_no_token(monkeypatch) -> None:
    """Revoked between the read and the statement: nothing stored, 409."""
    fake = _rotation(monkeypatch, _entry())
    fake.race = True
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 409


async def test_a_rotation_drops_the_old_tokens_cached_read(monkeypatch) -> None:
    """This pod reads the old row again on its next use."""
    old = _entry()
    rbac_token._registry_cache[old.token_hash] = (time.monotonic(), old)
    _rotation(monkeypatch, old)
    await _rotate()
    assert old.token_hash not in rbac_token._registry_cache


async def test_a_legacy_shop_role_rotates_as_user(monkeypatch) -> None:
    """The verifier reads "shop" as "user"; the successor is minted so."""
    _rotation(monkeypatch, _entry(role="shop"))
    assert _claims((await _rotate()).token)["role"] == "user"


# --- the statement and the shapes ------------------------------------------------------


def test_the_rotate_statement_guards_and_swaps() -> None:
    """Only a live row rotates; the new row copies it as minted; a stored
    merchant token is swapped by hash among the old token's merchants."""
    query, values = rotate_token_query(
        old_id="o",
        new_id="n",
        new_token_hash="b" * 64,
        new_token="tok",
        rotated_by="admin-1",
        grace_hours=24,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
    )
    assert "WHERE id = $1::uuid AND revoked_at IS NULL" in query
    assert "revoked_at = now() + make_interval(hours => $6::int)" in query
    assert "replaced_by = $2::uuid" in query
    assert "'minted'" in query
    assert "m.merchant_id = ANY(o.merchant_ids)" in query
    assert "= o.token_hash" in query
    assert values == ["o", "n", "b" * 64, "tok", "admin-1", 24, _NOW, values[7]]


async def test_the_rotate_accessor_reads_both_sides(monkeypatch) -> None:
    """Two rows back → (old, new, swapped); none → None."""
    old_row = _entry(
        revoked_at=_NOW,
        revoked_by="a",
        revoke_reason="rotated",
        replaced_by="2" * 8 + "-2222-2222-2222-" + "2" * 12,
    ).model_dump()
    new_row = _entry(
        id="2" * 8 + "-2222-2222-2222-" + "2" * 12, token_hash="b" * 64
    ).model_dump()
    answers = [
        [
            dict(old_row, side="old", merchant_tokens_swapped=1),
            dict(new_row, side="new", merchant_tokens_swapped=1),
        ],
        [],
    ]

    async def runner(query, values):
        """Hand back the next answer."""
        return answers.pop(0)

    monkeypatch.setattr(registry_module, "run_parameterized_query", runner)
    args: Dict[str, Any] = dict(
        old_id="o",
        new_id="n",
        new_token_hash="b" * 64,
        new_token="t",
        rotated_by="a",
        grace_hours=0,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
    )
    result = await registry_module.rotate_token(**args)
    assert result is not None
    replaced, new, swapped = result
    assert replaced.replaced_by == new.id and swapped == 1
    assert await registry_module.rotate_token(**args) is None


@pytest.mark.parametrize(
    "grace, ok", [(0, True), (168, True), (-1, False), (169, False)]
)
def test_grace_is_zero_to_a_week(grace, ok) -> None:
    """0–168 hours."""
    if ok:
        RotateTokenRequest(grace_hours=grace)
    else:
        with pytest.raises(ValidationError):
            RotateTokenRequest(grace_hours=grace)
