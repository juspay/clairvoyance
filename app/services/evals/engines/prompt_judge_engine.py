"""PromptJudgeEngine — the same typed questions as the structured judge
(score / choice / noul) WITHOUT criteria: the scoring conditions are written
into each question's ``instructions`` and the model is held to them by
text alone. For chat models; a vendor that needs criteria arrays (jev)
cannot serve it, so its providers are the chat ones.

What differs from the structured judge, and nothing else:
  - a question carries no ``criteria`` (one present is refused, so a
    structured rubric is never silently ignored);
  - the reply contract bounds nothing: a score is a number on whatever
    scale the instructions define, a choice is whatever option they name;
  - a score question MAY declare its scale (``min``/``max``, together);
    when it does the contract tells the model the bounds, an off-scale
    score is not an answer, and the stored result carries them. When it
    does not, the scale lives in the instructions alone: stored
    ``min``/``max`` are null and the value is stored as the model gave it;
    no level mapping either way;
  - no confidence is asked for or read: a chat model's self-reported
    confidence is a guess, so the stored ``confidence`` stays null.
Same document, same ``Verdict`` shape, same pipeline.

The engine briefs its chat model itself: the system prompt is the row's
``instruction`` (or a neutral preamble); the document is the user message;
the row's ``settings`` are the call options. How the reply contract
travels is the row's ``structured_output``:
  - true or absent: as ``schema`` (structured output, enforced by the
    endpoint; a model without it fails loudly) and the reply comes back
    parsed. Strict structured outputs drop bounds, so ``decide`` still
    checks them;
  - false: spelled out in the system prompt for a model without structured
    outputs, and the JSON is read from the reply text.
``instruction`` and ``settings`` are read leniently: absent, blank/empty or
of the wrong shape = not provided, never an error, so a bad option never
blocks an evaluation.
"""

import json
import math
import re
from typing import (
    Annotated,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
)
from app.services.model_provider import (
    OPENROUTER,
    GenerateRequest,
    GenerateResponse,
    GenerationSettings,
    Message,
)

__all__ = [
    "ALLOWED_QUESTION_TYPES",
    "PromptChoiceQuestion",
    "PromptJudgeConfiguration",
    "PromptJudgeEngine",
    "PromptNoulQuestion",
    "PromptQuestion",
    "PromptScoreQuestion",
    "answer_schema",
    "decide",
    "parse_questions",
]

ALLOWED_QUESTION_TYPES = frozenset({"score", "choice", "noul"})

# the system prompt when the row gives no instruction; says nothing about
# the questions — those travel in the document
_PREAMBLE = (
    "You are an impartial evaluation judge. The user message is a document "
    "to evaluate together with the questions to answer about it. Judge "
    "strictly from the document; do not assume anything it does not show."
)
# the reply contract as text, for a row with structured_output false
_SCHEMA_RULE = (
    "Reply with exactly one JSON object that is valid against the following "
    "JSON Schema. No prose, no markdown, nothing before or after the object:"
)
# a reply wrapped in a markdown fence (```json ... ```), anywhere in the text
_FENCED = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)
# a judge must be repeatable; the row's settings may override
_DEFAULT_TEMPERATURE = 0.0


def _non_empty(field: str):
    def check(value: str) -> str:
        if not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value

    return check


class _PromptQuestion(BaseModel):
    # a key the engine does not read (``criteria`` above all) is refused:
    # the scoring conditions belong in ``instructions`` here
    model_config = ConfigDict(extra="forbid")

    # the question's identity: the stored result's ``key``; unique within a
    # configuration
    key: str
    # the display name; it travels into the stored result as ``label``
    label: str
    # the question AND its scoring conditions (the scale, the options, what
    # yes means) — the only place the model learns how to answer
    instructions: str

    _key = field_validator("key")(_non_empty("key"))
    _label = field_validator("label")(_non_empty("label"))
    _instructions = field_validator("instructions")(_non_empty("instructions"))


class PromptScoreQuestion(_PromptQuestion):
    type: Literal["score"]
    # the scale, optional and given together; absent = the instructions
    # alone define it and the stored result says null
    min: Optional[float] = None
    max: Optional[float] = None

    @model_validator(mode="after")
    def _scale(self) -> "PromptScoreQuestion":
        if (self.min is None) != (self.max is None):
            raise ValueError("min and max must be given together")
        if self.min is not None and self.max is not None and not self.min < self.max:
            raise ValueError("min must be less than max")
        return self


class PromptChoiceQuestion(_PromptQuestion):
    type: Literal["choice"]


class PromptNoulQuestion(_PromptQuestion):
    type: Literal["noul"]


