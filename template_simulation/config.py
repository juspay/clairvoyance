"""Settings, the LLM seam, and the shared GRID retry.

The AGENT under test is always the template's own model, never chosen here.
GRID is everyone else — persona, judge, author, patch proposer — and must be
a different model, or a run passes because one model played both sides.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar

from app.core.logger import logger

REPO_ROOT = Path(__file__).resolve().parents[1]

# Run artifacts, relative to the repo root.
OUTPUT_DIR = "template_simulation/simulation_run"

# Real calls at once. Each builds its own STT/TTS websocket and recorder so
# they cannot bleed into each other, but they share one machine and this mode
# measures timing. Set 1 for a jitter-free fidelity pass.
CALL_CONCURRENCY = 3

# Hard ceilings on one call — a persona that cannot converge otherwise burns
# real minutes and real TTS credit.
MAX_CUSTOMER_TURNS = 24
MAX_CALL_SECONDS = 150.0

# Phase 2 — propose template patches from confirmed failures. Only runs when
# the eval pass actually fails.
RUN_ITERATE = True
MAX_ITERATIONS = 3
# Prompt edits only. True lets a patch touch nodes/functions/transitions —
# review the diff before promoting one.
ALLOW_STRUCTURAL = False

# GRID — persona, judge, authoring, iterate. Never the agent.
GRID_BASE_URL = "GRID_BASE_URL"
GRID_API_KEY = "GRID_API_KEY"
GRID_PERSONA_MODEL = "open-fast"
GRID_JUDGE_MODEL = "open-fast"
GRID_AUTHOR_MODEL = "open-fast"
GRID_ITERATE_MODEL = "open-fast"
GRID_TEMPERATURE = 0.7
GRID_MAX_TOKENS = 2000
# The patch proposer returns edits (or a whole template); 2000 truncates it.
GRID_ITERATE_MAX_TOKENS = 16000

RETRY_ATTEMPTS = 3
# Per ATTEMPT. A completion that has not answered in this long is hung, not
# slow, and its wait never shows up in the per-call timings.
RETRY_TIMEOUT_S = 45.0


class SimConfigError(RuntimeError):
    """A run needs a value that isn't set. The message names the exact fix."""


async def _grid_config(model: str, max_tokens: Optional[int] = None) -> Any:
    from app.ai.voice.llm.types import LLMConfiguration, LLMProvider
    from app.services.live_config.store import get_config

    base_url = (await get_config(GRID_BASE_URL, "", str)).strip()
    if not base_url:
        raise SimConfigError(
            f"GRID is not configured: '{GRID_BASE_URL}' is empty. "
            f"Add {GRID_BASE_URL}=<your endpoint> to .env."
        )
    if not os.environ.get(GRID_API_KEY):
        raise SimConfigError(
            f"GRID is not configured: env var '{GRID_API_KEY}' is not set. "
            f"Add {GRID_API_KEY}=<your key> to .env."
        )
    return LLMConfiguration(
        provider=LLMProvider.OPENAI,
        model=model,
        endpoint=base_url.rstrip("/").removesuffix("/chat/completions"),
        api_key_name=GRID_API_KEY,
        temperature=GRID_TEMPERATURE,
        max_tokens=max_tokens or GRID_MAX_TOKENS,
    )


async def grid_llm(model: str, max_tokens: Optional[int] = None) -> Any:
    from app.ai.voice.agents.breeze_buddy.llm import get_llm_service

    return await get_llm_service(await _grid_config(model, max_tokens))


def agent_llm_config(template: Any) -> Any:
    """The agent's model — production's own accessor, so it cannot drift."""
    from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
        resolve_llm_configuration,
    )

    return resolve_llm_configuration(template)


async def agent_llm(template: Any) -> Any:
    from app.ai.voice.agents.breeze_buddy.llm import get_llm_service

    return await get_llm_service(agent_llm_config(template))


T = TypeVar("T")


async def with_retry(
    call: Callable[[], Awaitable[T]], *, label: str, attempts: int = RETRY_ATTEMPTS
) -> T:
    """Retry a GRID call, treating a hang as a failure (no timeout = a run
    that blocks forever on one unanswered request)."""
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(call(), timeout=RETRY_TIMEOUT_S)
        except Exception as e:  # GRID failures are not a fixed set of types
            last = e
            if attempt == attempts:
                break
            wait = 1.5 * attempt
            logger.warning(f"[sim] {label}: attempt {attempt} failed ({e}); {wait}s")
            await asyncio.sleep(wait)
    assert last is not None
    raise last
