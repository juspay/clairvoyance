"""Two-level topics: the catalog's shape picks the path, the model's answer is
forced into the catalog, one call becomes one row."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics import (
    catalog,
    evaluator,
    extractor,
)
from app.database.queries.breeze_buddy.evaluation_config import (
    add_discovered_topics_query,
)
from app.database.queries.breeze_buddy.evaluation_result import (
    replace_topic_result_query,
)
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

ENTRIES = [
    "KYC.Location error",
    "KYC.Selfie fail",
    "Mandate / autopay.Limit exceeded",
    "Mandate / autopay.Autopay not created",
]
DETAILS = {
    "kyc": {"description": "The lender's KYC step"},
    "kyc.location_error": {
        "description": "Turn on location keeps appearing",
        "exclude": "camera permission",
    },
}
CONFIG = {
    "model": "m",
    "system_prompt": "Flipkart EMI recovery calls. {accepted_topics}",
    "topic_details": DETAILS,
    "breakdowns": {"lender_name": "Lender", "event_name": "Stage"},
}
TRANSCRIPT = [
    {"role": "assistant", "content": "Autopay set kijiye"},
    {"role": "user", "content": "autopay pe bola sorry your limit has exceeded"},
]


def test_catalog_shape_decides_the_path() -> None:
    assert catalog.split_entry("KYC.Location error") == ("kyc", "location_error")
    assert catalog.split_entry("too many calls") is None
    assert catalog.split_entry("a.b.c") is None
    assert catalog.split_entry("no_topic.x") is None
    assert catalog.is_two_level(ENTRIES)
    assert not catalog.is_two_level(["autopay setup failing", "KYC.Selfie"])
    assert not catalog.is_two_level([])
    assert catalog.catalog_problems(ENTRIES, ["Cart.Product missing"]) == []
    assert catalog.catalog_problems(ENTRIES, ["too many calls"])
    assert catalog.catalog_problems(["too many calls"], ["Cart.Product missing"])
    assert catalog.catalog_problems([], ["a.b.c"])
    assert catalog.catalog_problems([], ["KYC.Selfie fail", "too many calls"])


def test_catalog_adds_other_and_fixed_buckets_in_prompt_order() -> None:
    built = catalog.build_catalog(ENTRIES, DETAILS)
    assert list(built) == [
        "kyc.location_error",
        "kyc.selfie_fail",
        "kyc.other",
        "mandate_autopay.limit_exceeded",
        "mandate_autopay.autopay_not_created",
        "mandate_autopay.other",
        "no_topic.no_issue",
        "no_topic.no_detail",
        "no_topic.other",
    ]
    assert built["kyc.location_error"]["topic_label"] == "KYC"
    assert built["kyc.location_error"]["label"] == "Location error"
    rendered = catalog.render_catalog(built)
    assert "KYC (kyc): The lender's KYC step" in rendered
    assert "excludes: camera permission" in rendered
    assert "no_topic.no_detail" in rendered


def test_model_answer_is_forced_into_the_catalog() -> None:
    built = catalog.build_catalog(ENTRIES, DETAILS)
    base = {
        "phrase": "x",
        "phrase_en": "x in English",
        "screen_text": " Turn on location access ",
        "evidence_turns": [1],
        "summary": "s",
    }

    good = catalog.resolve_answer(
        {**base, "primary": "Mandate_Autopay.Limit_Exceeded", "proposed": "junk"}, built
    )
    assert good["type"] == "mandate_autopay.limit_exceeded"
    assert good["topic"] == "mandate_autopay"
    assert good["proposed"] is None
    assert good["phrase_en"] == "x in English"
    assert good["screen_text"] == "Turn on location access"

    unknown_sub = catalog.resolve_answer(
        {**base, "primary": "kyc.pan_failed", "proposed": "PAN failed"}, built
    )
    assert unknown_sub["type"] == "kyc.other"
    assert unknown_sub["proposed"] == "PAN failed"

    unknown_parent = catalog.resolve_answer({**base, "primary": "delivery.late"}, built)
    assert unknown_parent["type"] == "no_topic.other"
    bare = catalog.resolve_answer({"primary": "kyc.location_error"}, built)
    assert bare["phrase_en"] == "" and bare["screen_text"] == ""

    secondary = catalog.resolve_answer(
        {
            **base,
            "primary": "kyc.selfie_fail",
            "secondary": [
                "kyc.selfie_fail",
                "kyc.location_error",
                "bogus",
                "kyc.other",
            ],
        },
        built,
    )
    assert secondary["secondary"] == ["kyc.location_error", "kyc.other"]

    scalar = catalog.resolve_answer(
        {**base, "primary": "kyc.other", "secondary": 5, "evidence_turns": 4}, built
    )
    assert scalar["secondary"] == [] and scalar["evidence_turns"] == []


def test_topic_details_keys_match_ids_in_any_spelling() -> None:
    resolved = extractor.resolve_topic_evaluation_configuration(
        {
            **CONFIG,
            "topic_details": {
                "KYC.Location error": {"description": "d"},
                "Mandate / autopay": {"description": "m"},
            },
        }
    )
    assert set(resolved["topic_details"]) == {"kyc.location_error", "mandate_autopay"}


def test_funnel_is_an_ordered_list_of_topic_ids() -> None:
    resolved = extractor.resolve_topic_evaluation_configuration(
        {**CONFIG, "funnel": ["Flipkart app", "Cart", "KYC", "kyc", " "]}
    )
    assert resolved["funnel"] == ["flipkart_app", "cart", "kyc"]
    assert extractor.resolve_topic_evaluation_configuration(CONFIG)["funnel"] == []
    with pytest.raises(ValueError, match="at most 20"):
        extractor.resolve_topic_evaluation_configuration(
            {**CONFIG, "funnel": [f"step {n}" for n in range(21)]}
        )


def test_breakdowns_are_copied_and_cleaned_from_the_payload() -> None:
    dims = catalog.normalize_dims(
        {"lender_name": "TVS CREDIT, DMI", "event_name": "KYC_COMPLETED", "x": ""},
        {"lender_name": "Lender", "event_name": "Stage", "x": "X", "city": "City"},
    )
    assert dims == {
        "lender_name": ["DMI", "TVS_CREDIT"],
        "event_name": ["KYC_COMPLETED"],
    }
    with pytest.raises(ValueError, match="at most 2"):
        extractor.resolve_topic_evaluation_configuration(
            {**CONFIG, "breakdowns": {"a": "", "b": "", "c": ""}}
        )
    with pytest.raises(ValueError, match="payload key"):
        extractor.resolve_topic_evaluation_configuration(
            {**CONFIG, "breakdowns": {"lender name": ""}}
        )


async def test_classify_builds_rules_catalog_then_agent_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock(
        return_value={
            "primary": "mandate_autopay.limit_exceeded",
            "secondary": [],
            "phrase": "sorry your limit has exceeded",
            "evidence_turns": [1],
            "summary": "Limit exceeded at autopay",
            "proposed": None,
        }
    )
    monkeypatch.setattr(extractor, "_request_llm", request)

    answer = await extractor.classify_topic(TRANSCRIPT, ENTRIES, CONFIG)

    assert request.await_args is not None
    prompt = request.await_args.args[0]
    assert prompt.index(extractor.TWO_LEVEL_RULES) < prompt.index("CATALOG")
    assert prompt.index("CATALOG") < prompt.index("ABOUT THIS AGENT")
    assert "{accepted_topics}" not in prompt
    assert request.await_args.kwargs["response_instruction"].startswith("Return only")
    assert answer["type"] == "mandate_autopay.limit_exceeded"
    assert answer["grounded"] is True and answer["evidence_turns"] == [1]

    request.return_value = {**request.return_value, "phrase": "never said this"}
    answer = await extractor.classify_topic(TRANSCRIPT, ENTRIES, CONFIG)
    assert answer["grounded"] is False  # kept, flagged, never dropped

    request.return_value = {
        **request.return_value,
        "primary": "no_topic.no_issue",
        "phrase": "",
        "evidence_turns": [],
    }
    answer = await extractor.classify_topic(TRANSCRIPT, ENTRIES, CONFIG)
    assert answer["grounded"] is True  # no_topic needs no quote

    for broken in ({}, {"primary": None}, {"topics": [{"type": "limit"}]}):
        request.return_value = broken
        with pytest.raises(extractor.TopicModelResponseError):
            await extractor.classify_topic(TRANSCRIPT, ENTRIES, CONFIG)


async def test_two_level_catalog_saves_one_row_with_dims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace = AsyncMock()
    flat_save = AsyncMock()
    catalog_write = AsyncMock()
    monkeypatch.setattr(evaluator, "replace_topic_result", replace)
    monkeypatch.setattr(evaluator, "save_evaluation_results", flat_save)
    monkeypatch.setattr(evaluator, "add_discovered_topics", catalog_write)
    monkeypatch.setattr(
        evaluator,
        "classify_topic",
        AsyncMock(
            return_value={
                "type": "kyc.other",
                "label": "Other",
                "grounded": True,
                "proposed": None,
            }
        ),
    )
    monkeypatch.setattr(
        evaluator,
        "extract_topics",
        AsyncMock(
            return_value=[
                {"type": "cashback", "label": "rs. 500 cashback"},
                {"type": "refund_status", "label": "refund status"},
            ]
        ),
    )
    context = {
        "source_id": "call-1",
        "reseller_id": "r",
        "merchant_id": "m",
        "template_id": "t",
        "started_at": datetime.now(timezone.utc),
        "transcript": TRANSCRIPT,
        "payload": {"lender_name": "DMI", "event_name": "KYC_COMPLETED"},
    }

    assert await evaluator.analyze_topics(
        context, {"id": "cfg", "topics": ENTRIES, "configuration": CONFIG}
    )
    replace.assert_awaited_once()
    assert replace.await_args is not None
    row = replace.await_args.args[-1]
    assert row["type"] == "kyc.other"
    assert row["dims"] == {"lender_name": ["DMI"], "event_name": ["KYC_COMPLETED"]}
    flat_save.assert_not_awaited()
    catalog_write.assert_not_awaited()

    assert await evaluator.analyze_topics(
        context, {"id": "cfg", "topics": ["too many calls"], "configuration": CONFIG}
    )
    flat_save.assert_awaited_once()
    assert replace.await_count == 1
    catalog_write.assert_awaited_once_with("t", ["refund status"], flat_only=True)


@pytest.mark.skipif(not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run on Postgres")
async def test_replace_keeps_one_row_per_call_on_postgres() -> None:
    """A re-evaluation into the same id used to fail the unique index: the
    CTE's DELETE is not visible to the INSERT's check. Runs the shipped
    query against a temp table that shadows evaluation_result."""
    conn = await asyncpg.connect(DSN)
    txn = conn.transaction()
    await txn.start()
    try:
        await conn.execute("""
            DO $$ BEGIN CREATE TYPE evaluation_type AS ENUM ('TOPIC');
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
            CREATE TEMP TABLE evaluation_result (
                evaluation_config_id uuid, evaluation_type evaluation_type,
                source_id text, reseller_id text, merchant_id text,
                template_id uuid, started_at timestamptz, status text,
                result text, metadata jsonb
            );
            CREATE UNIQUE INDEX ON evaluation_result
                (source_id, evaluation_type, result) WHERE result IS NOT NULL;
            INSERT INTO evaluation_result (source_id, evaluation_type,
                evaluation_config_id, status, result)
            VALUES
                ('call-1', 'TOPIC', '00000000-0000-0000-0000-000000000001',
                 'COMPLETED', 'limit_low'),
                ('call-1', 'TOPIC', '00000000-0000-0000-0000-000000000001',
                 'FAILED', NULL);
            """)
        for result in ("kyc.other", "kyc.other", "kyc.selfie_fail"):
            query, values = replace_topic_result_query(
                "00000000-0000-0000-0000-000000000001",
                "TOPIC",
                "call-1",
                "r",
                "m",
                "00000000-0000-0000-0000-0000000000a1",
                datetime(2026, 10, 7, tzinfo=timezone.utc),
                result,
                json.dumps({"type": result}),
            )
            await conn.execute(query, *values)
            rows = await conn.fetch("SELECT status, result FROM evaluation_result")
            assert [tuple(r) for r in rows] == [("COMPLETED", result)]
    finally:
        await txn.rollback()
        await conn.close()


@pytest.mark.skipif(not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run on Postgres")
async def test_worker_auto_add_never_joins_a_two_level_list_on_postgres() -> None:
    """A job that read a flat list can finish after an admin converted it;
    its labels must not flip the list back. Admin adds are not guarded."""
    template = "00000000-0000-0000-0000-0000000000a1"
    cases = [
        (["KYC.Selfie fail"], True, "refund status", ["KYC.Selfie fail"]),
        (
            ["KYC.Selfie fail"],
            False,
            "KYC.Pan fail",
            ["KYC.Selfie fail", "KYC.Pan fail"],
        ),
        ([], True, "refund status", ["refund status"]),
        (
            ["too many calls"],
            True,
            "refund status",
            ["too many calls", "refund status"],
        ),
    ]
    conn = await asyncpg.connect(DSN)
    txn = conn.transaction()
    await txn.start()
    try:
        await conn.execute("""
            CREATE TEMP TABLE evaluation_config (
                id uuid DEFAULT gen_random_uuid(), template_id uuid,
                evaluation_type text, name text DEFAULT 'topic',
                enabled boolean, topics text[], configuration jsonb
            )
            """)
        for topics, flat_only, added, expected in cases:
            await conn.execute("DELETE FROM evaluation_config")
            await conn.execute(
                "INSERT INTO evaluation_config (template_id, evaluation_type,"
                " enabled, topics, configuration)"
                " VALUES ($1::uuid, 'TOPIC', true, $2, '{}')",
                template,
                topics,
            )
            query, values = add_discovered_topics_query(template, [added], flat_only)
            await conn.execute(query, *values)
            assert (
                await conn.fetchval("SELECT topics FROM evaluation_config") == expected
            )
    finally:
        await txn.rollback()
        await conn.close()


def test_a_masked_name_still_grounds_the_phrase() -> None:
    transcript = [
        {"role": "assistant", "content": "Namaste"},
        {"role": "user", "content": "Ankur ji ka phone hai, location on hai phir bhi"},
    ]
    masked = {"phrase": "[customer] ji ka phone hai", "evidence_turns": [1]}
    wrong = {"phrase": "[customer] ji ka laptop hai", "evidence_turns": [1]}
    assert extractor.validate_topic_evidence([masked], transcript)
    assert not extractor.validate_topic_evidence([wrong], transcript)
