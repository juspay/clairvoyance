import asyncio
import json
import os
from datetime import datetime, timezone, tzinfo
from typing import Optional
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics import (
    nightly,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics.extractor import (
    normalize_topics,
    topic_labels_to_catalog,
)
from app.database.accessor.breeze_buddy.analytics import evaluation_result
from app.database.queries.breeze_buddy.evaluation_config import (
    add_discovered_topics_query,
    grow_topic_tree_query,
    replace_topic_tree_query,
)
from app.schemas.breeze_buddy.analytics import AnalyticsFilters

TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"
TREE = ["KYC.Selfie fails", "KYC.Other", "Payment.Card fails", "Payment.Other"]
CONFIG = {"provider": "openrouter", "model": "qwen/qwen3.7-flash", "system_prompt": "P"}
DAY = (
    datetime(2026, 10, 23, tzinfo=timezone.utc),
    datetime(2026, 10, 24, tzinfo=timezone.utc),
)


def _topic(label: str, new_label: object = "") -> dict:
    return {
        "type": label,
        "label": label,
        "phrase": "p",
        "evidence_turns": [1],
        "new_label": new_label,
    }


def test_an_off_list_label_lands_in_other_with_its_name_kept() -> None:
    topics = normalize_topics(
        {
            "topics": [
                _topic("KYC.Video KYC fails"),
                _topic("Delivery.Late"),
                _topic("Payment.Card fails", "ignored"),
                _topic("Payment.Other", "Wallet refused"),
            ]
        },
        max_topics=None,
        existing_topics=topic_labels_to_catalog(TREE),
    )
    assert [(t["label"], t.get("new_label")) for t in topics] == [
        ("KYC.Other", "KYC.Video KYC fails"),
        ("Other", "Delivery.Late"),
        ("Payment.Card fails", None),
    ]


def test_a_null_or_long_new_label_does_not_fail_the_answer() -> None:
    topics = normalize_topics(
        {"topics": [_topic("KYC.Other", None), _topic("Payment.Other", "x" * 300)]},
        max_topics=None,
        existing_topics=topic_labels_to_catalog(TREE),
    )
    assert [(t["label"], len(t.get("new_label") or "")) for t in topics] == [
        ("KYC.Other", 0),
        ("Payment.Other", 120),
    ]


async def test_the_night_job_adds_only_groups_of_at_least_one_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "label": "Other",
            "new_label": "gift wrap",
            "calls": 8,
            "phrase": "p",
            "topic_calls": 1000,
        },
        {
            "label": "Other",
            "new_label": "gift wrapping",
            "calls": 3,
            "phrase": "p",
            "topic_calls": 1000,
        },
        {
            "label": "KYC.Other",
            "new_label": "random thing",
            "calls": 2,
            "phrase": "p",
            "topic_calls": 1000,
        },
        {
            "label": "KYC.Selfie fails",
            "new_label": "",
            "calls": 900,
            "phrase": "p",
            "topic_calls": 1000,
        },
    ]
    answer = {
        "add": [
            {
                "label": "Delivery.Gift wrap",
                "description": "d",
                "new_labels": ["gift wrap", "gift wrapping"],
            },
            {
                "label": "KYC.Random thing",
                "description": "d",
                "new_labels": ["random thing"],
            },
            {
                "label": "KYC.Selfie fails",
                "description": "d",
                "new_labels": ["gift wrap"],
            },
        ]
    }
    monkeypatch.setattr(nightly, "get_topic_day", AsyncMock(return_value=rows))
    monkeypatch.setattr(nightly, "_request_llm", AsyncMock(return_value=answer))
    grow = AsyncMock()
    monkeypatch.setattr(nightly, "grow_topic_tree", grow)

    await nightly.update_topic_list(
        {"template_id": TEMPLATE_ID, "topics": TREE, "configuration": CONFIG}, *DAY
    )

    grow.assert_awaited_once_with(
        TEMPLATE_ID,
        ["Delivery.Gift wrap", "Delivery.Other"],
        "\n  Delivery.Gift wrap: d",
    )


async def test_one_or_two_calls_never_add_a_label_on_a_small_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "label": "Other",
            "new_label": "gift wrap",
            "calls": 2,
            "phrase": "p",
            "topic_calls": 40,
        }
    ]
    answer = {
        "add": [
            {
                "label": "Delivery.Gift wrap",
                "description": "d",
                "new_labels": ["gift wrap"],
            }
        ]
    }
    monkeypatch.setattr(nightly, "get_topic_day", AsyncMock(return_value=rows))
    monkeypatch.setattr(nightly, "_request_llm", AsyncMock(return_value=answer))
    grow = AsyncMock()
    monkeypatch.setattr(nightly, "grow_topic_tree", grow)

    await nightly.update_topic_list(
        {"template_id": TEMPLATE_ID, "topics": TREE, "configuration": CONFIG}, *DAY
    )

    grow.assert_not_awaited()


async def test_a_mixed_list_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "label": "price high",
            "new_label": "",
            "calls": 90,
            "phrase": "p",
            "topic_calls": 90,
        }
    ]
    monkeypatch.setattr(nightly, "get_topic_day", AsyncMock(return_value=rows))
    model = AsyncMock()
    monkeypatch.setattr(nightly, "_request_llm", model)

    await nightly.update_topic_list(
        {
            "template_id": TEMPLATE_ID,
            "topics": TREE + ["price high"],
            "configuration": CONFIG,
        },
        *DAY,
    )

    model.assert_not_awaited()


