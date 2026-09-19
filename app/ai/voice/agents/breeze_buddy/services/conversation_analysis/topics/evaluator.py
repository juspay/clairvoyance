import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import anthropic
import httpx
import openai
from pydantic import ValidationError

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_config import add_discovered_topics
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_failure,
    save_evaluation_results,
)
from app.schemas.breeze_buddy.conversation_analysis import EvaluationType

from .extractor import TopicModelResponseError, extract_topics

_ANALYSIS_TIMEOUT_SECONDS = 60
_ANALYSIS_MAX_ATTEMPTS = 2

MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
MODEL_BAD_RESPONSE = "MODEL_BAD_RESPONSE"
EVALUATION_ERROR = "EVALUATION_ERROR"


class ModelUnavailableError(Exception):
    def __init__(self, detail: str, retry_after: Optional[float] = None):
        super().__init__(detail)
        self.retry_after = retry_after


def classify_failure(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            httpx.TransportError,
            openai.APIConnectionError,
            anthropic.APIConnectionError,
        ),
    ):
        return MODEL_UNAVAILABLE
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return MODEL_UNAVAILABLE
    if isinstance(
        exc, (TopicModelResponseError, json.JSONDecodeError, ValidationError)
    ):
        return MODEL_BAD_RESPONSE
    return EVALUATION_ERROR


async def save_topic_failure(
    context: Dict[str, Any],
    evaluation: Dict[str, Any],
    error: str,
) -> None:
    await save_evaluation_failure(
        str(evaluation["id"]),
        EvaluationType.TOPIC.value,
        context["source_id"],
        context["reseller_id"],
        context.get("merchant_id"),
        str(context["template_id"]),
        context["started_at"],
        error,
    )


async def analyze_topics(
    context: Dict[str, Any],
    evaluation: Dict[str, Any],
) -> None:
    """Extract and save the topics of one conversation.

    Raises ModelUnavailableError when the model cannot be reached, so the worker
    can put the job back and pause. Any other failure is saved as a FAILED row.
    """
    source_id = context["source_id"]
    configuration = evaluation.get("configuration") or {}
    if isinstance(configuration, str):
        configuration = json.loads(configuration)
    model = configuration.get("model")

    started_at = time.monotonic()
    logger.info(f"Topic evaluation {source_id} started")
    topics: List[Dict[str, Any]] = []
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
            failure = classify_failure(exc)
            if isinstance(exc, TimeoutError):
                detail = f"timeout after {_ANALYSIS_TIMEOUT_SECONDS}s"
            else:
                detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"Topic evaluation {source_id} attempt "
                f"{attempt}/{_ANALYSIS_MAX_ATTEMPTS} failed after "
                f"{time.monotonic() - attempt_started_at:.1f}s: "
                f"{failure} ({detail}) model={model}"
            )

            retryable = failure in (MODEL_UNAVAILABLE, MODEL_BAD_RESPONSE)
            if retryable and attempt < _ANALYSIS_MAX_ATTEMPTS:
                continue

            if failure == MODEL_UNAVAILABLE:
                response = getattr(exc, "response", None)
                retry_after = str(
                    getattr(response, "headers", {}).get("retry-after", "")
                )
                raise ModelUnavailableError(
                    f"{detail}; {attempt} attempts, model={model}",
                    float(retry_after) if retry_after.isdigit() else None,
                ) from exc

            error = f"{failure} after {attempt} attempt(s): {detail}"
            await save_topic_failure(context, evaluation, error)
            logger.error(
                f"Topic evaluation {source_id} {error}: FAILED row saved "
                f"(model={model})"
            )
            return

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
    logger.info(
        f"Topic evaluation {source_id} completed in "
        f"{time.monotonic() - started_at:.1f}s with {len(topics)} topics"
    )
