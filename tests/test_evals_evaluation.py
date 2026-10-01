"""The evals service: definition, engine purity, queries, API.

The model is never in a unit test: ``decide`` runs on recorded jev-1.13.0
answers (trace a93c7b52 from the 2026-09-23 ten-trace comparison). The
worker-integration half (adapter, dispatch, result storage) is covered by
tests/test_evals_adapter.py, which travels with that code.
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
from pydantic import BaseModel, ConfigDict

from app.ai.voice.agents.breeze_buddy.services.evals import (
    engines,
    providers,
)
from app.ai.voice.agents.breeze_buddy.services.evals.definition import (
    validate_evals_configuration,
)
from app.ai.voice.agents.breeze_buddy.services.evals.engines import (
    ENGINES,
)
from app.ai.voice.agents.breeze_buddy.services.evals.engines.common import (
    ChoiceResult,
    NoulResult,
    ScoreResult,
)
from app.ai.voice.agents.breeze_buddy.services.evals.engines.structured_judge_engine import (
    StructuredJudgeConfiguration,
    StructuredJudgeEngine,
    build_state,
    decide,
)
from app.ai.voice.agents.breeze_buddy.services.evals.providers import (
    PROVIDERS,
    ProviderRequest,
    ProviderResponse,
    TypeSafeError,
    typesafe,
)
from app.api.routers.breeze_buddy.evaluations import handlers
from app.database.queries.breeze_buddy.evaluation_config import (
    get_evaluation_config_query,
    save_evaluation_configuration_query,
    set_evaluation_enabled_query,
    update_evaluation_configuration_query,
)
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.evals import (
    EvaluationEnableRequest,
    EvaluationType,
    SaveEvaluationConfigurationRequest,
)

TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"

# A minimal VALID configuration for tests only. The product ships no
# default — an agent without a stored configuration does not run the
# evaluation.
EXAMPLE_CONFIGURATION: Dict[str, Any] = {
    "engine": "structured",
    "provider": "typesafe",
    "model": "jev-1.13.0",
    "questions": [
        *[
            {
                "key": question_id,
                "type": "score",
                "label": question_id.replace("_", " ").capitalize(),
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
        ],
        {
            "key": "loop_detection",
            "label": "Loop detection",
            "type": "score",
            "instructions": "did the call loop",
            "criteria": ["0 - FAIL: loop detected", "1 - PASS: no loop"],
        },
        {
            "key": "verified_outcome",
            "label": "Verified outcome",
            "type": "choice",
            "instructions": "the outcome this call SHOULD have",
            "criteria": {
                "CONFIRM": "definitive final yes",
                "CANCEL": "definitive final no",
                "BUSY": "ended before a definitive answer",
                "OTHER": "a clear outcome not listed",
            },
        },
        {
            "key": "asked_for_human",
            "label": "Asked for human",
            "type": "noul",
            "instructions": "Did the customer ask to speak to a human agent?",
            "criteria": {
                "true": "an explicit request for a person / customer care",
                "false": "no such request, or only frustration",
            },
        },
    ],
}


def _question(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    """The one question with this key in a configuration's array."""
    return next(q for q in config["questions"] if q["key"] == key)


