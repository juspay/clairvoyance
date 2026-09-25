"""CONVERSATION_EVALS evaluation: definition, engine purity, queries, API.

The model is never in a unit test: ``decide`` runs on recorded jev-1.13.0
answers (trace a93c7b52 from the 2026-09-23 ten-trace comparison). The
worker-integration half (adapter, dispatch, result storage) is covered by
tests/test_conversation_evals_adapter.py, which travels with that code.
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals import (
    engines,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.definition import (
    validate_conversation_evals_configuration,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.base import (
    RESULT_TYPE,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.jev import (
    build_state,
    decide,
)
from app.api.routers.breeze_buddy.evaluations import handlers
from app.database.queries.breeze_buddy.evaluation_config import (
    get_evaluation_config_query,
    save_evaluation_configuration_query,
    set_evaluation_enabled_query,
    update_evaluation_configuration_query,
)
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.conversation_analysis import (
    EvaluationEnableRequest,
    EvaluationType,
    SaveEvaluationConfigurationRequest,
)

TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"

# A minimal VALID configuration for tests only. The product ships no
# default — an agent without a stored configuration does not run the
# evaluation.
EXAMPLE_CONFIGURATION: Dict[str, Any] = {
    "engine": "jev",
    "provider": "typesafe",
    "model": "jev-1.13.0",
    "questions": {
        **{
            question_id: {
                "type": "score",
                "instructions": f"grade {question_id}",
                "criteria": [f"{level} - level {level}" for level in range(1, 11)],
            }
            for question_id in (
                "llm_accuracy",
                "user_emotion",
                "stt_accuracy",
                "outcome_correctness",
                "latency",
            )
        },
        "loop_detection": {
            "type": "score",
            "instructions": "did the call loop",
            "criteria": ["0 - FAIL: loop detected", "1 - PASS: no loop"],
        },
        "verified_outcome": {
            "type": "choice",
            "instructions": "the outcome this call SHOULD have",
            "criteria": {
                "CONFIRM": "definitive final yes",
                "CANCEL": "definitive final no",
                "BUSY": "ended before a definitive answer",
                "OTHER": "a clear outcome not listed",
            },
        },
        "asked_for_human": {
            "type": "noul",
            "instructions": "Did the customer ask to speak to a human agent?",
            "criteria": {
                "true": "an explicit request for a person / customer care",
                "false": "no such request, or only frustration",
            },
        },
    },
}

# jev-1.13.0 answers for trace a93c7b52 (recorded outcome BUSY).
RECORDED_ANSWERS = {
    "llm_accuracy": {
        "type": "score",
        "score": 3.9,
        "confidence": 0.4,
        "legend": {"0": "1 - worst"},
        "probabilities": {"3": 0.6},
    },
    "loop_detection": {"type": "score", "score": 0.06, "confidence": 0.88},
    "user_emotion": {"type": "score", "score": 7.4, "confidence": 0.55},
    "stt_accuracy": {"type": "score", "score": 8.2, "confidence": 0.79},
    "outcome_correctness": {"type": "score", "score": 8.4, "confidence": 0.78},
    "latency": {"type": "score", "score": 3.7, "confidence": 0.13},
    "verified_outcome": {"type": "choice", "choice": "BUSY", "confidence": 0.9},
    "asked_for_human": {"type": "noul", "noul": 0.12},
}

LEAD_PAYLOAD = {
    "customer_name": "BISHAL DAS",
    "customer_mobile_number": "+911234567890",
    "customer_email": "someone@example.com",
    "instructions": "call script ground truth",
    "current_limit": "40000",
    "facts_quiet-15m_customer_name": "BISHAL DAS",
    # nested contact data must be stripped at any depth
    "delivery": {"city": "Pune", "alt_phone": "+911111111111"},
    "contacts": [{"whatsapp": "+912222222222"}],
    "history": [{"note": "called twice", "msisdn": "+913333333333"}],
}

LEAD_META_DATA = {
    "errors": [],
    "call_ended_by": "customer",
    "node_traversal": [{"node": "intro"}],
    "transcription": [
        {"role": "system", "content": "agent prompt duplicate"},
        {"role": "assistant", "content": "नमस्ते"},
        {"role": "user", "content": "हेलो"},
        {"role": "tool", "content": "BUSY", "tool_call_id": "call_9", "latency_ms": 12},
    ],
    "pipecat_metrics": [
        {"summary": {"llm_total_ms": 400}},
        {"summary": {"llm_total_ms": 900}},
        {"summary": {"llm_total_ms": 6275}},
        {"other": "no summary"},
    ],
}


# --- the definition and its validator ------------------------------------


def test_example_configuration_validates_unchanged():
    normalized = validate_conversation_evals_configuration(EXAMPLE_CONFIGURATION)
    # nothing is defaulted or inserted — validation returns the input as-is
    assert normalized == EXAMPLE_CONFIGURATION


def test_validator_rejects_undescribed_option():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["questions"]["verified_outcome"]["criteria"]["CALLBACK"] = "  "
    with pytest.raises(ValueError, match="needs a description"):
        validate_conversation_evals_configuration(config)


def test_validator_checks_noul_criteria_shape():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["questions"]["asked_for_human"]["criteria"] = {"yes": "x"}
    with pytest.raises(ValueError, match="noul criteria"):
        validate_conversation_evals_configuration(config)
    del config["questions"]["asked_for_human"]["criteria"]  # optional
    validate_conversation_evals_configuration(config)


def test_validator_rejects_unknown_engine_and_keys():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["engine"] = "no-such-engine"
    with pytest.raises(ValueError, match="unknown engine"):
        validate_conversation_evals_configuration(config)

    # provider must be one the engine supports (jev runs only on typesafe)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["provider"] = "azure"
    with pytest.raises(ValueError, match="does not support provider"):
        validate_conversation_evals_configuration(config)

    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["surprise"] = True
    with pytest.raises(ValueError, match="unknown configuration keys"):
        validate_conversation_evals_configuration(config)


def test_validator_rejects_non_string_engine_and_type():
    # a list/object where a string belongs must be a ValueError (400), not
    # the TypeError a dict/set lookup would raise (500)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["engine"] = ["jev"]
    with pytest.raises(ValueError, match="unknown engine"):
        validate_conversation_evals_configuration(config)

    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["questions"]["latency"]["type"] = ["score"]
    with pytest.raises(ValueError, match="type must be one of"):
        validate_conversation_evals_configuration(config)


def test_question_rules_belong_to_the_engine(monkeypatch):
    # score/choice/noul, 2-10 criteria... are Jev's primitives.
    # The type-level validator checks only engine/provider/model/keys and
    # hands questions to the engine class — another engine is
    # not held to Jev's rules.
    seen = []

    class FakeEngine:
        name = "fake"
        channels = frozenset()
        providers = {"acme": object()}

        def validate_configuration(self, configuration):
            seen.append(configuration)

        async def evaluate(self, context, configuration):
            return {}

    monkeypatch.setitem(engines.ENGINES, "fake", FakeEngine())
    config = {
        "engine": "fake",
        "provider": "acme",
        "model": "fake-1",
        "questions": {"free": "form"},
    }
    assert validate_conversation_evals_configuration(config) == config
    assert seen == [config]


def test_migration_adds_enum_value_and_seeds_nothing():
    """One migration file: the enum value + the reshaped runtime check.
    Nothing is inserted — there is no default configuration anywhere;
    rows exist only when an admin stores one via the configuration POST."""
    sql = (
        Path(__file__).parent.parent
        / "app/database/migrations/080_add_conversation_evals_evaluation_type.sql"
    ).read_text()
    assert "ADD VALUE IF NOT EXISTS 'CONVERSATION_EVALS'" in sql
    # the CHECK is shape-based (prompt shape OR typed-judgment shape) and
    # references no enum values, so it can share the enum's transaction;
    # which shape belongs to which type is enforced by the API validators
    assert "evaluation_type" not in sql.split("ADD CONSTRAINT")[1]
    assert "jsonb_typeof(configuration -> 'system_prompt') = 'string'" in sql
    assert "jsonb_typeof(configuration -> 'engine') = 'string'" in sql
    assert "jsonb_typeof(configuration -> 'provider') = 'string'" in sql
    assert "jsonb_typeof(configuration -> 'questions') = 'object'" in sql
    assert "INSERT" not in sql.upper()


# --- build_state (the gather half, pure) ----------------------------------


def test_build_state_projection():
    state = build_state(
        LEAD_PAYLOAD, LEAD_META_DATA, "BUSY", LEAD_META_DATA["transcription"]
    )

    payload = state["extracted_payload"]
    assert "customer_mobile_number" not in payload  # PII never leaves
    assert "customer_email" not in payload
    assert not any(key.startswith("facts_quiet") for key in payload)
    assert payload["instructions"] == "call script ground truth"
    # nested and alternate contact keys are gone too; the rest survives
    assert "contacts" not in payload  # key name itself is a contact marker
    assert payload["delivery"] == {"city": "Pune"}
    assert payload["history"] == [{"note": "called twice"}]

    logs = state["call_logs"]
    assert [m["role"] for m in logs["transcription"]] == [
        "assistant",
        "user",
        "tool",
    ]  # system dropped: duplicates payload.instructions
    # turns are projected to role + content only
    assert logs["transcription"][2] == {"role": "tool", "content": "BUSY"}
    assert logs["pipecat_metrics"] == {
        "turns": 4,
        "llm_total_ms_median": 900,
        "llm_total_ms_max": 6275,
    }
    assert state["recorded_outcome"] == "BUSY"


def test_build_state_chat_uses_worker_transcript():
    # chat has no lead payload/metadata/outcome: the worker's transcript
    # alone is the state, voice-only extras come out empty
    turns = [{"idx": 0, "role": "user", "content": "hi"}]
    state = build_state(None, None, None, turns)
    # projected to role + content (chat's idx is dropped)
    assert state["call_logs"]["transcription"] == [{"role": "user", "content": "hi"}]
    assert state["extracted_payload"] == {}
    assert state["call_logs"]["pipecat_metrics"]["turns"] == 0


def test_build_state_handles_missing_everything():
    state = build_state(None, None, None, None)
    assert state["extracted_payload"] == {}
    assert state["call_logs"]["transcription"] == []
    assert state["call_logs"]["pipecat_metrics"]["llm_total_ms_median"] is None


def test_build_state_is_pure():
    payload_before = copy.deepcopy(LEAD_PAYLOAD)
    meta_before = copy.deepcopy(LEAD_META_DATA)
    build_state(LEAD_PAYLOAD, LEAD_META_DATA, "BUSY", LEAD_META_DATA["transcription"])
    assert LEAD_PAYLOAD == payload_before
    assert LEAD_META_DATA == meta_before


# --- decide (the pure verdict, config-driven) -----------------------------


def test_decide_golden_a93c7b52():
    verdict = decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION)

    assert verdict["type"] == RESULT_TYPE
    assert verdict["engine"] == "jev"
    assert verdict["provider"] == "typesafe"
    assert verdict["model"] == "jev-1.13.0"

    answers = verdict["answers"]
    # every configured question is stored, whatever its name — code knows none
    assert set(answers) == set(EXAMPLE_CONFIGURATION["questions"])
    # 10-level rubrics map level+1; a 2-level rubric stays 0-1
    assert answers["llm_accuracy"] == {"score": 4.9, "confidence": 0.4}
    assert answers["latency"] == {"score": 4.7, "confidence": 0.13}
    assert answers["loop_detection"] == {"score": 0.06, "confidence": 0.88}
    # a choice answer is the option picked, with its confidence
    assert answers["verified_outcome"] == {"choice": "BUSY", "confidence": 0.9}
    # a noul is P(yes) alone — TypeSafe reports no separate confidence for it
    assert answers["asked_for_human"] == {"noul": 0.12}
    # concise: nothing else per answer (no legend/raw/probabilities/type),
    # nothing derived at the top (no flags, no comparisons)
    assert all(
        set(a) <= {"score", "choice", "confidence"} or set(a) == {"noul"}
        for a in answers.values()
    )
    assert set(verdict) == {"type", "engine", "provider", "model", "answers"}


def test_decide_tolerates_missing_answers():
    verdict = decide({}, EXAMPLE_CONFIGURATION)
    assert verdict["answers"]["llm_accuracy"] == {"score": None, "confidence": None}
    assert verdict["answers"]["verified_outcome"] == {
        "choice": None,
        "confidence": None,
    }
    assert verdict["answers"]["asked_for_human"] == {"noul": None}


def test_decide_is_pure():
    answers_before = copy.deepcopy(RECORDED_ANSWERS)
    config_before = copy.deepcopy(EXAMPLE_CONFIGURATION)
    decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION)
    assert RECORDED_ANSWERS == answers_before
    assert EXAMPLE_CONFIGURATION == config_before


# --- the engine routes to the configured provider --------------------------


class FakeProvider:
    name = "typesafe"

    def __init__(self, body):
        self.body = body
        self.calls = []

    async def judge(self, state, questions, model):
        self.calls.append((state, questions, model))
        return self.body


async def test_jev_evaluate_routes_to_the_configured_provider(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.jev import (
        JevEngine,
    )

    provider = FakeProvider({"answers": RECORDED_ANSWERS, "model": "jev-1.13.0-b"})
    monkeypatch.setattr(JevEngine, "providers", {"typesafe": provider})

    context = {
        "payload": LEAD_PAYLOAD,
        "meta_data": LEAD_META_DATA,
        "recorded_outcome": "BUSY",
        "transcript": LEAD_META_DATA["transcription"],
    }
    verdict = await JevEngine().evaluate(context, EXAMPLE_CONFIGURATION)

    # one call, to the provider named in the config, with the projected state
    assert len(provider.calls) == 1
    state, questions, model = provider.calls[0]
    assert state == build_state(
        LEAD_PAYLOAD, LEAD_META_DATA, "BUSY", LEAD_META_DATA["transcription"]
    )
    assert questions == EXAMPLE_CONFIGURATION["questions"]
    assert model == "jev-1.13.0"
    # verdict is decide() over the body, with the model that actually served
    assert (
        verdict["answers"] == decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION)["answers"]
    )
    assert verdict["model"] == "jev-1.13.0-b"


def _typesafe_with(handler, monkeypatch):
    """A TypeSafeProvider whose shared pool is an httpx MockTransport."""
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers import (
        typesafe,
    )

    monkeypatch.setattr(typesafe, "TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(typesafe.TypeSafeProvider, "_BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(
        typesafe.TypeSafeProvider,
        "_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return typesafe.TypeSafeProvider()


async def test_typesafe_judge_posts_state_questions_model(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": {"q": {}}, "model": "m-served"})

    body = await _typesafe_with(handler, monkeypatch).judge({"s": 1}, {"q": {}}, "m")
    assert body == {"answers": {"q": {}}, "model": "m-served"}
    assert seen["auth"] == "Bearer k"
    assert seen["json"] == {"state": {"s": 1}, "model": "m", "questions": {"q": {}}}


async def test_typesafe_judge_retries_only_what_a_retry_can_fix(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers import (
        TypeSafeError,
    )

    codes = iter([503, 200])
    seen = []

    def flaky(request):
        code = next(codes)
        seen.append(code)
        return httpx.Response(code, json={"answers": {}})

    assert await _typesafe_with(flaky, monkeypatch).judge({}, {}, "m") == {
        "answers": {}
    }
    assert seen == [503, 200]  # a 5xx is retried

    seen.clear()

    def rejected(request):
        seen.append(400)
        return httpx.Response(400, text="bad rubric")

    with pytest.raises(TypeSafeError, match="HTTP 400"):
        await _typesafe_with(rejected, monkeypatch).judge({}, {}, "m")
    assert seen == [400]  # a 4xx is final: one attempt, no retry


async def test_typesafe_close_drains_the_shared_pool(monkeypatch):
    provider = _typesafe_with(lambda request: httpx.Response(200), monkeypatch)
    pool = type(provider)._client
    assert pool is not None and not pool.is_closed
    await provider.close()
    assert pool.is_closed and type(provider)._client is None
    await provider.close()  # idempotent: nothing open, nothing raised


async def test_close_conversation_evals_provider_pools_closes_every_provider_once(
    monkeypatch,
):
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals import (
        providers,
    )

    first, second = SimpleNamespace(close=AsyncMock()), SimpleNamespace(
        close=AsyncMock()
    )
    monkeypatch.setattr(providers, "PROVIDERS", {"a": first, "b": second})
    await providers.close_conversation_evals_provider_pools()
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


def test_engines_use_the_registered_provider_instances():
    # the closer drains PROVIDERS; an engine holding a private instance of a
    # per-instance-pool provider would escape it
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines import (
        ENGINES,
    )
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers import (
        PROVIDERS,
    )

    for engine in ENGINES.values():
        for name, provider in engine.providers.items():
            assert PROVIDERS[name] is provider


async def test_typesafe_judge_without_key_is_loud(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers import (
        typesafe,
    )

    monkeypatch.setattr(typesafe, "TYPESAFE_API_KEY", "")
    with pytest.raises(typesafe.TypeSafeError, match="TYPESAFE_API_KEY"):
        await typesafe.TypeSafeProvider().judge({}, {}, "m")


# --- the SQL builders ------------------------------------------------------


def test_enable_flips_existing_row_only():
    # no row (or a disabled one) = the eval does not run; enable NEVER
    # creates a row — creation belongs to the configuration write
    query, values = set_evaluation_enabled_query(
        TEMPLATE_ID, "CONVERSATION_EVALS", True
    )
    assert "UPDATE evaluation_config" in query
    assert "INSERT" not in query.upper()
    assert values == [TEMPLATE_ID, "CONVERSATION_EVALS", True]


def test_config_reads_are_type_threaded():
    query, values = get_evaluation_config_query(TEMPLATE_ID, "CONVERSATION_EVALS")
    assert "$2::evaluation_type" in query
    assert values == [TEMPLATE_ID, "CONVERSATION_EVALS"]


def test_configuration_update_is_shallow_merge():
    # edits an existing row only — nothing in this API creates rows
    query, values = update_evaluation_configuration_query(
        TEMPLATE_ID, "CONVERSATION_EVALS", {"engine": "jev"}
    )
    assert "UPDATE evaluation_config" in query
    assert "SET configuration = configuration || $3::jsonb" in query
    assert "INSERT" not in query.upper()
    assert values[:2] == [TEMPLATE_ID, "CONVERSATION_EVALS"]
    assert json.loads(values[2]) == {"engine": "jev"}


def test_save_configuration_creates_disabled_or_replaces():
    query, values = save_evaluation_configuration_query(
        TEMPLATE_ID, "CONVERSATION_EVALS", EXAMPLE_CONFIGURATION
    )
    # the one creation point — a missing row is born DISABLED: configuring
    # is not consenting to run; enable is a separate explicit flip
    assert "INSERT INTO evaluation_config" in query
    assert "VALUES ($1::uuid, $2::evaluation_type, false, $3::jsonb)" in query
    # an existing row: configuration replaced wholesale, enabled untouched
    assert "DO UPDATE SET configuration = EXCLUDED.configuration" in query
    assert "EXCLUDED.enabled" not in query
    assert values[:2] == [TEMPLATE_ID, "CONVERSATION_EVALS"]
    assert json.loads(values[2]) == EXAMPLE_CONFIGURATION


# --- the API handlers ------------------------------------------------------


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setattr(
        handlers,
        "get_template_by_id",
        AsyncMock(return_value=SimpleNamespace(reseller_id="r", merchant_id="m")),
    )
    monkeypatch.setattr(handlers, "validate_template_access", lambda *a, **k: None)
    monkeypatch.setattr(handlers, "require_admin", lambda user: None)
    return SimpleNamespace(role=UserRole.ADMIN)  # the current_user stub


async def test_get_404s_without_a_row(api_env, monkeypatch):
    # no default preview: a missing configuration means the eval is off
    monkeypatch.setattr(handlers, "get_evaluation_config", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await handlers.get_evaluation_config_handler(
            TEMPLATE_ID, EvaluationType.CONVERSATION_EVALS, api_env
        )
    assert exc.value.status_code == 404


async def test_response_decodes_jsonb_text(api_env, monkeypatch):
    # asyncpg returns jsonb columns as text: the response must still be a dict
    monkeypatch.setattr(
        handlers,
        "get_evaluation_config",
        AsyncMock(
            return_value={
                "template_id": TEMPLATE_ID,
                "enabled": True,
                "configuration": json.dumps(EXAMPLE_CONFIGURATION),
            }
        ),
    )
    response = await handlers.get_evaluation_config_handler(
        TEMPLATE_ID, EvaluationType.CONVERSATION_EVALS, api_env
    )
    assert response.configuration == EXAMPLE_CONFIGURATION


async def test_enable_flips_and_404s_when_absent(api_env, monkeypatch):
    flip = AsyncMock(
        return_value={
            "template_id": TEMPLATE_ID,
            "enabled": True,
            "configuration": EXAMPLE_CONFIGURATION,
        }
    )
    monkeypatch.setattr(handlers, "set_evaluation_enabled", flip)
    response = await handlers.set_evaluation_enabled_handler(
        TEMPLATE_ID,
        EvaluationEnableRequest(
            evaluation_type=EvaluationType.CONVERSATION_EVALS, enabled=True
        ),
        api_env,
    )
    assert response.enabled is True
    assert flip.await_args is not None
    # a pure flip: no configuration rides along, nothing is created
    assert flip.await_args.args == (TEMPLATE_ID, "CONVERSATION_EVALS", True)
    assert flip.await_args.kwargs == {}

    monkeypatch.setattr(
        handlers, "set_evaluation_enabled", AsyncMock(return_value=None)
    )
    with pytest.raises(HTTPException) as exc:
        await handlers.set_evaluation_enabled_handler(
            TEMPLATE_ID,
            EvaluationEnableRequest(
                evaluation_type=EvaluationType.CONVERSATION_EVALS, enabled=True
            ),
            api_env,
        )
    assert exc.value.status_code == 404


async def test_enable_serves_topic_too(api_env, monkeypatch):
    # the generic surface serves TOPIC as well; /topics stays alongside it
    flip = AsyncMock(
        return_value={
            "template_id": TEMPLATE_ID,
            "enabled": False,
            "topics": ["refund", "delivery"],
            "configuration": {"model": "gpt-4o", "system_prompt": "x"},
        }
    )
    monkeypatch.setattr(handlers, "set_evaluation_enabled", flip)
    response = await handlers.set_evaluation_enabled_handler(
        TEMPLATE_ID,
        EvaluationEnableRequest(evaluation_type=EvaluationType.TOPIC, enabled=False),
        api_env,
    )
    assert response.enabled is False
    assert response.topics == ["refund", "delivery"]  # the catalog rides along
    assert flip.await_args is not None
    assert flip.await_args.args == (TEMPLATE_ID, "TOPIC", False)


async def test_configuration_post_validates(api_env, monkeypatch):
    update = AsyncMock(
        return_value={
            "template_id": TEMPLATE_ID,
            "enabled": True,
            "configuration": EXAMPLE_CONFIGURATION,
        }
    )
    monkeypatch.setattr(handlers, "save_evaluation_configuration", update)

    # invalid configuration → 400, nothing stored
    with pytest.raises(HTTPException) as exc:
        await handlers.save_evaluation_configuration_handler(
            TEMPLATE_ID,
            SaveEvaluationConfigurationRequest(
                evaluation_type=EvaluationType.CONVERSATION_EVALS,
                configuration={"engine": "no-such-engine"},
            ),
            api_env,
        )
    assert exc.value.status_code == 400
    update.assert_not_awaited()

    # TOPIC goes through the topics resolver: a missing model is rejected
    with pytest.raises(HTTPException) as exc:
        await handlers.save_evaluation_configuration_handler(
            TEMPLATE_ID,
            SaveEvaluationConfigurationRequest(
                evaluation_type=EvaluationType.TOPIC, configuration={}
            ),
            api_env,
        )
    assert exc.value.status_code == 400
    update.assert_not_awaited()

    # TOPIC without a system_prompt: the resolver tolerates it, the runtime
    # CHECK does not — reject before the INSERT (400, not a 500)
    with pytest.raises(HTTPException) as exc:
        await handlers.save_evaluation_configuration_handler(
            TEMPLATE_ID,
            SaveEvaluationConfigurationRequest(
                evaluation_type=EvaluationType.TOPIC,
                configuration={"model": "gpt-4o", "system_prompt": "  "},
            ),
            api_env,
        )
    assert exc.value.status_code == 400
    assert "system_prompt" in exc.value.detail
    update.assert_not_awaited()

    # valid CONVERSATION_EVALS configuration goes through
    response = await handlers.save_evaluation_configuration_handler(
        TEMPLATE_ID,
        SaveEvaluationConfigurationRequest(
            evaluation_type=EvaluationType.CONVERSATION_EVALS,
            configuration=copy.deepcopy(EXAMPLE_CONFIGURATION),
        ),
        api_env,
    )
    assert response.enabled is True
    update.assert_awaited_once()


async def test_topic_configuration_post_stores_resolved_shape(api_env, monkeypatch):
    # full replacement: the resolver fills TOPIC's defaults (provider,
    # settings) so the stored row is the resolved shape, not the raw body
    update = AsyncMock(
        return_value={
            "template_id": TEMPLATE_ID,
            "enabled": True,
            "topics": [],
            "configuration": {},
        }
    )
    monkeypatch.setattr(handlers, "save_evaluation_configuration", update)
    await handlers.save_evaluation_configuration_handler(
        TEMPLATE_ID,
        SaveEvaluationConfigurationRequest(
            evaluation_type=EvaluationType.TOPIC,
            configuration={"model": "gpt-4o", "system_prompt": "find topics"},
        ),
        api_env,
    )
    assert update.await_args is not None
    stored = update.await_args.args[2]
    assert update.await_args.args[:2] == (TEMPLATE_ID, "TOPIC")
    assert stored["model"] == "gpt-4o"
    assert stored["system_prompt"] == "find topics"
    assert stored["provider"]  # defaulted by the resolver
    assert "settings" in stored


async def test_configuration_is_admin_only_in_responses(api_env, monkeypatch):
    # as on /topics: template access shows the flag + catalog, the
    # configuration itself (prompts, rubrics) only to admins
    row = {
        "template_id": TEMPLATE_ID,
        "enabled": True,
        "topics": ["refund"],
        "configuration": EXAMPLE_CONFIGURATION,
    }
    monkeypatch.setattr(handlers, "get_evaluation_config", AsyncMock(return_value=row))

    merchant: Any = SimpleNamespace(role=UserRole.MERCHANT)
    response = await handlers.get_evaluation_config_handler(
        TEMPLATE_ID, EvaluationType.CONVERSATION_EVALS, merchant
    )
    assert response.configuration is None
    assert response.enabled is True
    assert response.topics == ["refund"]

    response = await handlers.get_evaluation_config_handler(
        TEMPLATE_ID, EvaluationType.CONVERSATION_EVALS, api_env
    )
    assert response.configuration == EXAMPLE_CONFIGURATION
