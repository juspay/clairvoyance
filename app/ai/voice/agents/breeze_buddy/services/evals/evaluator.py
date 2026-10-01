"""The eval adapter — the topics-evaluator slot, engine-pluggable, for any
evaluation type: the row it is handed says which.

Resolve the engine named by the agent's config row, gate on the channels
that engine supports, run it once under a hard time bound (retries are the
client's job), and store exactly one evaluation_result row through the
existing topics insert (the verdict as a one-element array). Scores go to
the database ONLY — never to Langfuse. Fail posture: read-only analytics,
so any failure logs and skips; it never blocks the call record.
"""

import asyncio
import time
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.services.evals.engines import (
    ENGINES,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_results,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
)
from app.utils.common import parse_json

# One attempt here: retries live in the provider (TypeSafe: 3 x 30 s on 429/5xx/
# transport errors only, with backoff). This bound covers its worst case.
_EVALUATION_TIMEOUT_SECONDS = 120


async def analyze_evals(
    context: Dict[str, Any],
    evaluation: Dict[str, Any],
    channel: ConversationChannel,
) -> None:
    source_id = context["source_id"]
    # the row says which evaluation type this is: it names the result
    # column's label and the stored ``type`` — this package never does
    evaluation_type = str(evaluation["evaluation_type"])
    # asyncpg hands jsonb back as text; parse_json takes either form
    configuration = parse_json(evaluation, "configuration") or {}

    engine_name = configuration.get("engine")
    engine = ENGINES.get(engine_name) if isinstance(engine_name, str) else None
    if engine is None:
        logger.warning(
            f"{evaluation_type} evaluation {source_id}: unknown engine "
            f"{engine_name!r} in configuration, skipping"
        )
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
        verdict = await asyncio.wait_for(
            engine.evaluate(context, configuration),
            timeout=_EVALUATION_TIMEOUT_SECONDS,
        )

        # ``type`` is the result column's label; the identity CHECK compares it
        # against metadata->>'type', so it rides in the stored JSON too
        stored = {"type": evaluation_type, **verdict.model_dump()}
        await save_evaluation_results(
            str(evaluation["id"]),
            evaluation_type,
            source_id,
            context["reseller_id"],
            context.get("merchant_id"),
            str(context["template_id"]),
            context["started_at"],
            [stored],  # the topics insert takes an array; one verdict = one row
        )
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
