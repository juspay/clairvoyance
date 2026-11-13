"""Token extraction and the two s2s doors (A5/A9 seam)."""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, cast

import pytest
from fastapi import HTTPException, Request

import app.crm.auth as crm_auth
from app.crm.auth import _extract_token, verify_s2s_caller, verify_s2s_merchant
from app.schemas.breeze_buddy.auth import UserInfo, UserRole


def _request(headers: Dict[str, str]) -> Request:
    # Only .headers is touched; a stub is honest and avoids ASGI setup.
    return cast(Request, SimpleNamespace(headers=headers))


def test_bearer_token_extracted() -> None:
    req = _request({"authorization": "Bearer abc.def"})
    assert _extract_token(req) == "abc.def"


def test_bearer_is_case_insensitive() -> None:
    req = _request({"authorization": "bearer abc"})
    assert _extract_token(req) == "abc"


def test_s2s_header_fallback() -> None:
    req = _request({"x-s2s-token": "tok1"})
    assert _extract_token(req) == "tok1"


def test_raw_authorization_fallback() -> None:
    req = _request({"authorization": "rawtoken"})
    assert _extract_token(req) == "rawtoken"


def test_missing_token_is_none() -> None:
    assert _extract_token(_request({})) is None


def _s2s(monkeypatch: pytest.MonkeyPatch, stored: str | None) -> None:
    async def fake_get(merchant_id: str) -> str | None:
        return stored

    monkeypatch.setattr(crm_auth, "get_merchant_s2s_token", fake_get)
    monkeypatch.setattr(
        crm_auth.rbac_token_manager, "verify_rbac_token", lambda token: None
    )


def test_s2s_unknown_merchant_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _s2s(monkeypatch, stored=None)  # 404, not 403 — callers can't probe ids
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_merchant("m1", _request({"x-s2s-token": "t"})))
    assert e.value.status_code == 404


def test_s2s_token_mismatch_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _s2s(monkeypatch, stored="right-token")
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_merchant("m1", _request({"x-s2s-token": "wrong"})))
    assert e.value.status_code == 401


def test_s2s_valid_token_returns_merchant(monkeypatch: pytest.MonkeyPatch) -> None:
    _s2s(monkeypatch, stored="tok")
    result = asyncio.run(verify_s2s_merchant("m1", _request({"x-s2s-token": "tok"})))
    assert result == "m1"


# --- verify_s2s_caller: one door, two kinds of caller (A9) ---


def _caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: str | None,
    merchant_ids: List[str] | None = None,
    reseller_ids: List[str] | None = None,
    role: UserRole = UserRole.MERCHANT,
    merchant_reseller: str | None = "R1",
) -> None:
    """Merchant has `stored` provisioned (or not) and belongs to
    `merchant_reseller`; any presented token decodes to a caller carrying
    the given scopes."""

    async def fake_get(merchant_id: str) -> str | None:
        return stored

    async def fake_merchant(merchant_id: str) -> object:
        return SimpleNamespace(reseller_id=merchant_reseller)

    def fake_verify(token: str) -> UserInfo:
        return UserInfo(
            id="u1",
            username="caller",
            role=role,
            merchant_ids=merchant_ids or [],
            reseller_ids=reseller_ids or [],
        )

    async def exists(merchant_id: str) -> bool:
        return True

    monkeypatch.setattr(crm_auth, "get_merchant_s2s_token", fake_get)
    monkeypatch.setattr(crm_auth, "check_merchant_identifier_exists", exists)
    monkeypatch.setattr(crm_auth, "get_merchant_by_merchant_identifier", fake_merchant)
    monkeypatch.setattr(crm_auth.rbac_token_manager, "verify_rbac_token", fake_verify)


@pytest.mark.parametrize(
    "merchant_ids,reseller_ids,role",
    [
        (["*"], ["*"], UserRole.RESELLER),  # wildcard on both — the relay
        (["*"], ["R1"], UserRole.RESELLER),  # every merchant of R1; m1 is R1's
        (["m1"], ["*"], UserRole.RESELLER),  # m1 named outright
        ([], [], UserRole.ADMIN),  # platform admin
    ],
)
def test_caller_wildcard_token_accepted_without_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
    merchant_ids: List[str],
    reseller_ids: List[str],
    role: UserRole,
) -> None:
    # The relay's hot path. A wildcard can only belong to a relay/admin
    # credential — the per-merchant mint hard-codes exactly one id — so
    # there is no stored row it could have been revoked against, and no
    # reason to pay for the read.
    looked_up = False

    async def spy_get(merchant_id: str) -> str | None:
        nonlocal looked_up
        looked_up = True
        return "should-never-be-read"

    _caller(
        monkeypatch,
        stored=None,
        merchant_ids=merchant_ids,
        reseller_ids=reseller_ids,
        role=role,
    )
    monkeypatch.setattr(crm_auth, "get_merchant_s2s_token", spy_get)

    assert asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "relay"})))
    assert not looked_up


