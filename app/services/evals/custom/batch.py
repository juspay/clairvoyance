"""Run several of a conversation's evals at once.

An agent can have many evals (CONVERSATION_EVALS rows, each named). Evals
one judge request can answer together go out as one request: the
structured engine on the same provider and model. TypeSafe Jev answers any
number of independent questions in one request, and batching keeps an agent
with many evals at one request per call (the vendor's rate limit counts
requests). In the shared request each question's key carries its eval's
name as a prefix; the answers are split back into one verdict per eval,
stored under the eval's name.

A custom eval only stores its results: it never changes the call's
outcome (only the end-of-call outcome check, a preset eval, does).

Only structured (Jev) evals run for now. Free-form evals (the prompt
engine) come later, with function calling to hold their answers to a
schema; until then they are skipped.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.logger import logger
from app.database.queries.breeze_buddy.evaluation_config import NOT_CUSTOM_EVALS
from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel
from app.schemas.breeze_buddy.evals import EvaluationType
from app.services.evals.engines.common import Verdict
from app.services.evals.evaluator import get_engine, run_evaluation, save_verdict
from app.utils.common import parse_json

_STRUCTURED = "structured"
# eval names never hold a dot (EVAL_NAME_PATTERN), so "<name>." splits
# unambiguously
_SEPARATOR = "."


def agent_evals(
    evaluations: Optional[Sequence[Mapping[str, Any]]],
) -> List[Mapping[str, Any]]:
    """The agent's own evals among its enabled rows: CONVERSATION_EVALS rows
    other than the built-in outcome_correctness (it runs at the end of the
    call instead) and the per-type endpoints' row (NOT_CUSTOM_EVALS)."""
    return [
        row
        for row in evaluations or []
        if row.get("evaluation_type") == EvaluationType.CONVERSATION_EVALS.value
        and row.get("name") not in NOT_CUSTOM_EVALS
    ]


def batches(
    evaluations: Sequence[Mapping[str, Any]],
) -> List[List[Mapping[str, Any]]]:
    """``evaluations`` grouped into judge requests: structured evals on the
    same provider and model share one. Other engines are skipped for now."""
    grouped: Dict[Tuple[Any, Any], List[Mapping[str, Any]]] = {}
    for row in evaluations:
        configuration = parse_json(row, "configuration") or {}
        if configuration.get("engine") != _STRUCTURED:
            logger.info(
                f"Eval {row.get('name')!r} not run: engine "
                f"{configuration.get('engine')!r} evals come later"
            )
            continue
        key = (configuration.get("provider"), configuration.get("model"))
        grouped.setdefault(key, []).append(row)
    return list(grouped.values())


async def run_batch(
    batch: Sequence[Mapping[str, Any]],
    context: Dict[str, Any],
    channel: ConversationChannel,
) -> None:
    """Judge one batch in one request and store each eval's verdict under its
    name. Raises on any failure; nothing of the batch is stored then."""
    configurations = [parse_json(row, "configuration") or {} for row in batch]
    engine = get_engine(configurations[0].get("engine"))
    if channel not in engine.channels:
        return
    questions = [
        {**question, "key": f"{row['name']}{_SEPARATOR}{question['key']}"}
        for row, configuration in zip(batch, configurations)
        for question in configuration.get("questions") or []
    ]
    verdict = await run_evaluation(
        engine, context, {**configurations[0], "questions": questions}
    )
    for row in batch:
        prefix = f"{row['name']}{_SEPARATOR}"
        own = Verdict(
            engine=verdict.engine,
            provider=verdict.provider,
            model=verdict.model,
            result=[
                result.model_copy(update={"key": result.key[len(prefix) :]})
                for result in verdict.result
                if result.key.startswith(prefix)
            ],
        )
        await save_verdict(
            str(row["id"]), str(row["evaluation_type"]), context, own, str(row["name"])
        )
