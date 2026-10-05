"""StructuredJudgeEngine — typed questions (score / choice / noul) sent as
one structured JSON document, answered in one structured JSON shape. Owns
that question vocabulary (System One's) and the shape of the answers.

Served by a judge API that reads these questions natively and answers in
this shape: TypeSafe (model jev-*). The document is posted as its payload;
no prompt, no schema. A chat model is the prompt judge's.

The engine is a thin sandwich around PURE functions: ``build_state`` (the
projection the model sees — shared with every judge engine, in
``common``) and ``decide`` (answers in, verdict out). The questions and pinned model arrive in the
``configuration`` argument from the agent's evaluation_config row; this
module hardcodes no definition and names no question. The pure halves are
golden-tested with recorded data — the model is never in a unit test.
"""

from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Union

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel
from app.services.evals.engines.base import (
    EvalEngine,
)
from app.services.evals.engines.common import (
    ChoiceResult,
    NoulResult,
    Result,
    ScoreResult,
    Verdict,
    build_state,
)
from app.services.model_provider import TYPESAFE, GenerateRequest, GenerateResponse

__all__ = [
    "ALLOWED_QUESTION_TYPES",
    "ChoiceQuestion",
    "NoulQuestion",
    "Question",
    "ScoreQuestion",
    "StructuredJudgeConfiguration",
    "StructuredJudgeEngine",
    "build_state",
    "decide",
    "levels_of",
    "parse_questions",
]

ALLOWED_QUESTION_TYPES = frozenset({"score", "choice", "noul"})

# --- the question primitives: THIS engine's vocabulary --------------------------
# System One's: score (a described rubric of 2-10 levels), choice (described
# options), noul (a yes/no with optional descriptions). The models carry the
# rules; the messages are what the configuration API shows an admin.


def _instructions(value: str) -> str:
    if not value.strip():
        raise ValueError("instructions must be a non-empty string")
    return value


def _label(value: str) -> str:
    if not value.strip():
        raise ValueError("label must be a non-empty string")
    return value


def _key(value: str) -> str:
    if not value.strip():
        raise ValueError("key must be a non-empty string")
    return value


class ScoreQuestion(BaseModel):
    type: Literal["score"]
    # the question's identity: the stored result's ``key``, the vendor's
    # answer id; unique within a configuration
    key: str
    # the display name; it travels into the stored result as ``label``
    label: str
    instructions: str
    criteria: List[str]

    _instructions = field_validator("instructions")(_instructions)
    _label = field_validator("label")(_label)
    _key = field_validator("key")(_key)

    @field_validator("criteria")
    @classmethod
    def _levels(cls, criteria: List[str]) -> List[str]:
        if not 2 <= len(criteria) <= 10 or not all(level.strip() for level in criteria):
            raise ValueError("score criteria must be 2-10 non-empty strings")
        return criteria


class ChoiceQuestion(BaseModel):
    type: Literal["choice"]
    # the question's identity: the stored result's ``key``, the vendor's
    # answer id; unique within a configuration
    key: str
    # the display name; it travels into the stored result as ``label``
    label: str
    instructions: str
    criteria: Dict[str, str]

    _instructions = field_validator("instructions")(_instructions)
    _label = field_validator("label")(_label)
    _key = field_validator("key")(_key)

    @field_validator("criteria")
    @classmethod
    def _described(cls, criteria: Dict[str, str]) -> Dict[str, str]:
        if not criteria:
            raise ValueError(
                "choice criteria must be a non-empty object of option -> description"
            )
        for option, description in criteria.items():
            if not description.strip():
                raise ValueError(f"option {option!r} needs a description")
        return criteria


class NoulQuestion(BaseModel):
    type: Literal["noul"]
    # the question's identity: the stored result's ``key``, the vendor's
    # answer id; unique within a configuration
    key: str
    # the display name; it travels into the stored result as ``label``
    label: str
    instructions: str
    criteria: Optional[Dict[str, str]] = None

    _instructions = field_validator("instructions")(_instructions)
    _label = field_validator("label")(_label)
    _key = field_validator("key")(_key)

    @field_validator("criteria")
    @classmethod
    def _true_false(
        cls, criteria: Optional[Dict[str, str]]
    ) -> Optional[Dict[str, str]]:
        if criteria is not None and (
            set(criteria) - {"true", "false"}
            or not all(text.strip() for text in criteria.values())
        ):
            raise ValueError(
                "noul criteria, if given, must be an object with 'true'/'false' "
                "descriptions"
            )
        return criteria


Question = Annotated[
    Union[ScoreQuestion, ChoiceQuestion, NoulQuestion], Field(discriminator="type")
]


class _Questions(BaseModel):
    questions: List[Question]