def test_caller_wildcard_token_rejects_a_merchant_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "Any merchant" is not "this merchant is real". Without the existence
    # read the envelope's merchant_id is taken on the caller's word, and a
    # typo'd shop domain quietly founds a tenant the belt then resolves
    # customers under. Same 404 the narrow path gives.
    _caller(monkeypatch, stored=None, merchant_ids=["*"], reseller_ids=["*"])

    async def absent(merchant_id: str) -> bool:
        return False

    monkeypatch.setattr(crm_auth, "check_merchant_identifier_exists", absent)

    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_caller("ghost-shop", _request({"x-s2s-token": "relay"})))
    assert e.value.status_code == 404


@pytest.mark.parametrize(
    "merchant_ids,reseller_ids,merchant_reseller",
    [
        (["*"], ["R1"], "R2"),  # token for reseller R1, merchant is R2's
        (["*"], ["R1"], None),  # token for reseller R1, merchant has no reseller
        (["m1"], ["*"], "R1"),  # token for merchant m1, pushed for another
        ([], ["*"], "R1"),  # "*" on resellers names no merchant
    ],
)
def test_caller_wildcard_token_cannot_push_outside_its_scope(
    monkeypatch: pytest.MonkeyPatch,
    merchant_ids: List[str],
    reseller_ids: List[str],
    merchant_reseller: str | None,
) -> None:
    # A "*" on one list is not "any merchant". merchant_ids=["*"] is every
    # merchant of the token's resellers; reseller_ids=["*"] covers only the
    # merchants the token names. Refused with the unknown-merchant 404, so
    # the caller cannot probe what lies outside its scope.
    _caller(
        monkeypatch,
        stored=None,
        merchant_ids=merchant_ids,
        reseller_ids=reseller_ids,
        role=UserRole.RESELLER,
        merchant_reseller=merchant_reseller,
    )
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_caller("beta-bags", _request({"x-s2s-token": "t"})))
    assert e.value.status_code == 404


def test_caller_narrow_token_accepted_when_it_is_the_stored_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _caller(monkeypatch, stored="tok", merchant_ids=["m1"])
    result = asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "tok"})))
    assert result == "m1"


def test_caller_rotated_out_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # THE guarantee the ordering exists for. Per-merchant tokens ARE RBAC
    # JWTs, so this one still decodes and still names its merchant: under
    # a "verify the JWT, fall back only when it throws" order it would be
    # accepted until it expired, because it never throws. Only the
    # byte-compare against the stored value catches it.
    _caller(monkeypatch, stored="rotated-in", merchant_ids=["m1"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "rotated-out"})))
    assert e.value.status_code == 401


def test_caller_narrow_token_with_nothing_provisioned_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail CLOSED: a NULL s2s_token is "missing", and missing is NO.
    # Honouring the token's own claim here would mean CLEARING a
    # merchant's token widened its access instead of revoking it.
    _caller(monkeypatch, stored=None, merchant_ids=["m1"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "t"})))
    assert e.value.status_code == 404


