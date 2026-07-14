"""Regression test for a live-discovered RBAC bug: validate_kb_access (used
by GET/query/list/download) had no reseller-role bypass, while
require_kb_write_access (used by upload/connect/delete) already did. A
reseller-role user could write to any KB under their own reseller but
could not even read it — backwards from intent, confirmed live via a
minted reseller-scoped token against a real merchant-owned KB.
"""

from types import SimpleNamespace
from typing import List, Optional, cast

import pytest
from fastapi import HTTPException

from app.api.routers.breeze_buddy.knowledge_base.rbac import validate_kb_access
from app.schemas import UserInfo


def _reseller_user(
    reseller_ids: List[str], merchant_ids: Optional[List[str]] = None
) -> UserInfo:
    return cast(
        UserInfo,
        SimpleNamespace(
            username="e2e-reseller",
            role="reseller",
            reseller_ids=reseller_ids,
            merchant_ids=merchant_ids or [],
        ),
    )


def test_reseller_can_read_a_merchant_owned_kb_under_their_own_reseller():
    """The exact scenario that failed live: reseller owns 'purvanchal-reseller',
    KB belongs to merchant 'prayagraj_admin' under that reseller, reseller's
    own merchant_ids is empty (correct for a pure reseller-role token)."""
    user = _reseller_user(reseller_ids=["purvanchal-reseller"], merchant_ids=[])
    # Must not raise.
    validate_kb_access(user, "purvanchal-reseller", "prayagraj_admin", "read")


def test_reseller_can_read_a_sibling_merchants_kb_under_same_reseller():
    user = _reseller_user(reseller_ids=["purvanchal-reseller"], merchant_ids=[])
    validate_kb_access(user, "purvanchal-reseller", "mirzapur", "read")


def test_reseller_can_read_a_reseller_level_kb_with_no_merchant():
    user = _reseller_user(reseller_ids=["purvanchal-reseller"], merchant_ids=[])
    validate_kb_access(user, "purvanchal-reseller", None, "read")


def test_reseller_still_blocked_from_a_different_reseller():
    user = _reseller_user(reseller_ids=["purvanchal-reseller"], merchant_ids=[])
    with pytest.raises(HTTPException) as exc_info:
        validate_kb_access(user, "some-other-reseller", "some-merchant", "read")
    assert exc_info.value.status_code == 403


def test_merchant_role_still_blocked_from_sibling_merchant_kb():
    """Non-reseller roles must NOT get this bypass — merchant-role users stay
    scoped to their own merchant, exactly as before this fix."""
    user = cast(
        UserInfo,
        SimpleNamespace(
            username="e2e-merchant",
            role="merchant",
            reseller_ids=["purvanchal-reseller"],
            merchant_ids=["mirzapur"],
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        validate_kb_access(user, "purvanchal-reseller", "prayagraj_admin", "read")
    assert exc_info.value.status_code == 403


def test_admin_still_bypasses_everything():
    user = cast(
        UserInfo,
        SimpleNamespace(
            username="e2e-admin", role="admin", reseller_ids=[], merchant_ids=[]
        ),
    )
    validate_kb_access(user, "any-reseller", "any-merchant", "read")