def parse_questions(raw: object) -> Dict[str, Question]:
    """The configuration's ``questions`` array as typed primitives, keyed by
    each question's ``key`` in configuration order — or a ``ValueError``
    whose message names the question and the rule."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("questions must be a non-empty array")
    for index, question in enumerate(raw):
        if not isinstance(question, dict):
            raise ValueError(f"question #{index} must be an object")
        question_type = question.get("type")
        if (
            not isinstance(question_type, str)
            or question_type not in ALLOWED_QUESTION_TYPES
        ):
            raise ValueError(
                f"question {question.get('key', index)!r}: type must be one of "
                f"{sorted(ALLOWED_QUESTION_TYPES)}"
            )
    try:
        parsed = _Questions(questions=raw).questions
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = first["loc"]
        index = loc[1] if len(loc) > 1 and isinstance(loc[1], int) else None
        name = raw[index].get("key", index) if index is not None else "?"
        detail = first["msg"].removeprefix("Value error, ")
        if first["type"] == "missing":  # "Field required" alone names no field
            detail = f"{loc[-1]} is required"
        raise ValueError(f"question {name!r}: {detail}") from exc
    by_key: Dict[str, Question] = {}
    for question in parsed:
        if question.key in by_key:
            raise ValueError(f"duplicate question key {question.key!r}")
        by_key[question.key] = question
    return by_key


class StructuredJudgeConfiguration(BaseModel):
    """This engine's configuration row, decoded in ``validate_configuration``:
    the envelope (engine, provider, model) plus ``questions``. The row is
    stored as the admin sent it."""

    engine: Literal["structured"]
    provider: str
    model: str
    questions: List[Question]

    @field_validator("questions", mode="before")
    @classmethod
    def _questions(cls, raw: object) -> object:
        parse_questions(raw)  # the array rules, with readable messages
        return raw


def levels_of(question: Question) -> int:
    """How many levels a score rubric has; 0 for the other primitives."""
    return len(question.criteria) if isinstance(question, ScoreQuestion) else 0


def _number(value: Any) -> Optional[float]:
    """A vendor number, or None — a string, object or bool in a numeric slot
    must cost that one answer, never the whole verdict."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _within(value: Optional[float], low: float, high: float) -> Optional[float]:
    """The number if it is on the scale the question defined, else None."""
    return value if value is not None and low <= value <= high else None


def decide(
    answers: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> Verdict:
    """PURE: answers in, the stored ``Verdict`` out. No I/O.

    Names no question. Every question in the configuration gets one typed
    result, by its primitive:
      score  -> ScoreResult   (a 2-level rubric is binary and stays 0-1;
                               longer rubrics map level+1 onto 1..N — the
                               result says its own scale)
      choice -> ChoiceResult  (the option picked)
      noul   -> NoulResult  (P(yes) in 0-1; no separate confidence — the
                               value is the answer)
    A non-numeric or out-of-range score, confidence or noul, or a choice
    that is not one of the rubric's options, costs that one answer (stored
    as None), never the verdict: the scale is the question's, and a value
    off it is not an answer. Everything else the vendor returns (legend,
    raw position, probability distribution) is dropped: the legend is the
    config's own text, the rest is summarised by the value + confidence.
    """
    questions: Dict[str, Question] = parse_questions(configuration["questions"])
    results: List[Result] = []
    for question_id, question in questions.items():
        answer = answers.get(question_id)
        if not isinstance(answer, Mapping):  # e.g. a bare number: not an answer
            answer = {}
        if isinstance(question, NoulQuestion):
            results.append(
                NoulResult(
                    key=question_id,
                    label=question.label,
                    value=_within(_number(answer.get("noul")), 0, 1),
                )
            )
        elif isinstance(question, ChoiceQuestion):
            choice = answer.get("choice")
            results.append(
                ChoiceResult(
                    key=question_id,
                    label=question.label,
                    value=(
                        choice
                        if isinstance(choice, str) and choice in question.criteria
                        else None
                    ),
                    confidence=_within(_number(answer.get("confidence")), 0, 1),
                )
            )
        else:
            levels = levels_of(question)
            raw = _within(_number(answer.get("score")), 0, levels - 1)
            binary = levels == 2
            results.append(
                ScoreResult(
                    key=question_id,
                    label=question.label,
                    value=(
                        round(raw if binary else raw + 1, 2)
                        if raw is not None
                        else None
                    ),
                    min=0 if binary else 1,
                    max=1 if binary else levels,
                    confidence=_within(_number(answer.get("confidence")), 0, 1),
                )
            )

    return Verdict(
        engine=str(configuration.get("engine")),
        provider=str(configuration.get("provider")),
        model=str(configuration.get("model")),
        result=results,
    )


class StructuredJudgeEngine(EvalEngine):
    name = "structured"
    channels = frozenset({ConversationChannel.VOICE, ConversationChannel.CHAT})
    # System One reads the questions natively
    providers = {"typesafe": TYPESAFE}
    configuration_keys = frozenset({"questions"})

    def validate_configuration(
        self, configuration: Mapping[str, object]
    ) -> StructuredJudgeConfiguration:
        """The question primitives (score rubrics of 2-10 described levels,
        choice with every option described, noul with optional true/false
        descriptions) — System One's own vocabulary. Raises ``ValueError``."""
        try:
            return StructuredJudgeConfiguration.model_validate(configuration)
        except ValidationError as exc:
            raise ValueError(
                exc.errors()[0]["msg"].removeprefix("Value error, ")
            ) from exc

    def build_request(
        self, state: Dict[str, Any], configuration: Mapping[str, Any]
    ) -> GenerateRequest:
        # the state and the typed questions, posted as the payload. The model
        # gets the primitives keyed by question key; ``key`` and ``label``
        # are ours and stay home.
        parsed = parse_questions(configuration["questions"])
        questions = {
            key: question.model_dump(exclude={"key", "label"}, exclude_none=True)
            for key, question in parsed.items()
        }
        return GenerateRequest(
            model=str(configuration["model"]),
            input={"state": state, "questions": questions},
        )

    def transform(
        self, response: GenerateResponse, configuration: Mapping[str, Any]
    ) -> Verdict:
        body = response.structured
        # the contract makes ``answers`` required: a reply without it was not
        # read, and an unread reply is never stored as a verdict of Nones
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict) or not answers:
            raise ValueError(f"reply has no 'answers' object: {response.content[:300]}")
        verdict = decide(answers, configuration)
        # the model that actually served (the requested one is in the config row)
        return verdict.model_copy(update={"model": response.model or verdict.model})