def test_caller_narrow_token_cannot_push_for_another_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cross-tenant is closed by the byte-compare, not by a claim check:
    # a token only ever matches the row it was minted for.
    _caller(monkeypatch, stored="other-shops-token", merchant_ids=["m1"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            verify_s2s_caller("other-shop", _request({"x-s2s-token": "m1s-token"}))
        )
    assert e.value.status_code == 401


def test_caller_no_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _caller(monkeypatch, stored=None, merchant_ids=["*"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_caller("m1", _request({})))
    assert e.value.status_code == 401


# --- every refusal leaves a line (the door used to refuse in silence) ---


class _Lines:
    """A logger that records what each line BOUND, not what it said."""

    def __init__(self, sink: List[Dict[str, Any]] | None = None) -> None:
        self.lines: List[Dict[str, Any]] = [] if sink is None else sink
        self._bound: Dict[str, Any] = {}

    def bind(self, **fields: Any) -> "_Lines":
        view = _Lines(self.lines)
        view._bound = {**self._bound, **fields}
        return view

    def _record(self, message: Any) -> None:
        self.lines.append({**self._bound, "message": str(message)})

    info = warning = error = _record


def _refusals(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    lines = _Lines()
    monkeypatch.setattr(crm_auth, "logger", lines)
    return lines.lines


@pytest.mark.parametrize(
    "reason,stored,scopes,reseller_scopes,headers",
    [
        ("missing_token", None, ["*"], ["*"], {}),
        ("unknown_merchant", None, ["*"], ["*"], {"x-s2s-token": "relay"}),
        ("out_of_scope", None, ["*"], ["R2"], {"x-s2s-token": "relay"}),
        ("not_provisioned", None, ["m1"], [], {"x-s2s-token": "t"}),
        (
            "token_mismatch",
            "rotated-in",
            ["m1"],
            [],
            {"x-s2s-token": "rotated-out"},
        ),
    ],
)
def test_each_refusal_names_its_reason_and_merchant(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    stored: str | None,
    scopes: List[str],
    reseller_scopes: List[str],
    headers: Dict[str, str],
) -> None:
    """A merchant whose token breaks gets no error anyone sees — their
    events just stop arriving. One WARNING per refusal, reason as a
    field so a rule can count them per merchant."""
    _caller(
        monkeypatch, stored=stored, merchant_ids=scopes, reseller_ids=reseller_scopes
    )
    if reason == "unknown_merchant":

        async def absent(merchant_id: str) -> bool:
            return False

        monkeypatch.setattr(crm_auth, "check_merchant_identifier_exists", absent)
    lines = _refusals(monkeypatch)

    with pytest.raises(HTTPException):
        asyncio.run(verify_s2s_caller("m1", _request(headers)))

    assert len(lines) == 1, "one refusal, one line"
    assert lines[0]["reason"] == reason
    assert lines[0]["merchant_id"] == "m1"
    assert lines[0]["component"] == crm_auth.S2S_LOG_COMPONENT


def test_a_token_the_manager_rejects_is_logged_then_re_raised_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token manager's verdict; we add a line and hand its answer
    back untouched."""
    _caller(monkeypatch, stored=None, merchant_ids=["*"])
    refused = HTTPException(status_code=401, detail="Token expired")

    def reject(token: str) -> Any:
        raise refused

    monkeypatch.setattr(crm_auth.rbac_token_manager, "verify_rbac_token", reject)
    lines = _refusals(monkeypatch)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "nope"})))

    assert caught.value is refused  # the same object, not a reworded copy
    assert [line["reason"] for line in lines] == ["bad_token"]


def test_an_accepted_caller_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """One line per accepted push would bury every refusal."""
    _caller(monkeypatch, stored="tok", merchant_ids=["m1"])
    lines = _refusals(monkeypatch)

    assert (
        asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "tok"}))) == "m1"
    )
    assert lines == []


def test_caller_fails_closed_when_token_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A DB blip must not demote a merchant to a weaker check — the error
    # propagates instead of falling through to an accept.
    _caller(monkeypatch, stored=None, merchant_ids=["m1"])

    async def broken_get(merchant_id: str) -> str | None:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(crm_auth, "get_merchant_s2s_token", broken_get)

    with pytest.raises(RuntimeError):
        asyncio.run(verify_s2s_caller("m1", _request({"x-s2s-token": "t"})))


# --- the tenancy check merchant-facing CRM routes share ---------------------


def _user(role: str = "user", merchant_ids=("shop",), reseller_ids=()) -> object:
    """A UserInfo-shaped stand-in; only four fields are read."""
    return SimpleNamespace(
        role=role,
        merchant_ids=list(merchant_ids),
        reseller_ids=list(reseller_ids),
        username="someone",
    )


def _access(user: object, merchant_id: str) -> None:
    asyncio.run(
        crm_auth.assert_merchant_access(cast(Any, user), merchant_id, "onboard")
    )


def test_a_caller_may_touch_their_own_merchant() -> None:
    _access(_user(), "shop")


def test_a_caller_may_not_touch_another_merchant() -> None:
    """Fail closed on tenancy: the 403 lands before anything reads or
    writes, because merchant_id arrives in the REQUEST and a caller may hold
    several — there is no single 'current' one to infer from the token."""
    with pytest.raises(HTTPException) as caught:
        _access(_user(), "rival")
    assert caught.value.status_code == 403


def test_the_wildcard_and_admins_pass() -> None:
    """Our team still drives the pilot: admins pass, so making these routes
    merchant-facing does not take the surface away from ops."""
    _access(_user(merchant_ids=("*",), reseller_ids=("*",)), "anything")
    _access(_user(role="admin", merchant_ids=()), "anything")


def test_a_wildcard_on_merchants_covers_only_the_tokens_resellers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """merchant_ids=["*"] with reseller_ids=["R1"] is every merchant of R1.
    Read as "any merchant", it opened every merchant_scope route to that
    token for every other reseller's merchants."""
    resellers = {"r1-shop": "R1", "r2-shop": "R2"}

    async def fake_merchant(merchant_id: str) -> object:
        if merchant_id not in resellers:
            return None
        return SimpleNamespace(reseller_id=resellers[merchant_id])

    monkeypatch.setattr(crm_auth, "get_merchant_by_merchant_identifier", fake_merchant)
    r1_wide = _user(role="reseller", merchant_ids=("*",), reseller_ids=("R1",))

    _access(r1_wide, "r1-shop")
    for outside in ("r2-shop", "unknown-shop"):
        with pytest.raises(HTTPException) as caught:
            _access(r1_wide, outside)
        assert caught.value.status_code == 403
