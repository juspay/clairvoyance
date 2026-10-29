"""token_registry at mint: every long-lived token is written down, by hash.

What is proven here:
- ``create_registered_token`` writes exactly one row whose id is the token's
  ``jti``, whose hash is the sha256 of the token, and whose iat/exp/role/
  scopes are the token's own claims;
- the registered token differs from a plain one only by ``jti``;
- it fails CLOSED: a registry write that errors or returns no row gives a
  500 and no token;
- both long-lived doors use it (``POST /auth/s2s/token`` → kind ``s2s``,
  ``POST /merchant`` with ``issue_token`` → kind ``merchant``), and a failed
  write on the merchant door never stores the token on the merchant row;
- short-lived tokens are untouched: no ``jti``, no row;
- new tokens are capped at 365 days (S2S) and 3650 days (merchant);
- the "RBAC token verified" log line names the ``jti``.

Hermetic: the accessor's query runner is replaced by an in-memory stand-in,
so the real query builder, accessor and decoder all run.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routers.breeze_buddy.auth import handlers as auth_handlers
from app.api.routers.breeze_buddy.merchants import handlers as merchant_handlers
from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.logger import logger
from app.database.accessor.breeze_buddy import token_registry as registry_module
from app.database.queries.breeze_buddy.token_registry import register_token_query
from app.schemas import S2STokenRequest, UserInDB, UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import MerchantCreate, MerchantResponse

_COLUMNS = (
    "id",
    "token_hash",
    "kind",
    "source",
    "subject",
    "username",
    "role",
    "reseller_ids",
    "merchant_ids",
    "created_by",
    "issued_at",
    "expires_at",
)


class _Registry:
    """Stands in for run_parameterized_query on the registry accessor."""

    def __init__(self, fail: bool = False, empty: bool = False) -> None:
        self.rows = []
        self.fail = fail
        self.empty = empty

    async def __call__(self, query, values):
        """Record the insert the way Postgres would, or fail as asked."""
        if self.fail:
            raise RuntimeError("db down")
        if self.empty:
            return []
        row = dict(zip(_COLUMNS, values))
        row["created_at"] = datetime.now(timezone.utc)
        self.rows.append(row)
        return [row]


def _claims(token: str) -> dict:
    """Decode a token with the app's own key."""
    return pyjwt.decode(
        token,
        rbac_token_manager.jwt_manager.secret_key,
        algorithms=[rbac_token_manager.jwt_manager.algorithm],
    )


@pytest.fixture
def registry(monkeypatch) -> _Registry:
    """A working in-memory registry behind the real accessor."""
    fake = _Registry()
    monkeypatch.setattr(registry_module, "run_parameterized_query", fake)
    return fake


def _broken_registry(monkeypatch, **kwargs) -> _Registry:
    """A registry that errors (fail=True) or writes nothing (empty=True)."""
    fake = _Registry(**kwargs)
    monkeypatch.setattr(registry_module, "run_parameterized_query", fake)
    return fake


async def _mint() -> str:
    """Mint a registered S2S token with test defaults."""
    return await rbac_token_manager.create_registered_token(
        kind="admin",
        created_by="admin-1",
        user_id="admin-1",
        username="admin",
        role=UserRole.RESELLER,
        reseller_ids=["r1"],
        merchant_ids=["*"],
        expires_delta=timedelta(days=30),
        email="admin@example.test",
    )


async def test_a_registered_mint_writes_one_row_named_by_the_tokens_jti(
    registry,
) -> None:
    """The row's id is the jti, and its hash, times, role and scopes are the
    token's own."""
    token = await _mint()
    claims = _claims(token)

    assert len(registry.rows) == 1
    row = registry.rows[0]
    assert row["id"] == claims["jti"]
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert row["token_hash"] != token
    assert row["kind"] == "admin"
    assert row["source"] == "minted"
    assert row["subject"] == claims["sub"] == "admin-1"
    assert row["username"] == "admin"
    assert row["role"] == claims["role"] == "reseller"
    assert row["reseller_ids"] == ["r1"]
    assert row["merchant_ids"] == ["*"]
    assert row["created_by"] == "admin-1"
    assert row["issued_at"] == datetime.fromtimestamp(claims["iat"], timezone.utc)
    assert row["expires_at"] == datetime.fromtimestamp(claims["exp"], timezone.utc)