def _typesafe_questions(config: Dict[str, Any]) -> Dict[str, Any]:
    """What the structured engine sends the vendor: the primitives keyed by
    question key; ``key`` and ``label`` stay home."""
    return {
        q["key"]: {k: v for k, v in q.items() if k not in ("key", "label")}
        for q in config["questions"]
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


def _voice_context() -> Dict[str, Any]:
    """What the worker hands the evals for one finished call."""
    return {
        "channel": "VOICE",
        "payload": LEAD_PAYLOAD,
        "meta_data": LEAD_META_DATA,
        "recorded_outcome": "BUSY",
        "transcript": LEAD_META_DATA["transcription"],
    }


# --- the definition and its validator ------------------------------------


def test_example_configuration_validates_unchanged():
    normalized = validate_evals_configuration(EXAMPLE_CONFIGURATION)
    # the engine's typed configuration, with nothing defaulted or inserted
    assert isinstance(normalized, StructuredJudgeConfiguration)
    assert normalized.model_dump() == EXAMPLE_CONFIGURATION


def test_every_question_needs_a_unique_key():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    del config["questions"][0]["key"]
    with pytest.raises(ValueError, match="question 0"):
        validate_evals_configuration(config)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["questions"][1]["key"] = config["questions"][0]["key"]
    with pytest.raises(ValueError, match="duplicate question key 'llm_accuracy'"):
        validate_evals_configuration(config)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["questions"] = {}
    with pytest.raises(ValueError, match="questions must be a non-empty array"):
        validate_evals_configuration(config)


def test_every_question_needs_a_label_for_display():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    del _question(config, "latency")["label"]
    with pytest.raises(ValueError, match="question 'latency'"):
        validate_evals_configuration(config)
    _question(config, "latency")["label"] = "  "
    with pytest.raises(ValueError, match="label must be a non-empty string"):
        validate_evals_configuration(config)


def test_validator_rejects_undescribed_option():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    _question(config, "verified_outcome")["criteria"]["CALLBACK"] = "  "
    with pytest.raises(ValueError, match="needs a description"):
        validate_evals_configuration(config)


def test_validator_checks_noul_criteria_shape():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    _question(config, "asked_for_human")["criteria"] = {"yes": "x"}
    with pytest.raises(ValueError, match="noul criteria"):
        validate_evals_configuration(config)
    del _question(config, "asked_for_human")["criteria"]  # optional
    validate_evals_configuration(config)


def test_validator_rejects_unknown_engine_and_keys():
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["engine"] = "no-such-engine"
    with pytest.raises(ValueError, match="unknown engine"):
        validate_evals_configuration(config)

    # provider must be one the engine supports (structured runs only on typesafe)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["provider"] = "azure"
    with pytest.raises(ValueError, match="does not support provider"):
        validate_evals_configuration(config)

    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["surprise"] = True
    with pytest.raises(ValueError, match="unknown configuration keys"):
        validate_evals_configuration(config)


def test_validator_rejects_non_string_engine_and_type():
    # a list/object where a string belongs must be a ValueError (400), not
    # the TypeError a dict/set lookup would raise (500)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["engine"] = ["structured"]
    with pytest.raises(ValueError, match="unknown engine"):
        validate_evals_configuration(config)

    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    _question(config, "latency")["type"] = ["score"]
    with pytest.raises(ValueError, match="type must be one of"):
        validate_evals_configuration(config)


def test_question_rules_belong_to_the_engine(monkeypatch):
    # score/choice/noul, 2-10 criteria... are the structured engine's primitives.
    # The type-level validator checks only engine/provider/model/keys and
    # hands questions to the engine class — another engine is
    # not held to the structured engine's rules.
    seen = []

    class FakeConfiguration(BaseModel):
        model_config = ConfigDict(extra="allow")
        engine: str
        provider: str
        model: str

    class FakeEngine:
        name = "fake"
        channels = frozenset()
        providers = {"acme": object()}
        configuration_keys = frozenset({"questions"})

        def validate_configuration(self, configuration):
            seen.append(configuration)
            return FakeConfiguration.model_validate(configuration)

        async def evaluate(self, context, configuration):
            return {}

    monkeypatch.setitem(engines.ENGINES, "fake", FakeEngine())
    config = {
        "engine": "fake",
        "provider": "acme",
        "model": "fake-1",
        "questions": {"free": "form"},
    }
    assert validate_evals_configuration(config).model_dump() == config
    assert seen == [config]


def test_migration_adds_enum_value_and_seeds_nothing():
    """One migration file: the enum value + the reshaped runtime check.
    Nothing is inserted — there is no default configuration anywhere;
    rows exist only when an admin stores one via the configuration POST."""
    sql = (
        Path(__file__).parent.parent
        / "app/database/migrations/082_add_conversation_evals_evaluation_type.sql"
    ).read_text()
    assert "ADD VALUE IF NOT EXISTS 'CONVERSATION_EVALS'" in sql
    # the CHECK is shape-based (prompt shape OR typed-judgment shape) and
    # references no enum values, so it can share the enum's transaction;
    # which shape belongs to which type is enforced by the API validators
    assert "evaluation_type" not in sql.split("ADD CONSTRAINT")[1]
    assert "jsonb_typeof(configuration -> 'system_prompt') = 'string'" in sql
    assert "jsonb_typeof(configuration -> 'engine') = 'string'" in sql
    assert "jsonb_typeof(configuration -> 'provider') = 'string'" in sql
    assert "jsonb_typeof(configuration -> 'questions') = 'array'" in sql
    assert "INSERT" not in sql.upper()


# --- build_state (the gather half, pure) ----------------------------------


def test_build_state_projection():
    state = build_state(_voice_context())

    payload = state["extracted_payload"]
    assert "customer_mobile_number" not in payload  # PII never leaves
    assert "customer_email" not in payload
    assert not any(key.startswith("facts_quiet") for key in payload)
    assert payload["instructions"] == "call script ground truth"
    # nested and alternate contact keys are gone too; the rest survives
    assert "contacts" not in payload  # key name itself is a contact marker
    assert payload["delivery"] == {"city": "Pune"}
    assert payload["history"] == [{"note": "called twice"}]

    assert state["channel"] == "VOICE"
    logs = state["conversation"]
    assert logs["ended_by"] == "customer"
    assert [m["role"] for m in logs["transcription"]] == [
        "assistant",
        "user",
        "tool",
    ]  # system dropped: duplicates payload.instructions
    # turns are projected to role + content only
    assert logs["transcription"][2] == {"role": "tool", "content": "BUSY"}
    assert logs["metrics"] == {
        "turns": 4,
        "llm_total_ms_median": 900,
        "llm_total_ms_max": 6275,
    }
    assert state["recorded_outcome"] == "BUSY"


def test_build_state_chat_uses_worker_transcript():
    # chat has no lead payload/metadata/outcome: the worker's transcript
    # alone is the state, voice-only extras come out empty
    turns = [{"idx": 0, "role": "user", "content": "hi"}]
    state = build_state(
        {
            "channel": "CHAT",
            "transcript": turns,
            "payload": {"customer_name": "A", "customer_mobile_number": "+91"},
            "meta_data": {"ended_reason": "user_ended"},
            "recorded_outcome": "CONFIRM",
        }
    )
    assert state["channel"] == "CHAT"
    assert state["recorded_outcome"] == "CONFIRM"
    # projected to role + content (chat's idx is dropped)
    assert state["conversation"]["transcription"] == [{"role": "user", "content": "hi"}]
    # a chat session's render-time variables are its payload, contacts stripped
    assert state["extracted_payload"] == {"customer_name": "A"}
    # how the session ended rides in the same slot as a call's call_ended_by
    assert state["conversation"]["ended_by"] == "user_ended"
    # voice-only extras come out empty, never missing
    assert state["conversation"]["metrics"]["turns"] == 0
    assert state["conversation"]["errors"] == []


def test_build_state_handles_missing_everything():
    state = build_state({})
    assert state["channel"] is None
    assert state["extracted_payload"] == {}
    assert state["conversation"]["transcription"] == []
    assert state["conversation"]["metrics"]["llm_total_ms_median"] is None


def test_build_state_is_pure():
    payload_before = copy.deepcopy(LEAD_PAYLOAD)
    meta_before = copy.deepcopy(LEAD_META_DATA)
    build_state(_voice_context())
    assert LEAD_PAYLOAD == payload_before
    assert LEAD_META_DATA == meta_before


# --- decide (the pure verdict, config-driven) -----------------------------


def test_decide_golden_a93c7b52():
    verdict = decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION)

    assert verdict.engine == "structured"
    assert verdict.provider == "typesafe"
    assert verdict.model == "jev-1.13.0"
    # the stored structure: top level says who judged, result is the list
    assert set(verdict.model_dump()) == {"engine", "provider", "model", "result"}

    results = {r.key: r for r in verdict.result}
    # every configured question gets one typed result — code knows no name
    assert [r.key for r in verdict.result] == [
        q["key"] for q in EXAMPLE_CONFIGURATION["questions"]
    ]
    # 10-level rubrics map level+1 onto 1..10; a 2-level rubric stays 0..1
    assert results["llm_accuracy"] == ScoreResult(
        key="llm_accuracy",
        label="Llm accuracy",
        value=4.9,
        min=1,
        max=10,
        confidence=0.4,
    )
    assert results["latency"] == ScoreResult(
        key="latency", label="Latency", value=4.7, min=1, max=10, confidence=0.13
    )
    assert results["loop_detection"] == ScoreResult(
        key="loop_detection",
        label="Loop detection",
        value=0.06,
        min=0,
        max=1,
        confidence=0.88,
    )
    # a choice is the option picked, with its confidence
    assert results["verified_outcome"] == ChoiceResult(
        key="verified_outcome", label="Verified outcome", value="BUSY", confidence=0.9
    )
    # a noul is P(yes) alone — TypeSafe reports no separate confidence for it
    assert results["asked_for_human"] == NoulResult(
        key="asked_for_human", label="Asked for human", value=0.12
    )
    # concise: nothing else per result (no legend/raw/probabilities),
    # nothing derived at the top (no flags, no comparisons)
    assert set(results["llm_accuracy"].model_dump()) == {
        "kind",
        "key",
        "label",
        "value",
        "min",
        "max",
        "confidence",
    }
    assert set(results["asked_for_human"].model_dump()) == {
        "kind",
        "key",
        "label",
        "value",
    }


