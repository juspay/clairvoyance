"""Topic tree analytics: the breakdown filters, the dims SQL, the
most-affected finder and the per-topic split (DB-free)."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.database.accessor.breeze_buddy.analytics.evaluation_result import (
    dim_split,
    most_affected,
)
from app.database.queries.breeze_buddy.analytics.evaluation_result import (
    get_topic_tree_query,
)
from app.schemas import AnalyticsFilters


def _dim_rows(segments, cells):
    """Rows as get_topic_dim_counts_query returns them: calls per key,
    value and topic id, each call in one value per key. Calls of a value
    outside the given cells land in no_topic.no_issue, so each value's
    total is its segment size."""
    cells = dict(cells)
    for (key, value), total in segments.items():
        given = sum(n for (k, v, _), n in cells.items() if (k, v) == (key, value))
        cells[(key, value, "no_topic.no_issue")] = total - given
    return [
        {"key": k, "value": v, "topic_type": t, "calls": n}
        for (k, v, t), n in cells.items()
    ]


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


def test_most_affected_finds_the_value_a_topic_concentrates_in():
    rows = _dim_rows(
        segments={
            ("event_name", "KYC_COMPLETED"): 600,
            ("event_name", "OFFERED"): 1900,
        },
        cells={
            ("event_name", "KYC_COMPLETED", "autopay.not_set"): 105,
            ("event_name", "OFFERED", "autopay.not_set"): 15,
        },
    )
    finding = most_affected(rows, set())["autopay.not_set"]
    assert finding["value"] == "KYC_COMPLETED"
    assert finding["share"] == 17.5
    assert finding["rest_share"] == round(15 * 100 / 1900, 2)
    assert finding["z"] >= 3


def test_most_affected_skips_small_cells_filtered_keys_no_topic_and_multiple():
    rows = _dim_rows(
        segments={
            ("event_name", "KYC_COMPLETED"): 150,
            ("event_name", "OFFERED"): 2350,
            ("lender_name", "DMI"): 1000,
            ("lender_name", "TVS_CREDIT"): 1300,
            ("lender_name", "MORE_THAN_ONE"): 200,
            ("stage", "X"): 1000,
            ("stage", "Y"): 1500,
        },
        cells={
            ("event_name", "KYC_COMPLETED", "autopay.not_set"): 29,
            ("event_name", "OFFERED", "autopay.not_set"): 1,
            ("lender_name", "DMI", "kyc.selfie_fails"): 200,
            ("lender_name", "TVS_CREDIT", "kyc.selfie_fails"): 10,
            ("lender_name", "MORE_THAN_ONE", "cart.out_of_stock"): 150,
            ("lender_name", "DMI", "cart.out_of_stock"): 5,
            ("stage", "X", "no_topic.no_detail"): 400,
            ("stage", "Y", "no_topic.no_detail"): 50,
        },
    )
    assert most_affected(rows, {"lender_name"}) == {}
    assert "cart.out_of_stock" not in most_affected(rows, set())


def test_most_affected_keeps_the_strongest_value_per_topic():
    rows = _dim_rows(
        segments={
            ("lender_name", "DMI"): 1000,
            ("lender_name", "TVS_CREDIT"): 1500,
            ("event_name", "KYC_COMPLETED"): 600,
            ("event_name", "OFFERED"): 1900,
        },
        cells={
            ("lender_name", "DMI", "autopay.not_set"): 100,
            ("lender_name", "TVS_CREDIT", "autopay.not_set"): 20,
            ("event_name", "KYC_COMPLETED", "autopay.not_set"): 105,
            ("event_name", "OFFERED", "autopay.not_set"): 15,
        },
    )
    best = most_affected(rows, set())
    assert list(best) == ["autopay.not_set"]
    assert best["autopay.not_set"]["key"] == "event_name"


def test_split_lists_each_keys_values_largest_first_and_adds_up():
    rows = _dim_rows(
        segments={("lender_name", "DMI"): 30, ("lender_name", "MORE_THAN_ONE"): 5},
        cells={
            ("lender_name", "DMI", "kyc.selfie_fails"): 7,
            ("lender_name", "MORE_THAN_ONE", "kyc.selfie_fails"): 9,
        },
    )
    split = dim_split(rows)["kyc.selfie_fails"]["lender_name"]
    assert split == [
        {"value": "MORE_THAN_ONE", "calls": 9},
        {"value": "DMI", "calls": 7},
    ]
