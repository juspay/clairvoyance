"""Tests for Buddy Copilot data scope foundation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.database.accessor.breeze_buddy.template import get_template_merchant_id
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.copilot import (
    CopilotDateRangeSource,
    CopilotRequestedDateRange,
    CopilotScopeRequest,
)
from app.services.breeze_buddy.copilot.scope import (
    CopilotScopeError,
    resolve_copilot_scope,
    validate_persisted_copilot_scope_access,
)

DATA_TEMPLATE_ID = "11111111-1111-4111-8111-111111111111"
MISSING_TEMPLATE_ID = "22222222-2222-4222-8222-222222222222"


def _user(
    *,
    role: UserRole = UserRole.MERCHANT,
    merchant_ids: list[str] | None = None,
    permissions: list[str] | None = None,
) -> UserInfo:
    return UserInfo(
        id="user-1",
        username="tester",
        role=role,
        reseller_ids=["reseller-1"],
        merchant_ids=merchant_ids or ["merchant-1"],
        permissions=permissions or ["analytics:own"],
    )


def _template_merchant_loader(mapping: dict[str, str | None]):
    async def load(template_id: str) -> str | None:
        return mapping.get(template_id)

    return load


def _merchant_scope(merchant_ids: list[str] | None):
    async def resolve(_user: UserInfo):
        return merchant_ids

    return resolve


def _templates(
    *,
    data_merchant: str | None = "merchant-1",
) -> dict[str, str | None]:
    return {DATA_TEMPLATE_ID: data_merchant}


def test_resolves_authorized_scope_with_data_filters():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(
                data_merchant_id="merchant-1",
                data_template_id=DATA_TEMPLATE_ID,
                date_range=CopilotRequestedDateRange(
                    date_from=date(2026, 7, 1),
                    date_to=date(2026, 7, 27),
                ),
            ),
            _user(),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
        )
    )

    assert scope.data.data_merchant_id == "merchant-1"
    assert scope.data.data_template_id == DATA_TEMPLATE_ID
    assert scope.session_metadata()["copilot"]["data"] == {
        "data_merchant_id": "merchant-1",
        "data_template_id": DATA_TEMPLATE_ID,
    }


def test_scope_metadata_does_not_include_runtime_identity():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(data_merchant_id="merchant-1"),
            _user(),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
        )
    )

    metadata = scope.session_metadata()["copilot"]
    data_metadata = metadata["data"]

    assert "runtime_merchant_id" not in metadata
    assert "runtime_template_id" not in metadata
    assert metadata["actor"] == {"user_id": "user-1"}
    assert isinstance(data_metadata, dict)
    assert "merchant_id" not in data_metadata


def test_resolves_authenticated_user_without_analytics_permission():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(data_merchant_id="merchant-1"),
            _user(permissions=["read:own_data"]),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
        )
    )

    assert scope.data.data_merchant_id == "merchant-1"


def test_rejects_unauthorized_selected_merchant():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(data_merchant_id="merchant-2"),
                _user(merchant_ids=["merchant-1"]),
                template_merchant_loader=_template_merchant_loader(_templates()),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "unauthorized_merchant"
    assert exc.value.status_code == 403


def test_requires_selected_merchant_when_scope_is_ambiguous():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(),
                _user(
                    role=UserRole.RESELLER,
                    merchant_ids=["merchant-1", "merchant-2"],
                ),
                template_merchant_loader=_template_merchant_loader(_templates()),
                merchant_scope_resolver=_merchant_scope(["merchant-1", "merchant-2"]),
            )
        )

    assert exc.value.code == "ambiguous_merchant"


def test_auto_selects_single_authorized_merchant():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(),
            _user(merchant_ids=["merchant-1"]),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            now=datetime(2026, 7, 27, 12, 0),
        )
    )

    assert scope.data.data_merchant_id == "merchant-1"


def test_rejects_data_template_from_another_merchant():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(
                    data_merchant_id="merchant-1",
                    data_template_id=DATA_TEMPLATE_ID,
                ),
                _user(),
                template_merchant_loader=_template_merchant_loader(
                    _templates(data_merchant="merchant-2")
                ),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "unauthorized_template"
    assert exc.value.status_code == 403


def test_rejects_missing_or_unowned_data_template():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(
                    data_merchant_id="merchant-1",
                    data_template_id=MISSING_TEMPLATE_ID,
                ),
                _user(),
                template_merchant_loader=_template_merchant_loader(_templates()),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "unauthorized_template"
    assert exc.value.status_code == 403


def test_rejects_reseller_level_data_template_for_drilldown():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(
                    data_merchant_id="merchant-1",
                    data_template_id=DATA_TEMPLATE_ID,
                ),
                _user(),
                template_merchant_loader=_template_merchant_loader(
                    _templates(data_merchant=None)
                ),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "unauthorized_template"


def test_default_date_window_uses_last_seven_days_in_timezone():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(
                data_merchant_id="merchant-1",
                timezone="Asia/Kolkata",
            ),
            _user(),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            now=datetime(2026, 7, 27, 12, 0),
        )
    )

    assert scope.date_window.date_from == date(2026, 7, 21)
    assert scope.date_window.date_to == date(2026, 7, 27)
    assert scope.date_window.source == CopilotDateRangeSource.DEFAULT


def test_requested_date_range_is_preserved():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(
                data_merchant_id="merchant-1",
                date_range=CopilotRequestedDateRange(
                    date_from=date(2026, 7, 10),
                    date_to=date(2026, 7, 12),
                ),
            ),
            _user(),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
        )
    )

    assert scope.date_window.date_from == date(2026, 7, 10)
    assert scope.date_window.date_to == date(2026, 7, 12)
    assert scope.date_window.source == CopilotDateRangeSource.REQUEST


def test_malformed_timezone_is_rejected_as_scope_error():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(data_merchant_id="merchant-1", timezone="../UTC"),
                _user(),
                template_merchant_loader=_template_merchant_loader(_templates()),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "invalid_timezone"
    assert exc.value.status_code == 400


def test_invalid_date_range_is_rejected_by_schema():
    with pytest.raises(ValidationError):
        CopilotRequestedDateRange(
            date_from=date(2026, 7, 12),
            date_to=date(2026, 7, 10),
        )


def test_invalid_data_template_id_is_rejected_by_schema():
    with pytest.raises(ValidationError):
        CopilotScopeRequest(
            data_merchant_id="merchant-1",
            data_template_id="not-a-uuid",
        )


def test_unrestricted_merchant_scope_still_requires_selection():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            resolve_copilot_scope(
                CopilotScopeRequest(),
                _user(role=UserRole.ADMIN, merchant_ids=["*"]),
                template_merchant_loader=_template_merchant_loader(_templates()),
                merchant_scope_resolver=_merchant_scope(None),
            )
        )

    assert exc.value.code == "ambiguous_merchant"


def test_selected_merchant_can_use_shared_unrestricted_scope():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(data_merchant_id="merchant-2"),
            _user(role=UserRole.ADMIN, merchant_ids=["*"]),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(None),
            now=datetime(2026, 7, 27, 12, 0),
        )
    )

    assert scope.data.data_merchant_id == "merchant-2"


def test_template_accessor_reads_only_template_merchant(monkeypatch):
    captured: dict[str, object] = {}

    async def run_query(query: str, values: list[object]):
        captured["query"] = query
        captured["values"] = values
        return [{"merchant_id": "merchant-1"}]

    monkeypatch.setattr(
        "app.database.accessor.breeze_buddy.template.run_parameterized_query",
        run_query,
    )

    merchant_id = asyncio.run(get_template_merchant_id("template-1"))

    assert merchant_id == "merchant-1"
    assert captured["values"] == ["template-1"]
    assert "SELECT merchant_id" in str(captured["query"])
    assert "flow" not in str(captured["query"])
    assert "secrets" not in str(captured["query"])


def test_persisted_scope_access_allows_current_data_merchant():
    scope = asyncio.run(
        resolve_copilot_scope(
            CopilotScopeRequest(
                data_merchant_id="merchant-1",
                data_template_id=DATA_TEMPLATE_ID,
            ),
            _user(),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
        )
    )

    asyncio.run(
        validate_persisted_copilot_scope_access(
            scope.session_metadata(),
            _user(merchant_ids=["merchant-1"]),
            template_merchant_loader=_template_merchant_loader(_templates()),
            merchant_scope_resolver=_merchant_scope(["merchant-1"]),
        )
    )


def test_persisted_scope_access_noops_for_ordinary_chat_metadata():
    asyncio.run(
        validate_persisted_copilot_scope_access(
            {"template_vars": {}},
            _user(merchant_ids=[]),
            template_merchant_loader=_template_merchant_loader({}),
            merchant_scope_resolver=_merchant_scope([]),
        )
    )


def test_persisted_scope_access_rejects_unauthorized_data_merchant():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            validate_persisted_copilot_scope_access(
                {"copilot": {"data": {"data_merchant_id": "merchant-2"}}},
                _user(merchant_ids=["merchant-1"]),
                template_merchant_loader=_template_merchant_loader({}),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "unauthorized_merchant"
    assert exc.value.status_code == 404


def test_persisted_scope_access_rejects_stale_data_template_mapping():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            validate_persisted_copilot_scope_access(
                {
                    "copilot": {
                        "data": {
                            "data_merchant_id": "merchant-1",
                            "data_template_id": DATA_TEMPLATE_ID,
                        }
                    }
                },
                _user(merchant_ids=["merchant-1"]),
                template_merchant_loader=_template_merchant_loader(
                    _templates(data_merchant="merchant-2")
                ),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "unauthorized_template"
    assert exc.value.status_code == 404


def test_persisted_scope_access_rejects_malformed_copilot_metadata():
    with pytest.raises(CopilotScopeError) as exc:
        asyncio.run(
            validate_persisted_copilot_scope_access(
                {"copilot": {"data": {"data_template_id": DATA_TEMPLATE_ID}}},
                _user(merchant_ids=["merchant-1"]),
                template_merchant_loader=_template_merchant_loader({}),
                merchant_scope_resolver=_merchant_scope(["merchant-1"]),
            )
        )

    assert exc.value.code == "invalid_persisted_scope"
    assert exc.value.status_code == 404