def test_decide_tolerates_missing_answers():
    results = {r.key: r for r in decide({}, EXAMPLE_CONFIGURATION).result}
    assert results["llm_accuracy"] == ScoreResult(
        key="llm_accuracy",
        label="Llm accuracy",
        value=None,
        min=1,
        max=10,
        confidence=None,
    )
    assert results["verified_outcome"] == ChoiceResult(
        key="verified_outcome", label="Verified outcome", value=None, confidence=None
    )
    assert results["asked_for_human"] == NoulResult(
        key="asked_for_human", label="Asked for human", value=None
    )


def test_decide_survives_a_malformed_vendor_answer():
    # one bad answer costs that answer, never the verdict (CodeRabbit on
    # #1207: a string score used to raise and lose all eight results)
    answers: Dict[str, Dict[str, Any]] = copy.deepcopy(RECORDED_ANSWERS)
    answers["llm_accuracy"]["score"] = "high"
    answers["latency"]["confidence"] = {"oops": 1}
    answers["asked_for_human"]["noul"] = "yes"
    answers["loop_detection"]["score"] = True  # a bool is not a number
    results = {r.key: r for r in decide(answers, EXAMPLE_CONFIGURATION).result}
    assert results["llm_accuracy"] == ScoreResult(  # the good part survives
        key="llm_accuracy",
        label="Llm accuracy",
        value=None,
        min=1,
        max=10,
        confidence=0.4,
    )
    assert results["latency"] == ScoreResult(
        key="latency", label="Latency", value=4.7, min=1, max=10, confidence=None
    )
    assert results["asked_for_human"].value is None
    assert results["loop_detection"].value is None
    # the untouched answers are stored exactly as before
    assert results["verified_outcome"] == ChoiceResult(
        key="verified_outcome", label="Verified outcome", value="BUSY", confidence=0.9
    )
    assert results["stt_accuracy"].value == 9.2


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

    async def call(self, request):
        self.calls.append(request)
        return ProviderResponse(text=json.dumps(self.body), model=self.body["model"])

    async def close(self):
        return None