async def test_a_flat_day_becomes_a_two_level_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "label": "price high",
            "new_label": "",
            "calls": 60,
            "phrase": "p",
            "topic_calls": 60,
        }
    ]
    answer = {
        "about": "A",
        "topics": [
            {
                "label": "Price",
                "description": "d",
                "subtopics": [{"label": "Too high", "description": "d"}],
            }
        ],
    }
    monkeypatch.setattr(nightly, "get_topic_day", AsyncMock(return_value=rows))
    monkeypatch.setattr(nightly, "_request_llm", AsyncMock(return_value=answer))
    replace = AsyncMock()
    monkeypatch.setattr(nightly, "replace_topic_tree", replace)

    await nightly.update_topic_list(
        {"template_id": TEMPLATE_ID, "topics": ["price high"], "configuration": CONFIG},
        *DAY,
    )

    template_id, labels, prompt = replace.await_args_list[0].args
    assert (template_id, labels) == (TEMPLATE_ID, ["Price.Too high", "Price.Other"])
    assert "{accepted_topics}" in prompt
    assert "  Price.Too high: d" in prompt


def test_other_can_be_opened_with_thousands_of_topic_types() -> None:
    filters = AnalyticsFilters(
        topic_type="__other__", topic_types=[f"t{i}" for i in range(2920)]
    )
    assert len(filters.topic_types or []) == 2920


def test_rows_saved_as_other_stay_in_the_other_bucket() -> None:
    when = datetime(2026, 10, 23, tzinfo=timezone.utc)
    rows = [
        {
            "template_id": TEMPLATE_ID,
            "template_name": "t",
            "source_id": f"s{i}",
            "started_at": when,
            "raw_topic_type": topic_type,
            "raw_label": label,
        }
        for i, (topic_type, label) in enumerate(
            [("other", "Other")] * 5 + [("kyc_selfie_fails", "KYC.Selfie fails")]
        )
    ]
    counts = evaluation_result._topic_counts(rows, when.date())
    assert evaluation_result._mapped_topic(rows[0], counts, 10)[3] is True
    assert evaluation_result._mapped_topic(rows[-1], counts, 10)[:3] == (
        "kyc_selfie_fails",
        "KYC.Selfie fails",
        1,
    )


async def test_list_writes_keep_a_tree_two_level_and_merge_in_sql() -> None:
    dsn = os.environ.get("TOPICS_SQL_TEST_DSN")
    if not dsn:
        pytest.skip("set TOPICS_SQL_TEST_DSN to a throwaway Postgres to run the SQL")
    conn = await asyncpg.connect(dsn)

    async def run(query_and_values: tuple) -> list:
        query, values = query_and_values
        return await conn.fetch(query, *values)

    try:
        await conn.execute(
            "CREATE TEMP TABLE evaluation_config (id uuid DEFAULT gen_random_uuid(),"
            " template_id uuid, evaluation_type text, name text, enabled boolean,"
            " topics text[], configuration jsonb)"
        )
        tree_id, flat_id = TEMPLATE_ID, "00000000-0000-0000-0000-000000000002"
        for template_id, topics in ((tree_id, TREE), (flat_id, ["price high"])):
            await conn.execute(
                "INSERT INTO evaluation_config VALUES (DEFAULT, $1, 'TOPIC', 'topic',"
                " true, $2, $3)",
                template_id,
                topics,
                json.dumps({"system_prompt": "P", "settings": {"temperature": 0}}),
            )

        await run(add_discovered_topics_query(tree_id, ["late delivery", "KYC.New"]))
        assert await conn.fetchval(
            "SELECT topics FROM evaluation_config WHERE template_id = $1", tree_id
        ) == TREE + ["KYC.New"]

        assert not await run(replace_topic_tree_query(tree_id, ["A.B"], "Q"))
        await run(replace_topic_tree_query(flat_id, ["Price.Too high"], "Q"))
        row = await conn.fetchrow(
            "SELECT topics, configuration FROM evaluation_config WHERE template_id = $1",
            flat_id,
        )
        assert row["topics"] == ["Price.Too high"]
        assert json.loads(row["configuration"]) == {
            "system_prompt": "Q",
            "settings": {"temperature": 0, "max_topics": 1},
        }

        for line in ("\n  Delivery.Gift wrap: d", "\n  Delivery.Late: d"):
            await run(
                grow_topic_tree_query(
                    tree_id, ["Delivery.Gift wrap", "Delivery.Other"], line
                )
            )
        row = await conn.fetchrow(
            "SELECT topics, configuration FROM evaluation_config WHERE template_id = $1",
            tree_id,
        )
        assert row["topics"][-2:] == ["Delivery.Gift wrap", "Delivery.Other"]
        assert json.loads(row["configuration"])["system_prompt"] == (
            "P\n\nNEWER TOPICS\n  Delivery.Gift wrap: d\n  Delivery.Late: d"
        )
    finally:
        await conn.close()


async def test_the_scheduler_tick_returns_while_the_night_work_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TwoAm(datetime):
        @classmethod
        def now(cls, tz: Optional[tzinfo] = None) -> "TwoAm":
            return cls(2026, 10, 24, 2, 5, tzinfo=nightly.IST)

    release = asyncio.Event()
    started = []

    async def work(start: datetime, end: datetime) -> None:
        started.append((start, end))
        await release.wait()

    monkeypatch.setattr(nightly, "datetime", TwoAm)
    monkeypatch.setattr(nightly, "_update_all", work)

    await nightly.run_topic_night_job()
    await nightly.run_topic_night_job()
    await asyncio.sleep(0)

    assert started == [
        (
            datetime(2026, 10, 23, tzinfo=nightly.IST),
            datetime(2026, 10, 24, tzinfo=nightly.IST),
        )
    ]
    task = next(iter(nightly._running))
    release.set()
    await task
    await asyncio.sleep(0)
    assert not nightly._running
