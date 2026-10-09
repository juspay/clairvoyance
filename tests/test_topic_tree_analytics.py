"""Topic tree analytics: the breakdown filters, the dims SQL and the
Segment menu (DB-free)."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.database.queries.breeze_buddy.analytics.evaluation_result import (
    get_topic_tree_query,
)
from app.schemas import AnalyticsFilters, UserInfo, UserRole


def test_dims_filter_normalizes_values_and_allows_two_keys():
    filters = AnalyticsFilters(
        dims={"lender_name": [" dmi ", "tvs credit", ""], "event_name": ["OFFERED"]}
    )
    assert filters.dims == {
        "lender_name": ["DMI", "TVS_CREDIT"],
        "event_name": ["OFFERED"],
    }


@pytest.mark.parametrize(
    "filters",
    [
        {"dims": {"a": ["x"], "b": ["y"], "c": ["z"]}},
        {"dims": {"lender name": ["DMI"]}},
        {"dims": {"lender_name": ["  "]}},
        {"template_ids": ["not-a-uuid"]},
    ],
)
def test_bad_breakdown_filters_are_rejected(filters):
    with pytest.raises(ValidationError):
        AnalyticsFilters(**filters)


def test_dims_and_template_ids_are_bound_parameters():
    query, values = get_topic_tree_query(
        {
            "date_from": date(2026, 1, 10),
            "date_to": date(2026, 1, 11),
            "template_ids": ["c77102ad-1067-427c-80aa-559538073aee"],
            "dims": {"lender_name": ["DMI"]},
        }
    )
    assert "ca.template_id = ANY($1::uuid[])" in query
    assert "ca.metadata -> 'dims' -> $2::text" in query
    assert "= ANY($3::text[])" in query
    assert "'MORE_THAN_ONE'" in query
    assert values[:3] == [
        ["c77102ad-1067-427c-80aa-559538073aee"],
        "lender_name",
        ["DMI"],
    ]
    assert values[3:] == [date(2026, 1, 8), date(2026, 1, 11)]
    assert f"${len(values)}" in query
    assert "DMI" not in query


async def test_breakdowns_list_each_field_with_its_values_busiest_first(monkeypatch):
    from app.api.routers.breeze_buddy.analytics import handlers

    async def labels(filters):
        assert "dims" not in filters
        return {"lender_name": "Lender"}

    async def rows(filters):
        return [
            {"key": "lender_name", "value": "DMI", "calls": 7},
            {"key": "lender_name", "value": "MORE_THAN_ONE", "calls": 9},
            {"key": "outcome", "value": "BUSY", "calls": 20},
        ]

    monkeypatch.setattr(handlers, "get_topic_breakdown_labels", labels)
    monkeypatch.setattr(handlers, "get_topic_dim_rows", rows)
    response = await handlers.get_topic_breakdowns_analytics(
        {
            "date_from": date(2026, 10, 8),
            "date_to": date(2026, 10, 8),
            "dims": {"lender_name": ["DMI"]},
        },
        {},
        UserInfo(id="admin-1", username="admin", role=UserRole.ADMIN),
    )
    assert response["breakdowns"] == [
        {
            "key": "outcome",
            "label": "Outcome",
            "calls": 20,
            "values": [{"value": "BUSY", "calls": 20}],
        },
        {
            "key": "lender_name",
            "label": "Lender",
            "calls": 16,
            "values": [
                {"value": "MORE_THAN_ONE", "calls": 9},
                {"value": "DMI", "calls": 7},
            ],
        },
    ]


async def test_breakdown_labels_take_the_first_agents_label(monkeypatch):
    from app.database.accessor.breeze_buddy.analytics import evaluation_result

    async def rows(query, values):
        return [
            {"breakdowns": {"lender_name": "Lender"}},
            {"breakdowns": '{"lender_name": "Bank", "event_name": "Stage"}'},
        ]

    monkeypatch.setattr(evaluation_result, "run_reader_query", rows)
    labels = await evaluation_result.get_topic_breakdown_labels({})
    assert labels == {"lender_name": "Lender", "event_name": "Stage"}