async def test_structured_evaluate_routes_to_the_configured_provider(monkeypatch):
    provider = FakeProvider({"answers": RECORDED_ANSWERS, "model": "jev-1.13.0-b"})
    monkeypatch.setattr(StructuredJudgeEngine, "providers", {"typesafe": provider})

    context = _voice_context()
    verdict = await StructuredJudgeEngine().evaluate(context, EXAMPLE_CONFIGURATION)

    # one call, to the provider named in the config: the projected state and
    # the typed questions as the JSON body, no prompt, no call options
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert json.loads(request.content) == {
        "state": build_state(_voice_context()),
        "questions": _typesafe_questions(EXAMPLE_CONFIGURATION),
    }
    assert request.model == "jev-1.13.0"
    assert request.instruction is None
    assert request.settings == {}
    # verdict is decide() over the body, with the model that actually served
    assert verdict.result == decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION).result
    assert verdict.model == "jev-1.13.0-b"


def _request(state=None, questions=None, model="m") -> ProviderRequest:
    """What an engine hands TypeSafe: the JSON body, no instruction."""
    return ProviderRequest(
        model=model,
        instruction=None,
        content=json.dumps({"state": state or {}, "questions": questions or {}}),
        settings={},
    )


def _typesafe_with(handler, monkeypatch):
    """A TypeSafeProvider whose shared pool is an httpx MockTransport."""
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

    response = await _typesafe_with(handler, monkeypatch).call(
        _request({"s": 1}, {"q": {}}, "m")
    )
    assert json.loads(response.text) == {"answers": {"q": {}}, "model": "m-served"}
    assert response.model == "m-served"
    assert seen["auth"] == "Bearer k"
    assert seen["json"] == {"state": {"s": 1}, "model": "m", "questions": {"q": {}}}


