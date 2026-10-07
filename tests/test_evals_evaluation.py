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

from app.api.routers.breeze_buddy.evaluations import handlers
from app.database.queries.breeze_buddy.evaluation_config import (
    OUTCOME_CORRECTNESS,
    get_evaluation_config_query,
    get_outcome_correctness_query,
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
from app.services.evals import engines
from app.services.evals.definition import (
    validate_evals_configuration,
)
from app.services.evals.engines import (
    ENGINES,
)
from app.services.evals.engines.common import (
    ChoiceResult,
    NoulResult,
    ScoreResult,
)
from app.services.evals.engines.prompt_judge_engine import (
    answer_schema as prompt_answer_schema,
    decide as prompt_decide,
    parse_questions as prompt_parse_questions,
)
from app.services.evals.engines.structured_judge_engine import (
    StructuredJudgeConfiguration,
    StructuredJudgeEngine,
    build_state,
    decide,
)
from app.services.model_provider import (
    OPENROUTER,
    PROVIDERS,
    GenerateRequest,
    GenerateResponse,
    GenerationSettings,
    ProviderConfig,
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

    # provider must be one the engine supports (structured: typesafe)
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["provider"] = "azure"
    with pytest.raises(ValueError, match="does not support provider"):
        validate_evals_configuration(config)

    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["surprise"] = True
    with pytest.raises(ValueError, match="unknown configuration keys"):
        validate_evals_configuration(config)


def test_instruction_and_settings_are_the_prompt_engines_keys():
    # instruction/settings brief the prompt engine's chat model: accepted as
    # written on a prompt row — even blank or misshapen, the engine reads
    # them leniently at run time — and unknown on a structured row
    config = copy.deepcopy(PROMPT_CONFIGURATION)
    config.update(instruction="   ", settings=["temperature", 0])
    decoded = validate_evals_configuration(config).model_dump()
    assert decoded["engine"] == "prompt"

    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config["settings"] = {"temperature": 0}
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
        providers = {"acme": SimpleNamespace(configuration_keys=frozenset())}
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

    # a list or dict choice cannot be looked up among the options (unhashable):
    # it costs that one answer, the rest of the verdict stands
    for bad in (["BUSY"], {"BUSY": 1}):
        answers = copy.deepcopy(RECORDED_ANSWERS)
        answers["verified_outcome"]["choice"] = bad
        results = {r.key: r for r in decide(answers, EXAMPLE_CONFIGURATION).result}
        assert results["verified_outcome"] == ChoiceResult(
            key="verified_outcome", label="Verified outcome", value=None, confidence=0.9
        )
        assert results["stt_accuracy"].value == 9.2


def test_decide_drops_answers_off_the_questions_scale():
    # a chat model is only ASKED to stay on the rubric; jev always does. An
    # off-scale answer is not an answer: that one result is None, the
    # verdict stands (the stored min/max would otherwise lie)
    answers = {
        "llm_accuracy": {"score": 10, "confidence": 0.5},  # 1-based on a 0..9 rubric
        "latency": {"score": -1, "confidence": 0.5},
        "loop_detection": {"score": 1, "confidence": 1.5},  # confidence off 0..1
        "verified_outcome": {"choice": "N/A", "confidence": 0.9},  # not an option
        "asked_for_human": {"noul": 1.2},
        "user_emotion": {"score": 9, "confidence": 1.0},  # the top level, in range
    }
    by_key = {
        r["key"]: r
        for r in decide(answers, EXAMPLE_CONFIGURATION).model_dump()["result"]
    }
    assert by_key["llm_accuracy"]["value"] is None
    assert by_key["latency"]["value"] is None
    assert by_key["loop_detection"]["value"] == 1
    assert by_key["loop_detection"]["confidence"] is None
    assert by_key["verified_outcome"]["value"] is None
    assert by_key["asked_for_human"]["value"] is None
    assert by_key["user_emotion"]["value"] == 10
    assert by_key["user_emotion"]["confidence"] == 1.0
    assert len(by_key) == len(EXAMPLE_CONFIGURATION["questions"])


def test_transform_refuses_a_reply_without_answers():
    # a valid JSON object in the wrong shape would decide({}) into a verdict
    # of Nones and be stored as "completed"; the contract requires `answers`,
    # so its absence means the reply was not read — raise, store nothing
    engine = StructuredJudgeEngine()
    for body in (
        {"llm_accuracy": {"score": 8, "confidence": 0.5}},  # no wrapper
        {"answers": {}},
        {"answers": "none"},
        [1, 2],
        None,  # a reply that was not JSON at all
    ):
        with pytest.raises(ValueError, match="no 'answers' object"):
            engine.transform(
                GenerateResponse(content=json.dumps(body), model="m", structured=body),
                EXAMPLE_CONFIGURATION,
            )


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

    async def generate(self, request):
        self.calls.append(request)
        return GenerateResponse(
            content=json.dumps(self.body),
            model=self.body["model"],
            structured=self.body,
        )

    async def close(self):
        return None


async def test_structured_evaluate_routes_to_the_configured_provider(monkeypatch):
    provider = FakeProvider({"answers": RECORDED_ANSWERS, "model": "jev-1.13.0-b"})
    monkeypatch.setattr(StructuredJudgeEngine, "providers", {"typesafe": provider})

    verdict = await StructuredJudgeEngine().evaluate(
        _voice_context(), EXAMPLE_CONFIGURATION
    )

    # one call, to the provider named in the config: a payload judge gets the
    # projected state and the typed questions as the payload, nothing else
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert request.input == {
        "state": build_state(_voice_context()),
        "questions": _typesafe_questions(EXAMPLE_CONFIGURATION),
    }
    assert request.model == "jev-1.13.0"
    assert request.system_prompt is None and request.schema is None
    # verdict is decide() over the body, with the model that actually served
    assert verdict.result == decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION).result
    assert verdict.model == "jev-1.13.0-b"


def test_engines_use_the_registered_provider_instances():
    # close_all drains PROVIDERS; an engine holding a private instance of a
    # provider would escape it
    for engine in ENGINES.values():
        for name, provider in engine.providers.items():
            assert PROVIDERS[name] is provider


# --- the pipeline steps are the engine's: build_request and transform ------


def test_structured_build_request_and_transform_are_the_pipeline():
    engine = StructuredJudgeEngine()
    state = build_state(_voice_context())
    request = engine.build_request(state, EXAMPLE_CONFIGURATION)
    # the payload a judge API reads natively: no prompt, no schema
    assert request.model == "jev-1.13.0"
    assert request.input == {
        "state": state,
        "questions": _typesafe_questions(EXAMPLE_CONFIGURATION),
    }
    assert request.system_prompt is None and request.schema is None
    # transform = what gets stored; it is decide() over the model's answers
    body = {"answers": RECORDED_ANSWERS, "model": "jev-1.13.0-b"}
    response = GenerateResponse(
        content=json.dumps(body), model="jev-1.13.0-b", structured=body
    )
    verdict = engine.transform(response, EXAMPLE_CONFIGURATION)
    assert verdict.result == decide(RECORDED_ANSWERS, EXAMPLE_CONFIGURATION).result
    assert verdict.model == "jev-1.13.0-b"


def test_structured_engine_is_served_by_typesafe_only():
    # criteria questions are read natively by jev; a chat model takes the
    # prompt engine instead
    config = copy.deepcopy(EXAMPLE_CONFIGURATION)
    config.update(provider="openrouter", model="openai/gpt-4o-mini")
    with pytest.raises(ValueError, match="does not support provider 'openrouter'"):
        validate_evals_configuration(config)


def _chat_reply(content, finish_reason="stop", model="openai/gpt-4o-mini-2024"):
    """One OpenAI-shaped chat completion as OpenRouter returns it."""
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _openrouter_with(handler, monkeypatch):
    """The registered OpenRouter provider on an httpx MockTransport."""
    monkeypatch.setattr(
        OPENROUTER,
        "_config",
        ProviderConfig(api_key="k", base_url="https://or.test", timeout_seconds=5),
    )
    monkeypatch.setattr(OPENROUTER, "_BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(
        OPENROUTER, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return OPENROUTER


def _sent_document(request: GenerateRequest) -> Dict[str, Any]:
    """The document a chat request carries as its user message."""
    assert isinstance(request.input, list) and len(request.input) == 1
    return json.loads(request.input[0].content)


# --- the prompt engine: the same questions, no criteria -----------------------

PROMPT_CONFIGURATION: Dict[str, Any] = {
    "engine": "prompt",
    "provider": "openrouter",
    "model": "openai/gpt-4o-mini",
    "questions": [
        {
            "key": "llm_accuracy",
            "type": "score",
            "label": "Llm accuracy",
            "instructions": (
                "Rate factual accuracy from 1 (fabricated facts or action lies) "
                "to 10 (every fact correct). Style deviations are not errors."
            ),
        },
        {
            "key": "verified_outcome",
            "type": "choice",
            "label": "Verified outcome",
            "instructions": (
                "The outcome this call should have had: CONFIRM, CANCEL, BUSY or OTHER."
            ),
        },
        {
            "key": "asked_for_human",
            "type": "noul",
            "label": "Asked for human",
            "instructions": "Did the customer ask to speak to a human agent?",
        },
    ],
}


def test_prompt_configuration_validates_without_criteria():
    # the decoded row, minus the optional scale a score may leave unset
    decoded = validate_evals_configuration(PROMPT_CONFIGURATION).model_dump(
        exclude_none=True
    )
    assert decoded == PROMPT_CONFIGURATION

    # criteria is refused: the scoring conditions belong in instructions
    config = copy.deepcopy(PROMPT_CONFIGURATION)
    config["questions"][0]["criteria"] = ["1 - bad", "2 - good"]
    with pytest.raises(ValueError, match="no criteria"):
        validate_evals_configuration(config)

    # so is any other key a question does not have
    config = copy.deepcopy(PROMPT_CONFIGURATION)
    config["questions"][0]["levels"] = 10
    with pytest.raises(ValueError, match="unknown question key 'levels'"):
        validate_evals_configuration(config)

    # chat models only: jev needs criteria arrays
    config = copy.deepcopy(PROMPT_CONFIGURATION)
    config["provider"] = "typesafe"
    config["model"] = "jev-1.13.0"
    with pytest.raises(ValueError, match="does not support provider 'typesafe'"):
        validate_evals_configuration(config)

    # the structured engine still demands criteria on its own rows
    config = copy.deepcopy(PROMPT_CONFIGURATION)
    config.update(engine="structured", provider="typesafe", model="jev-1.13.0")
    with pytest.raises(
        ValueError, match="question 'llm_accuracy': criteria is required"
    ):
        validate_evals_configuration(config)


def test_prompt_answer_schema_bounds_nothing():
    schema = json.loads(
        json.dumps(
            prompt_answer_schema(
                prompt_parse_questions(PROMPT_CONFIGURATION["questions"])
            )
        )
    )
    answers = schema["properties"]["answers"]
    assert answers["required"] == [
        "llm_accuracy",
        "verified_outcome",
        "asked_for_human",
    ]
    score = answers["properties"]["llm_accuracy"]["properties"]["score"]
    assert score["type"] == "number"
    assert "minimum" not in score and "maximum" not in score  # the scale is prose
    choice = answers["properties"]["verified_outcome"]["properties"]["choice"]
    assert "enum" not in choice  # the options are prose too
    assert answers["properties"]["llm_accuracy"]["required"] == ["score"]
    assert answers["properties"]["verified_outcome"]["required"] == ["choice"]
    assert answers["properties"]["asked_for_human"]["required"] == ["noul"]


def test_prompt_decide_stores_the_score_as_given_with_no_scale():
    answers = {
        "llm_accuracy": {"score": 7},
        "verified_outcome": {"choice": "BUSY"},
        "asked_for_human": {"noul": 0.2},
    }
    verdict = prompt_decide(answers, PROMPT_CONFIGURATION)
    assert verdict.result == [
        ScoreResult(
            key="llm_accuracy", label="Llm accuracy", value=7, min=None, max=None
        ),
        ChoiceResult(key="verified_outcome", label="Verified outcome", value="BUSY"),
        NoulResult(key="asked_for_human", label="Asked for human", value=0.2),
    ]
    # a confidence the model volunteers anyway is not read: none was asked for
    volunteered = {"llm_accuracy": {"score": 7, "confidence": 0.6}}
    first = prompt_decide(volunteered, PROMPT_CONFIGURATION).result[0]
    assert isinstance(first, ScoreResult) and first.confidence is None
    # the stored JSON says the scale explicitly: null, not missing
    stored = verdict.model_dump()["result"][0]
    assert stored["min"] is None and stored["max"] is None and "min" in stored

    # a malformed or missing answer costs that one result, never the verdict
    verdict = prompt_decide(
        {"llm_accuracy": {"score": "seven"}, "verified_outcome": {"choice": 3}},
        PROMPT_CONFIGURATION,
    )
    assert [result.value for result in verdict.result] == [None, None, None]


def test_prompt_decide_bounds_only_what_it_knows_and_refuses_no_answers():
    # the score scale is prose, so a score is stored as given; a noul is a
    # probability and 0..1 is the one scale this engine knows
    answers = {
        "llm_accuracy": {"score": 11},
        "verified_outcome": {"choice": "whatever"},
        "asked_for_human": {"noul": 1.2},
    }
    by_key = {
        r["key"]: r
        for r in prompt_decide(answers, PROMPT_CONFIGURATION).model_dump()["result"]
    }
    assert by_key["llm_accuracy"]["value"] == 11
    assert by_key["verified_outcome"]["value"] == "whatever"
    assert by_key["asked_for_human"]["value"] is None

    # NaN / Infinity parse as JSON here but JSONB refuses them: with no
    # declared scale to check against, _number itself must drop them
    body = json.loads(
        '{"llm_accuracy": {"score": NaN}, "asked_for_human": {"noul": Infinity}}'
    )
    stored = prompt_decide(body, PROMPT_CONFIGURATION).model_dump()["result"]
    assert stored[0]["value"] is None and stored[2]["value"] is None
    assert json.dumps(stored, allow_nan=False)  # what the result writer needs

    engine = ENGINES["prompt"]
    with pytest.raises(ValueError, match="no 'answers' object"):
        body = {"llm_accuracy": {"score": 8}}
        engine.transform(
            GenerateResponse(content=json.dumps(body), model="m", structured=body),
            PROMPT_CONFIGURATION,
        )


def test_prompt_score_may_declare_its_scale():
    # min/max are optional on a prompt score question; given together they
    # reach the contract, bound the answer and are stored; absent = null
    config = copy.deepcopy(PROMPT_CONFIGURATION)
    config["questions"][0].update(min=1, max=10)
    decoded = validate_evals_configuration(config).model_dump()
    assert decoded["questions"][0]["min"] == 1 and decoded["questions"][0]["max"] == 10

    schema = prompt_answer_schema(prompt_parse_questions(config["questions"]))
    score = json.loads(json.dumps(schema))["properties"]["answers"]["properties"][
        "llm_accuracy"
    ]["properties"]["score"]
    assert (score["minimum"], score["maximum"]) == (1, 10)

    stored = prompt_decide({"llm_accuracy": {"score": 7}}, config).model_dump()[
        "result"
    ]
    assert (stored[0]["value"], stored[0]["min"], stored[0]["max"]) == (7, 1, 10)
    stored = prompt_decide({"llm_accuracy": {"score": 11}}, config).model_dump()[
        "result"
    ]
    assert (
        stored[0]["value"] is None and stored[0]["max"] == 10
    )  # off the declared scale

    # the model sees the scale in the document too
    document = _sent_document(ENGINES["prompt"].build_request({}, config))
    sent = document["questions"]["llm_accuracy"]
    assert (sent["min"], sent["max"]) == (1, 10)
    assert "min" not in document["questions"]["verified_outcome"]

    # one without the other, or an empty range, is refused
    for bad in ({"min": 1}, {"max": 10}, {"min": 5, "max": 5}, {"min": 10, "max": 1}):
        config = copy.deepcopy(PROMPT_CONFIGURATION)
        config["questions"][0].update(bad)
        with pytest.raises(ValueError, match="min"):
            validate_evals_configuration(config)


_PREAMBLE_START = "You are an impartial evaluation judge."


async def test_prompt_on_openrouter_end_to_end(monkeypatch):
    answers = {
        "llm_accuracy": {"score": 8},
        "verified_outcome": {"choice": "CONFIRM"},
        "asked_for_human": {"noul": 0.05},
    }
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_reply(json.dumps({"answers": answers})))

    _openrouter_with(handler, monkeypatch)
    verdict = await ENGINES["prompt"].evaluate(_voice_context(), PROMPT_CONFIGURATION)

    # the document: type + instructions per question, no criteria anywhere
    system, user = seen["json"]["messages"]
    assert json.loads(user["content"])["questions"] == {
        q["key"]: {"type": q["type"], "instructions": q["instructions"]}
        for q in PROMPT_CONFIGURATION["questions"]
    }
    # the reply shape travels as structured output, never as prompt text
    assert system["content"].startswith(_PREAMBLE_START)
    assert '"answers"' not in system["content"]
    assert seen["json"]["response_format"]["type"] == "json_schema"
    schema = seen["json"]["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["answers"]
    # the same stored shape as every other engine
    assert (verdict.engine, verdict.provider) == ("prompt", "openrouter")
    assert verdict.model == "openai/gpt-4o-mini-2024"
    assert verdict.result[0] == ScoreResult(
        key="llm_accuracy", label="Llm accuracy", value=8, min=None, max=None
    )


def test_prompt_reads_its_options_leniently():
    # absent, blank/empty or the wrong shape = not provided; never an error
    schema = prompt_answer_schema(
        prompt_parse_questions(PROMPT_CONFIGURATION["questions"])
    )
    for options in (
        {},
        {"instruction": "   ", "settings": {}},
        {"instruction": ["x"], "settings": ["temperature", 0]},
    ):
        config = {**PROMPT_CONFIGURATION, **options}
        request = ENGINES["prompt"].build_request({}, config)
        assert request.system_prompt is not None
        assert request.system_prompt.startswith(_PREAMBLE_START)
        assert request.schema == schema
        # the default: a judge must be repeatable
        assert request.settings == GenerationSettings(temperature=0)
        assert request.extra is None


def test_prompt_briefs_its_chat_model():
    config = {
        **PROMPT_CONFIGURATION,
        "instruction": " Be strict. ",
        "settings": {
            "temperature": 0.5,
            "max_tokens": 9,
            "provider": {"order": ["openai"]},
            "seed": 7,
        },
    }
    state = build_state(_voice_context())
    request = ENGINES["prompt"].build_request(state, config)

    assert request.model == "openai/gpt-4o-mini"
    # the document is the one user message
    assert _sent_document(request) == {
        "state": json.loads(json.dumps(state)),
        "questions": {
            q["key"]: {"type": q["type"], "instructions": q["instructions"]}
            for q in PROMPT_CONFIGURATION["questions"]
        },
    }
    # the row's instruction is the whole system prompt; the reply contract
    # goes as the structured-output schema only
    schema = prompt_answer_schema(prompt_parse_questions(config["questions"]))
    assert request.schema == schema
    assert request.system_prompt == "Be strict."
    # the common knobs are typed; the rest passes through to the vendor
    assert request.settings == GenerationSettings(temperature=0.5, max_tokens=9)
    assert request.extra == {"provider": {"order": ["openai"]}, "seed": 7}


def test_an_answer_that_is_not_an_object_costs_that_one_result():
    # seen live: a small model replied "llm_accuracy": 8 instead of
    # {"score": 8} — that one result is None, the rest of the verdict stands
    verdict = prompt_decide(
        {"llm_accuracy": 8, "verified_outcome": {"choice": "BUSY"}},
        PROMPT_CONFIGURATION,
    )
    assert [result.value for result in verdict.result] == [None, "BUSY", None]
    verdict = decide({"llm_accuracy": 3, "latency": [1]}, EXAMPLE_CONFIGURATION)
    by_key = {result.key: result.value for result in verdict.result}
    assert by_key["llm_accuracy"] is None and by_key["latency"] is None


def test_structured_output_is_a_prompt_row_switch():
    # true / false / absent are valid; anything else is a 400
    for value in (True, False):
        config = {**PROMPT_CONFIGURATION, "structured_output": value}
        assert (
            validate_evals_configuration(config).model_dump()["structured_output"]
            is value
        )
    config = {**PROMPT_CONFIGURATION, "structured_output": "yes"}
    with pytest.raises(ValueError, match="structured_output must be true or false"):
        validate_evals_configuration(config)
    # the structured engine's model reads criteria natively: not its key
    config = {**EXAMPLE_CONFIGURATION, "structured_output": False}
    with pytest.raises(ValueError, match="unknown configuration keys"):
        validate_evals_configuration(config)


def test_structured_output_decides_how_the_contract_travels():
    schema = prompt_answer_schema(
        prompt_parse_questions(PROMPT_CONFIGURATION["questions"])
    )
    contract = json.dumps(schema)
    # absent or true: as the structured-output schema, never prompt text
    for options in ({}, {"structured_output": True}):
        request = ENGINES["prompt"].build_request(
            {}, {**PROMPT_CONFIGURATION, **options}
        )
        assert request.schema == schema
        assert request.system_prompt is not None
        assert contract not in request.system_prompt
    # false: no schema sent; the contract is spelled out in the system prompt
    request = ENGINES["prompt"].build_request(
        {}, {**PROMPT_CONFIGURATION, "structured_output": False, "instruction": "Hi."}
    )
    assert request.schema is None
    assert request.system_prompt is not None
    assert request.system_prompt.startswith("Hi.\n\n")
    assert request.system_prompt.endswith(contract)


async def test_prompt_without_structured_output_reads_the_reply_text(monkeypatch):
    config = {**PROMPT_CONFIGURATION, "structured_output": False}
    answers = {
        "llm_accuracy": {"score": 6},
        "verified_outcome": {"choice": "BUSY"},
        "asked_for_human": {"noul": 0.3},
    }
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        reply = "Sure:\n```json\n" + json.dumps({"answers": answers}) + "\n```"
        return httpx.Response(200, json=_chat_reply(reply))

    _openrouter_with(handler, monkeypatch)
    verdict = await ENGINES["prompt"].evaluate(_voice_context(), config)

    # one plain chat call: no structured output, no routing constraint
    assert "response_format" not in seen["json"] and "provider" not in seen["json"]
    assert [result.value for result in verdict.result] == [6, "BUSY", 0.3]

    # a reply with no readable JSON is not stored
    def prose(request):
        return httpx.Response(200, json=_chat_reply("The agent did fine."))

    _openrouter_with(prose, monkeypatch)
    with pytest.raises(ValueError, match="no 'answers' object"):
        await ENGINES["prompt"].evaluate(_voice_context(), config)


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
    # the per-type endpoint's row is the one named after its type
    assert (
        "VALUES ($1::uuid, $2::evaluation_type, lower($2::text), false, $3::jsonb)"
        in query
    )
    assert "ON CONFLICT (template_id, name)" in query
    # an existing row: configuration replaced wholesale, enabled untouched
    assert "DO UPDATE SET configuration = EXCLUDED.configuration" in query
    assert "EXCLUDED.enabled" not in query
    assert values[:2] == [TEMPLATE_ID, "CONVERSATION_EVALS"]
    assert json.loads(values[2]) == EXAMPLE_CONFIGURATION


def test_the_preset_outcome_eval_is_a_default_an_agent_row_overrides():
    query, values = get_outcome_correctness_query(TEMPLATE_ID)
    # the preset row (no template) holds the engine, model and threshold ...
    assert "builtin.template_id IS NULL" in query
    # ... and the agent's own row of that name, when there is one, decides
    # whether it runs, either way; without one the preset row's flag does
    assert "LEFT JOIN evaluation_config own" in query
    assert "COALESCE(own.enabled, builtin.enabled)" in query
    assert values == [TEMPLATE_ID, OUTCOME_CORRECTNESS]


def test_the_preset_outcome_eval_is_seeded_off():
    migration = Path(
        "app/database/migrations/083_evaluation_config_names.sql"
    ).read_text()
    seed = migration[migration.index("INSERT INTO evaluation_config") :]
    assert "'outcome_correctness',\n    false," in seed


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
