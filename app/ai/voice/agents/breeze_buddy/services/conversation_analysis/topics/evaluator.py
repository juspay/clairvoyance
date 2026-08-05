import asyncio
import time
from typing import Any, Dict

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_config import add_discovered_topics
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_results,
)
from app.schemas.breeze_buddy.conversation_analysis import EvaluationType

from .extractor import extract_topics

_ANALYSIS_TIMEOUT_SECONDS = 60
_ANALYSIS_MAX_ATTEMPTS = 2


async def analyze_topics(
    context: Dict[str, Any],
    evaluation: Dict[str, Any],
) -> None:
    started_at = time.monotonic()
    logger.info(f"Topic evaluation {context['source_id']} started")
    try:
        topics = []
        for attempt in range(1, _ANALYSIS_MAX_ATTEMPTS + 1):
            attempt_started_at = time.monotonic()
            try:
                topics = await asyncio.wait_for(
                    extract_topics(
                        context["transcript"],
                        evaluation.get("topics") or [],
                        evaluation.get("configuration"),
                    ),
                    timeout=_ANALYSIS_TIMEOUT_SECONDS,
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elapsed = time.monotonic() - attempt_started_at
                logger.warning(
                    f"Topic evaluation {context['source_id']} attempt "
                    f"{attempt}/{_ANALYSIS_MAX_ATTEMPTS} failed after "
                    f"{elapsed:.1f}s: {type(exc).__name__}: {exc}"
                )
                if attempt == _ANALYSIS_MAX_ATTEMPTS:
                    raise

        await save_evaluation_results(
            str(evaluation["id"]),
            EvaluationType.TOPIC.value,
            context["source_id"],
            context["reseller_id"],
            context.get("merchant_id"),
            str(context["template_id"]),
            context["started_at"],
            topics,
        )
        labels = list(
            {
                str(topic.get("label") or "")
                .strip()
                .lower(): str(topic.get("label") or "")
                .strip()
                for topic in topics
                if str(topic.get("label") or "").strip()
            }.values()
        )
        if labels:
            await add_discovered_topics(str(context["template_id"]), labels)
        elapsed = time.monotonic() - started_at
        logger.info(
            f"Topic evaluation {context['source_id']} completed in "
            f"{elapsed:.1f}s with {len(topics)} topics"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        error = f"{type(exc).__name__}: {exc}"
        logger.error(
            f"Topic evaluation {context['source_id']} failed after "
            f"{elapsed:.1f}s: {error}"
        )