async def test_typesafe_judge_retries_only_what_a_retry_can_fix(monkeypatch):
    codes = iter([503, 504, 200])
    seen = []

    def flaky(request):
        code = next(codes)
        seen.append(code)
        return httpx.Response(code, json={"answers": {}})

    response = await _typesafe_with(flaky, monkeypatch).call(_request())
    assert json.loads(response.text) == {"answers": {}}
    assert seen == [503, 504, 200]  # every 5xx is retried, 504 included

    seen.clear()

    def rejected(request):
        seen.append(400)
        return httpx.Response(400, text="bad rubric")

    with pytest.raises(TypeSafeError, match="HTTP 400"):
        await _typesafe_with(rejected, monkeypatch).call(_request())
    assert seen == [400]  # a 4xx is final: one attempt, no retry


async def test_typesafe_close_drains_the_shared_pool(monkeypatch):
    provider = _typesafe_with(lambda request: httpx.Response(200), monkeypatch)
    pool = type(provider)._client
    assert pool is not None and not pool.is_closed
    await provider.close()
    assert pool.is_closed and type(provider)._client is None
    await provider.close()  # idempotent: nothing open, nothing raised


async def test_close_eval_provider_pools_closes_every_provider_once(
    monkeypatch,
):
    first, second = SimpleNamespace(close=AsyncMock()), SimpleNamespace(
        close=AsyncMock()
    )
    monkeypatch.setattr(providers, "PROVIDERS", {"a": first, "b": second})
    await providers.close_eval_provider_pools()
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


def test_engines_use_the_registered_provider_instances():
    # the closer drains PROVIDERS; an engine holding a private instance of a
    # per-instance-pool provider would escape it
    for engine in ENGINES.values():
        for name, provider in engine.providers.items():
            assert PROVIDERS[name] is provider


async def test_typesafe_judge_without_key_is_loud(monkeypatch):
    monkeypatch.setattr(typesafe, "TYPESAFE_API_KEY", "")
    with pytest.raises(typesafe.TypeSafeError, match="TYPESAFE_API_KEY"):
        await typesafe.TypeSafeProvider().call(_request())


async def test_typesafe_rejects_content_that_is_not_a_json_object(monkeypatch):
    provider = _typesafe_with(lambda request: httpx.Response(200), monkeypatch)
    bad = ProviderRequest(model="m", instruction=None, content="not json", settings={})
    with pytest.raises(typesafe.TypeSafeError, match="not JSON"):
        await provider.call(bad)


# --- the pipeline steps are the engine's: build_request and transform --------


def test_structured_build_request_and_transform_are_the_pipeline():
    engine = StructuredJudgeEngine()
    state = build_state(_voice_context())
    request = engine.build_request(state, EXAMPLE_CONFIGURATION)
    assert request.instruction is None and request.settings == {}
    assert json.loads(request.content) == {
        "state": state,
        "questions": _typesafe_questions(EXAMPLE_CONFIGURATION),
    }
    # transform = what gets stored; it is decide() over the vendor's answers
    response = ProviderResponse(
        text=json.dumps({"answers": RECORDED_ANSWERS, "model": "jev-1.13.0-b"}),
        model="jev-1.13.0-b",
    )
    verdict = engine.transform(response, EXAMPLE_CONFIGURATION)
    assert verdict.result == decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION).result
    assert verdict.model == "jev-1.13.0-b"
    with pytest.raises(json.JSONDecodeError):  # an unreadable reply is never stored
        engine.transform(
            ProviderResponse(text="nope", model="m"), EXAMPLE_CONFIGURATION
        )


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
        TEMPLATE_ID, "CONVERSATION_EVALS", {"engine": "structured"}
    )
    assert "UPDATE evaluation_config" in query
    assert "SET configuration = configuration || $3::jsonb" in query
    assert "INSERT" not in query.upper()
    assert values[:2] == [TEMPLATE_ID, "CONVERSATION_EVALS"]
    assert json.loads(values[2]) == {"engine": "structured"}


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
