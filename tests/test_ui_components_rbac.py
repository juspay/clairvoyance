"""ui_component CRUD: merchant-scoped listing and PATCH-style update.

Two review findings, both about the boundary between the route handler
and the query builder:

1. Listing must scope by the caller's MERCHANTS as well as resellers —
   both allowlists reach the SQL, which filters before pagination and
   the total. Reseller-wide rows (NULL merchant) remain visible to any
   user with reseller access, as templates do.
2. Update distinguishes "field omitted" (untouched) from "field sent as
   null" (cleared) via Pydantic field presence. NOT NULL columns refuse an
   explicit null with a 400.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.ui_components.handlers as handlers
from app.database.queries.breeze_buddy.ui_component import (
    list_ui_components_query,
    update_ui_component_query,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.ui_component import (
    UiComponentResponse,
    UiComponentUpdate,
)

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}


def _user(role="reseller", resellers=("r1",), merchants=("m1",)) -> UserInfo:
    return UserInfo(
        id="u1",
        username="u1",
        role=role,
        reseller_ids=list(resellers),
        merchant_ids=list(merchants),
    )


# ---------------------------------------------------------------------------
# list: merchant allowlist reaches the query
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_list(monkeypatch) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}

    async def fake_list(**kwargs):
        seen.update(kwargs)
        return [], 0

    monkeypatch.setattr(handlers, "list_ui_components", fake_list)
    return seen


async def _list(user: UserInfo, **overrides):
    kwargs = dict(
        reseller_id=None,
        merchant_id=None,
        include_inactive=False,
        page=1,
        limit=50,
        current_user=user,
    )
    kwargs.update(overrides)
    return await handlers.list_ui_components_handler(**kwargs)


@pytest.mark.asyncio
async def test_list_scopes_non_admin_to_their_merchants(captured_list):
    await _list(_user())
    assert captured_list["reseller_ids"] == ["r1"]
    assert captured_list["merchant_ids"] == ["m1"]


@pytest.mark.asyncio
async def test_list_refuses_merchant_outside_scope(captured_list):
    with pytest.raises(HTTPException) as exc:
        await _list(_user(), merchant_id="m2")
    assert exc.value.status_code == 403
    assert captured_list == {}  # never reached the accessor


@pytest.mark.asyncio
async def test_list_passes_explicit_merchant_inside_scope(captured_list):
    await _list(_user(merchants=("m1", "m2")), merchant_id="m2")
    assert captured_list["merchant_id"] == "m2"
    assert captured_list["merchant_ids"] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_list_wildcard_and_admin_skip_merchant_filter(captured_list):
    await _list(_user(merchants=("*",)))
    assert captured_list["merchant_ids"] is None
    assert captured_list["reseller_ids"] == ["r1"]
    captured_list.clear()
    await _list(_user(role="admin", resellers=(), merchants=()))
    assert captured_list["merchant_ids"] is None
    assert captured_list["reseller_ids"] is None


def test_list_query_admits_reseller_wide_rows_for_merchant_allowlist():
    query, count_query, params = list_ui_components_query(
        reseller_ids=["r1"], merchant_ids=["m1"]
    )
    clause = "(merchant_id IS NULL OR merchant_id = ANY($2))"
    assert clause in query and clause in count_query
    assert params[:2] == [["r1"], ["m1"]]
    # an explicit merchant_id wins over the allowlist
    query, _, params = list_ui_components_query(merchant_id="m1", merchant_ids=["m1"])
    assert "merchant_id = $1" in query and "ANY" not in query


# ---------------------------------------------------------------------------
# update: presence semantics
# ---------------------------------------------------------------------------


def _row(**overrides) -> UiComponentResponse:
    base: Dict[str, Any] = dict(
        id="11111111-1111-1111-1111-111111111111",
        reseller_id="r1",
        merchant_id="m1",
        name="JourneyOptions",
        version=3,
        props_schema=SCHEMA,
        flags={"data_bound": True},
        render_def={"type": "col", "children": [{"type": "text"}]},
        prompt_hint="hint",
    )
    base.update(overrides)
    return UiComponentResponse(**base)


@pytest.fixture
def captured_update(monkeypatch) -> List[Any]:
    calls: List[Any] = []

    async def fake_get(ui_component_id):
        return _row()

    async def fake_update(ui_component_id, patch):
        calls.append(patch)
        return _row(**{k: v for k, v in patch.items()})

    monkeypatch.setattr(handlers, "get_ui_component_by_id", fake_get)
    monkeypatch.setattr(handlers, "update_ui_component", fake_update)
    return calls


@pytest.mark.asyncio
async def test_update_forwards_only_supplied_fields(captured_update):
    body = UiComponentUpdate.model_validate({"prompt_hint": "new"})
    await handlers.update_ui_component_handler(_row().id, body, _user())
    assert captured_update == [{"prompt_hint": "new"}]


@pytest.mark.asyncio
async def test_update_explicit_null_clears_nullable_fields(captured_update):
    body = UiComponentUpdate.model_validate({"render_def": None, "prompt_hint": None})
    await handlers.update_ui_component_handler(_row().id, body, _user())
    assert captured_update == [{"render_def": None, "prompt_hint": None}]


@pytest.mark.asyncio
async def test_update_explicit_null_refused_on_not_null_columns(captured_update):
    body = UiComponentUpdate.model_validate({"props_schema": None, "is_active": None})
    with pytest.raises(HTTPException) as exc:
        await handlers.update_ui_component_handler(_row().id, body, _user())
    assert exc.value.status_code == 400
    assert "props_schema" in exc.value.detail and "is_active" in exc.value.detail
    assert captured_update == []


@pytest.mark.asyncio
async def test_update_reruns_guards_on_merged_row(captured_update, monkeypatch):
    # clearing render_def on an overlay_only def is refused, as at create
    async def fake_get(ui_component_id):
        return _row(flags={"data_bound": True, "overlay_only": True})

    monkeypatch.setattr(handlers, "get_ui_component_by_id", fake_get)
    body = UiComponentUpdate.model_validate({"render_def": None})
    with pytest.raises(HTTPException) as exc:
        await handlers.update_ui_component_handler(_row().id, body, _user())
    assert exc.value.status_code == 400
    assert captured_update == []


def test_update_query_writes_null_and_ignores_unknown_keys():
    query, params = update_ui_component_query(
        ui_component_id="id-1",
        patch={"render_def": None, "prompt_hint": "p", "not_a_column": "x"},
    )
    assert "render_def = $1::jsonb" in query and "prompt_hint = $2" in query
    assert "not_a_column" not in query
    assert "version = version + 1" in query
    assert params[:2] == [None, "p"] and params[-1] == "id-1"
    assert update_ui_component_query(ui_component_id="id-1", patch={}) == ("", [])
    assert update_ui_component_query(
        ui_component_id="id-1", patch={"not_a_column": 1}
    ) == ("", [])