async def test_every_mint_gets_its_own_jti(registry) -> None:
    """Two mints for the same user are two rows, told apart by jti."""
    first, second = await _mint(), await _mint()
    assert _claims(first)["jti"] != _claims(second)["jti"]
    assert len({row["id"] for row in registry.rows}) == 2


async def test_a_registered_token_differs_from_a_plain_one_only_by_jti(
    registry,
) -> None:
    """Every other claim is what the plain mint would have produced."""
    registered = _claims(await _mint())
    plain = _claims(
        rbac_token_manager.create_access_token_with_rbac(
            user_id="admin-1",
            username="admin",
            role=UserRole.RESELLER,
            reseller_ids=["r1"],
            merchant_ids=["*"],
            email="admin@example.test",
            expires_delta=timedelta(days=30),
        )
    )
    for claims in (registered, plain):
        claims.pop("iat")
        claims.pop("exp")
    assert "jti" not in plain
    registered.pop("jti")
    assert registered == plain


@pytest.mark.parametrize("broken", [{"fail": True}, {"empty": True}])
async def test_a_registry_write_that_fails_gives_a_500_and_no_token(
    monkeypatch, broken
) -> None:
    """Fail closed: no row, no token — whether the insert raised or wrote
    nothing."""
    _broken_registry(monkeypatch, **broken)
    with pytest.raises(HTTPException) as caught:
        await _mint()
    assert caught.value.status_code == 500
    assert caught.value.detail == "Could not create access token"


async def test_a_short_lived_token_carries_no_jti_and_writes_no_row(
    registry,
) -> None:
    """Login, launch and signup tokens mint as before: nothing registered."""
    token = rbac_token_manager.create_access_token_with_rbac(
        user_id="u1",
        username="someone",
        role=UserRole.MERCHANT,
        reseller_ids=["r1"],
        merchant_ids=["m1"],
    )
    assert "jti" not in _claims(token)
    assert registry.rows == []


def _admin(**overrides) -> UserInDB:
    """An active admin with wildcard scopes."""
    fields = dict(
        id="admin-1",
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        email="admin@example.test",
        reseller_ids=["*"],
        merchant_ids=["*"],
    )
    fields.update(overrides)
    return UserInDB(**fields)


def _admin_logs_in(monkeypatch, user: UserInDB) -> None:
    """The S2S door finds this user and accepts the password."""

    async def by_username(username):
        """Test double: stands in for get_user_by_username."""
        return user

    async def password_ok(password, password_hash):
        """Test double: stands in for verify_password_async."""
        return True

    monkeypatch.setattr(auth_handlers, "get_user_by_username", by_username)
    monkeypatch.setattr(auth_handlers, "verify_password_async", password_ok)


async def test_the_s2s_door_registers_its_token_as_kind_admin(
    monkeypatch, registry
) -> None:
    """POST /auth/s2s/token: the token returned is the token registered, with
    the downgraded role it actually carries."""
    _admin_logs_in(monkeypatch, _admin())
    response = await auth_handlers.generate_s2s_token_handler(
        S2STokenRequest(
            username="admin",
            password="pw",
            token_lifetime_days=90,
            merchant_ids=["m1"],
        )
    )

    (row,) = registry.rows
    assert row["kind"] == "admin"
    assert row["created_by"] == "admin-1"
    assert row["merchant_ids"] == ["m1"]
    assert row["role"] == "reseller"
    assert row["id"] == _claims(response.access_token)["jti"]
    assert (
        row["token_hash"] == hashlib.sha256(response.access_token.encode()).hexdigest()
    )
    assert response.expires_in == 90 * 24 * 3600


async def test_the_s2s_door_answers_500_when_the_registry_is_down(
    monkeypatch,
) -> None:
    """No row → no S2S token handed out."""
    _admin_logs_in(monkeypatch, _admin())
    _broken_registry(monkeypatch, fail=True)
    with pytest.raises(HTTPException) as caught:
        await auth_handlers.generate_s2s_token_handler(
            S2STokenRequest(username="admin", password="pw")
        )
    assert caught.value.status_code == 500


class _MerchantStore:
    """Stands in for the merchant accessors the create handler calls."""

    def __init__(self) -> None:
        self.stored_tokens = []

    async def exists(self, merchant_id):
        """No merchant of that id yet."""
        return False

    async def create(self, **kwargs):
        """Create the merchant row."""
        return MerchantResponse(
            merchant_id=kwargs["merchant_id"],
            name=kwargs["name"],
            reseller_id=kwargs["reseller_id"],
        )

    async def store_token(self, merchant_id, token):
        """Record what lands on merchants.s2s_token."""
        self.stored_tokens.append((merchant_id, token))
        return True