PromptQuestion = Annotated[
    Union[PromptScoreQuestion, PromptChoiceQuestion, PromptNoulQuestion],
    Field(discriminator="type"),
]


class _Questions(BaseModel):
    questions: List[PromptQuestion]


def parse_questions(raw: object) -> Dict[str, PromptQuestion]:
    """The configuration's ``questions`` array as typed questions, keyed by
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
        if "criteria" in question:
            raise ValueError(
                f"question {question.get('key', index)!r}: the prompt engine takes "
                "no criteria; write the scoring conditions in instructions"
            )
    try:
        parsed = _Questions(questions=raw).questions
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = first["loc"]
        index = loc[1] if len(loc) > 1 and isinstance(loc[1], int) else None
        name = raw[index].get("key", index) if index is not None else "?"
        detail = first["msg"].removeprefix("Value error, ")
        if first["type"] == "extra_forbidden":
            detail = f"unknown question key {loc[-1]!r}"
        raise ValueError(f"question {name!r}: {detail}") from exc
    by_key: Dict[str, PromptQuestion] = {}
    for question in parsed:
        if question.key in by_key:
            raise ValueError(f"duplicate question key {question.key!r}")
        by_key[question.key] = question
    return by_key


class PromptJudgeConfiguration(BaseModel):
    """This engine's configuration row, decoded in ``validate_configuration``:
    the envelope (engine, provider, model) plus ``questions`` without
    criteria. The row is stored as the admin sent it."""

    engine: Literal["prompt"]
    provider: str
    model: str
    questions: List[PromptQuestion]
    # the reply contract as structured output (true, or absent) or as prompt
    # text (false) — for a model without structured outputs
    structured_output: Optional[bool] = None

    @field_validator("questions", mode="before")
    @classmethod
    def _questions(cls, raw: object) -> object:
        parse_questions(raw)  # the array rules, with readable messages
        return raw

    @field_validator("structured_output", mode="before")
    @classmethod
    def _structured_output(cls, raw: object) -> object:
        if raw is not None and not isinstance(raw, bool):
            raise ValueError("structured_output must be true or false")
        return raw


def answer_schema(questions: Mapping[str, PromptQuestion]) -> Dict[str, object]:
    """PURE: the JSON Schema of the reply ``transform`` reads — one answer
    per question key. Unlike the structured judge's, it bounds nothing (the
    scale and the options are in each question's instructions) and asks
    for no confidence."""
    answers: Dict[str, object] = {}
    for key, question in questions.items():
        if isinstance(question, PromptScoreQuestion):
            score: Dict[str, object] = {
                "type": "number",
                "description": (
                    "the score, on the scale the question's instructions define"
                ),
            }
            if question.min is not None and question.max is not None:
                score.update(minimum=question.min, maximum=question.max)
            answers[key] = {
                "type": "object",
                "properties": {"score": score},
                "required": ["score"],
            }
        elif isinstance(question, PromptChoiceQuestion):
            answers[key] = {
                "type": "object",
                "properties": {
                    "choice": {
                        "type": "string",
                        "description": (
                            "the one option, named in the question's instructions, "
                            "that fits best"
                        ),
                    },
                },
                "required": ["choice"],
            }
        else:
            answers[key] = {
                "type": "object",
                "properties": {
                    "noul": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "the probability that the answer is yes",
                    }
                },
                "required": ["noul"],
            }
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "description": "one answer per question, under the question's key",
                "properties": answers,
                "required": list(answers),
            }
        },
        "required": ["answers"],
    }


def _number(value: Any) -> Optional[float]:
    """A model number, or None — a string, object, bool, NaN or infinity in
    a numeric slot must cost that one answer, never the whole verdict
    (``json.loads`` accepts the NaN/Infinity tokens; JSONB does not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _instruction(raw: object) -> Optional[str]:
    """The row's instruction, or None when absent, blank or not a string."""
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _settings(raw: object) -> Tuple[GenerationSettings, Dict[str, object]]:
    """The row's ``settings``: the common knobs as ``GenerationSettings``
    (temperature defaults to 0), everything else passed through as the
    vendor's own fields (``provider`` routing, ``seed``, ...)."""
    values: Dict[str, Any] = (
        {str(key): value for key, value in raw.items()}
        if isinstance(raw, Mapping)
        else {}
    )
    temperature = _number(values.pop("temperature", None))
    max_tokens = values.pop("max_tokens", None)
    stop = values.pop("stop", None)
    settings = GenerationSettings(
        temperature=_DEFAULT_TEMPERATURE if temperature is None else temperature,
        top_p=_number(values.pop("top_p", None)),
        max_tokens=(
            max_tokens
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool)
            else None
        ),
        stop=(
            stop
            if isinstance(stop, list) and all(isinstance(s, str) for s in stop)
            else None
        ),
    )
    return settings, values


