"""Explicit Vertex context caching for the chat loop's static prefix.

Every chat cycle re-sends the same system instruction + tool declarations
(10k+ tokens for a commerce template). Vertex implicit caching never hit
for this shape (probed 2026-07-30: ``cached_content_token_count`` stayed
empty across identical prefixes), so the driver caches the prefix
EXPLICITLY: one CachedContent per distinct (model, system_instruction,
tools) triple, resolved lazily and reused until TTL.

Constraints discovered by probe:

- A request using ``cached_content`` must NOT carry ``system_instruction``,
  ``tools``, or ``tool_config`` (400 INVALID_ARGUMENT) — so forced-choice
  cycles (chips / plan steps, which need ``tool_config`` mode=ANY) bypass
  the cache and send the full prefix. Only unforced cycles — the expensive
  routing + post-tool reasoning calls — ride the cache.
- Failures are memoized with a cooldown (a template whose prefix is under
  the model's cache minimum shouldn't pay a failed create per cycle), and
  a NOT_FOUND at generate time invalidates so the caller can retry with
  the full prefix.

Process-local registry: caches are cheap (storage is per token-hour on a
~10k prefix) and pods just mint their own.

Creation never blocks a turn: a miss returns ``None`` (the caller sends the
full prefix, which is as fast as a cached one at these sizes) and creates
the cache in a background task, one per key. A cache near expiry is still
served while its replacement is created.
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

from app.core.concurrency import spawn_background_task
from app.core.logger import logger

_TTL_SECONDS = 3600
# Start recreating when the entry is this close to expiry.
_REFRESH_MARGIN_SECONDS = 300
# Stop serving an entry this close to expiry — a cycle must never start on
# a cache that lapses mid-stream.
_SERVE_FLOOR_SECONDS = 60
_FAILURE_COOLDOWN_SECONDS = 600

# Registry timestamps are WALL clock (time.time), matching the server's
# TTL accounting. monotonic() pauses across macOS sleep, which made a
# laptop-overnight registry claim a long-expired cache was live
# (2026-07-31 incident: five 404'd turns).

_registry: Dict[str, Tuple[str, float]] = {}
_failed_at: Dict[str, float] = {}
# Cache key → the background create running for it (one per key).
_inflight: Dict[str, "asyncio.Task[None]"] = {}


def _cache_key(
    model: str, system_instruction: Any, tools: Any, scope: Any = None
) -> str:
    """``scope`` is the project + location the client is bound to: a
    CachedContent name belongs to one GCP project, so two templates on two
    Vertex accounts (accounts) must never share an entry."""
    payload = json.dumps(
        {"m": model, "s": system_instruction, "t": tools, "p": scope},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _client_scope(client: Any) -> Any:
    api = getattr(client, "_api_client", None)
    project = getattr(api, "project", None)
    location = getattr(api, "location", None)
    return [project, location] if project or location else None


async def resolve_prompt_cache(
    client: Any,
    *,
    model: str,
    system_instruction: Any,
    tools: Any,
) -> Optional[str]:
    """Return a live CachedContent name for this prefix, or ``None`` — the
    caller then sends the full prefix as before, so this can only ever be a
    no-op, never a regression. Never waits on a cache create: a miss (or an
    entry near expiry) starts one in the background."""
    key = _cache_key(model, system_instruction, tools, _client_scope(client))
    now = time.time()
    failed = _failed_at.get(key)
    cooling = failed is not None and now - failed < _FAILURE_COOLDOWN_SECONDS
    entry = _registry.get(key)
    remaining = entry[1] - now if entry is not None else 0.0
    if remaining <= _REFRESH_MARGIN_SECONDS and key not in _inflight and not cooling:
        _inflight[key] = spawn_background_task(
            _create(client, key, model, system_instruction, tools),
            name=f"prompt-cache:{key[:12]}",
        )
    if entry is not None and remaining > _SERVE_FLOOR_SECONDS:
        return entry[0]
    return None


async def _create(
    client: Any, key: str, model: str, system_instruction: Any, tools: Any
) -> None:
    started = time.time()
    try:
        from google.genai.types import CreateCachedContentConfig

        cache = await client.aio.caches.create(
            model=model,
            config=CreateCachedContentConfig(
                system_instruction=system_instruction,
                tools=tools,
                ttl=f"{_TTL_SECONDS}s",
                display_name=f"bb-chat-prefix-{key[:12]}",
            ),
        )
        _registry[key] = (cache.name, time.time() + _TTL_SECONDS)
        logger.info(
            f"prompt_cache: created {cache.name} in "
            f"{round((time.time() - started) * 1000)} ms "
            f"({getattr(getattr(cache, 'usage_metadata', None), 'total_token_count', '?')} tokens)"
        )
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        _failed_at[key] = time.time()
        logger.warning(
            f"prompt_cache: create failed ({type(exc).__name__}: "
            f"{str(exc)[:160]}) — full-prefix requests for "
            f"{_FAILURE_COOLDOWN_SECONDS}s"
        )
    finally:
        _inflight.pop(key, None)


def invalidate_prompt_cache(name: str) -> None:
    """Drop a cache the server no longer honors (NOT_FOUND at generate)."""
    for key, (cached_name, _) in list(_registry.items()):
        if cached_name == name:
            del _registry[key]


__all__ = ["resolve_prompt_cache", "invalidate_prompt_cache"]
