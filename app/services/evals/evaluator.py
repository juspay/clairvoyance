"""The eval adapter — the topics-evaluator slot, engine-pluggable, for any
evaluation type: the row it is handed says which.

Three steps, each callable on its own:

  get_engine      the engine a configuration names
  run_evaluation  run it once under a hard time bound (retries are the
                  provider's job) and return the ``Verdict`` — nothing stored
  save_verdict    store one evaluation_result row through the existing
                  topics insert (the verdict as a one-element array)

``analyze_evals`` composes them for a finished conversation: resolve, gate
on the channels the engine supports, run, store. Scores go to the database
ONLY — never to Langfuse. Its fail posture is read-only analytics: any
failure logs and skips, it never blocks the call record. A caller that
only wants the verdict uses ``get_engine`` + ``run_evaluation`` and owns
its own fail posture (they raise).
"""

import asyncio
import time
from typing import Any, Dict, Mapping

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_results,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
)
from app.services.evals.engines import (
    ENGINES,
)
from app.services.evals.engines.base import EvalEngine
from app.services.evals.engines.common import Verdict
from app.utils.common import parse_json

# One attempt here: retries live in the provider (3 attempts on 429/5xx/
# transport errors only, with backoff: TypeSafe 3 x 30 s, OpenRouter 3 x 60 s).
# This bound covers the slowest provider's worst case.
_EVALUATION_TIMEOUT_SECONDS = 200


def get_engine(name: object) -> EvalEngine:
    """The engine registered under ``name`` (a configuration's ``engine``).
    Raises ``ValueError`` for an unknown or non-string name."""
    engine = ENGINES.get(name) if isinstance(name, str) else None
    if engine is None:
        raise ValueError(f"unknown engine {name!r}; available: {sorted(ENGINES)}")
    return engine


async def run_evaluation(
    engine: EvalEngine,
    context: Dict[str, Any],
    configuration: Dict[str, Any],
) -> Verdict:
    """Run ``engine`` once on ``context`` and return its ``Verdict``; nothing
    is stored. Bounded by ``_EVALUATION_TIMEOUT_SECONDS``. Raises on any
    failure (timeout, provider error, unreadable reply)."""
    return await asyncio.wait_for(
        engine.evaluate(context, configuration),
        timeout=_EVALUATION_TIMEOUT_SECONDS,
    )


async def save_verdict(
    evaluation_id: str,
    evaluation_type: str,
    context: Mapping[str, Any],
    verdict: Verdict,
) -> None:
    """Store ``verdict`` as one evaluation_result row for the conversation
    in ``context`` (source_id, reseller_id, merchant_id, template_id,
    started_at). ``evaluation_type`` is the result column's label."""
    # the identity CHECK compares ``type`` against metadata->>'type', so it
    # rides in the stored JSON too
    stored = {"type": evaluation_type, **verdict.model_dump()}
    await save_evaluation_results(
        evaluation_id,
        evaluation_type,
        context["source_id"],
        context["reseller_id"],
        context.get("merchant_id"),
        str(context["template_id"]),
        context["started_at"],
        [stored],  # the topics insert takes an array; one verdict = one row
    )


async def analyze_evals(
    context: Dict[str, Any],
    evaluation: Dict[str, Any],
    channel: ConversationChannel,
) -> None:
    """Evaluate a finished conversation with the engine its evaluation row
    names and store the verdict. Never raises: every failure logs and skips."""
    source_id = context["source_id"]
    # the row says which evaluation type this is: it names the result
    # column's label and the stored ``type`` — this package never does
    evaluation_type = str(evaluation["evaluation_type"])
    # asyncpg hands jsonb back as text; parse_json takes either form
    configuration = parse_json(evaluation, "configuration") or {}

    try:
        engine = get_engine(configuration.get("engine"))
    except ValueError as exc:
        logger.warning(f"{evaluation_type} evaluation {source_id}: {exc}, skipping")
        return
    if channel not in engine.channels:
        logger.info(
            f"{evaluation_type} evaluation {source_id}: engine {engine.name!r} does "
            f"not support channel {channel.value}, skipping"
        )
        return

    started_at = time.monotonic()
    logger.info(
        f"{evaluation_type} evaluation {source_id} started (engine={engine.name})"
    )
    try:
        verdict = await run_evaluation(engine, context, configuration)
        await save_verdict(str(evaluation["id"]), evaluation_type, context, verdict)
        elapsed = time.monotonic() - started_at
        logger.info(
            f"{evaluation_type} evaluation {source_id} completed in {elapsed:.1f}s: "
            f"{len(verdict.result)} results stored "
            f"(engine={engine.name}, model={verdict.model})"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        logger.error(
            f"{evaluation_type} evaluation {source_id} failed after "
            f"{elapsed:.1f}s: {type(exc).__name__}: {exc}"
        )