def _within(value: Optional[float], low: float, high: float) -> Optional[float]:
    """The number if it is on the scale, else None."""
    return value if value is not None and low <= value <= high else None


def decide(
    answers: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> Verdict:
    """PURE: answers in, the stored ``Verdict`` out. No I/O.

    One typed result per configured question. A score is stored as the
    model gave it, with the question's ``min``/``max`` when it declared a
    scale (an off-scale score is then None) and null when it did not — the
    scale is in the instructions; a choice as the option text
    the model picked; a noul as P(yes), and only in 0..1 — the one scale
    this engine knows. No confidence: none is asked for, so none is read
    (stored null). A missing, non-numeric or off-scale answer costs that
    one result (None), never the verdict.
    """
    questions = parse_questions(configuration["questions"])
    results: List[Result] = []
    for key, question in questions.items():
        answer = answers.get(key)
        if not isinstance(answer, Mapping):  # e.g. a bare number: not an answer
            answer = {}
        if isinstance(question, PromptNoulQuestion):
            results.append(
                NoulResult(
                    key=key,
                    label=question.label,
                    value=_within(_number(answer.get("noul")), 0, 1),
                )
            )
        elif isinstance(question, PromptChoiceQuestion):
            choice = answer.get("choice")
            results.append(
                ChoiceResult(
                    key=key,
                    label=question.label,
                    value=choice if isinstance(choice, str) else None,
                )
            )
        else:
            results.append(
                ScoreResult(
                    key=key,
                    label=question.label,
                    value=(
                        _within(
                            _number(answer.get("score")), question.min, question.max
                        )
                        if question.min is not None and question.max is not None
                        else _number(answer.get("score"))
                    ),
                    min=question.min,
                    max=question.max,
                )
            )
    return Verdict(
        engine=str(configuration.get("engine")),
        provider=str(configuration.get("provider")),
        model=str(configuration.get("model")),
        result=results,
    )


class PromptJudgeEngine(EvalEngine):
    name = "prompt"
    channels = frozenset({ConversationChannel.VOICE, ConversationChannel.CHAT})
    # chat models only: a rubric written in prose has no criteria array for jev
    providers = {"openrouter": OPENROUTER}
    configuration_keys = frozenset(
        {"questions", "instruction", "settings", "structured_output"}
    )

    def validate_configuration(
        self, configuration: Mapping[str, object]
    ) -> PromptJudgeConfiguration:
        """Questions of type score/choice/noul with key, label and
        instructions — and no criteria. Raises ``ValueError``."""
        try:
            return PromptJudgeConfiguration.model_validate(configuration)
        except ValidationError as exc:
            raise ValueError(
                exc.errors()[0]["msg"].removeprefix("Value error, ")
            ) from exc

    def build_request(
        self, state: Dict[str, Any], configuration: Mapping[str, Any]
    ) -> GenerateRequest:
        # the document (the state and the questions keyed by question key:
        # type + instructions; ``key`` and ``label`` are ours and stay home)
        # as the user message, briefed by the system prompt
        parsed = parse_questions(configuration["questions"])
        questions = {
            key: question.model_dump(exclude={"key", "label"}, exclude_none=True)
            for key, question in parsed.items()
        }
        document = {"state": state, "questions": questions}
        schema = answer_schema(parsed)
        system_prompt = _instruction(configuration.get("instruction")) or _PREAMBLE
        structured = configuration.get("structured_output") is not False
        if not structured:
            contract = json.dumps(schema, ensure_ascii=False)
            system_prompt = f"{system_prompt}\n\n{_SCHEMA_RULE}\n{contract}"
        settings, extra = _settings(configuration.get("settings"))
        return GenerateRequest(
            model=str(configuration["model"]),
            input=[
                Message(role="user", content=json.dumps(document, ensure_ascii=False))
            ],
            system_prompt=system_prompt,
            schema=schema if structured else None,
            settings=settings,
            extra=extra or None,
        )

    def transform(
        self, response: GenerateResponse, configuration: Mapping[str, Any]
    ) -> Verdict:
        body = response.structured
        if body is None:  # structured_output false: the JSON is in the text
            match = _FENCED.search(response.content)
            text = (match.group(1) if match else response.content).strip()
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = None
        # the contract makes ``answers`` required: a reply without it was not
        # read, and an unread reply is never stored as a verdict of Nones
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict) or not answers:
            raise ValueError(f"reply has no 'answers' object: {response.content[:300]}")
        verdict = decide(answers, configuration)
        # the model that actually served (the requested one is in the config row)
        return verdict.model_copy(update={"model": response.model or verdict.model})