@pytest.fixture
def merchants(monkeypatch) -> _MerchantStore:
    """In-memory merchant accessors."""
    store = _MerchantStore()
    accessors = merchant_handlers.merchant_accessors
    monkeypatch.setattr(accessors, "check_merchant_identifier_exists", store.exists)
    monkeypatch.setattr(accessors, "create_merchant", store.create)
    monkeypatch.setattr(accessors, "set_merchant_s2s_token", store.store_token)
    return store


_ADMIN_CALLER = UserInfo(
    id="admin-1",
    username="admin",
    role=UserRole.ADMIN,
    reseller_ids=["*"],
    merchant_ids=["*"],
)


def _new_merchant() -> MerchantCreate:
    """A merchant create that asks for a token."""
    return MerchantCreate(
        merchant_id="shop-1", name="Shop", reseller_id="r1", issue_token=True
    )


async def test_the_merchant_door_registers_the_token_it_stores(
    merchants, registry
) -> None:
    """POST /merchant with issue_token: kind merchant, minted by the caller,
    and the stored + returned token is the registered one."""
    merchant = await merchant_handlers.create_merchant_handler(
        _new_merchant(), _ADMIN_CALLER
    )

    assert merchant.token is not None
    (row,) = registry.rows
    assert row["kind"] == "merchant"
    assert row["created_by"] == "admin-1"
    assert row["subject"] == "merchant:shop-1"
    assert row["reseller_ids"] == ["r1"]
    assert row["merchant_ids"] == ["shop-1"]
    assert merchants.stored_tokens == [("shop-1", merchant.token)]
    assert row["token_hash"] == hashlib.sha256(merchant.token.encode()).hexdigest()


async def test_the_merchant_door_never_stores_an_unregistered_token(
    monkeypatch, merchants
) -> None:
    """A failed registry write answers 500 and nothing reaches s2s_token."""
    _broken_registry(monkeypatch, fail=True)
    with pytest.raises(HTTPException) as caught:
        await merchant_handlers.create_merchant_handler(_new_merchant(), _ADMIN_CALLER)
    assert caught.value.status_code == 500
    assert merchants.stored_tokens == []


@pytest.mark.parametrize(
    "build, days, accepted",
    [
        (
            lambda d: S2STokenRequest(
                username="a", password="p", token_lifetime_days=d
            ),
            365,
            True,
        ),
        (
            lambda d: S2STokenRequest(
                username="a", password="p", token_lifetime_days=d
            ),
            366,
            False,
        ),
        (
            lambda d: MerchantCreate(merchant_id="shop-1", token_lifetime_days=d),
            3650,
            True,
        ),
        (
            lambda d: MerchantCreate(merchant_id="shop-1", token_lifetime_days=d),
            3651,
            False,
        ),
    ],
)
def test_new_tokens_are_capped(build, days, accepted) -> None:
    """S2S tokens live at most 365 days; per-merchant tokens 3650."""
    if accepted:
        build(days)
    else:
        with pytest.raises(ValidationError):
            build(days)


def test_the_insert_has_one_parameter_per_placeholder() -> None:
    """Twelve values for twelve placeholders, in column order."""
    now = datetime.now(timezone.utc)
    query, values = register_token_query(
        token_id="00000000-0000-0000-0000-000000000001",
        token_hash="a" * 64,
        kind="admin",
        source="minted",
        subject="admin-1",
        username="admin",
        role="admin",
        reseller_ids=["*"],
        merchant_ids=["*"],
        created_by="admin-1",
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    assert len(values) == 12
    assert "$12" in query and "$13" not in query
    assert values[1] == "a" * 64


@pytest.mark.parametrize("with_jti", [True, False])
async def test_the_verified_log_line_names_the_jti(registry, with_jti) -> None:
    """Logs can tell two tokens of one user apart; a token without a jti
    logs '-'."""
    if with_jti:
        token = await _mint()
        expected = f"jti: {_claims(token)['jti']})"
    else:
        token = rbac_token_manager.create_access_token_with_rbac(
            user_id="u1",
            username="someone",
            role=UserRole.MERCHANT,
            reseller_ids=["r1"],
            merchant_ids=["m1"],
        )
        expected = "jti: -)"

    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        rbac_token_manager.verify_rbac_token(token)
    finally:
        logger.remove(sink)
    (verified,) = [line for line in lines if "RBAC token verified" in line]
    assert expected in verified
