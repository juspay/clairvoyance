"""Declarative session-state reducer + tool-arg injection engines.

Both engines are **commerce-agnostic**. They evaluate JMESPath
expressions declared in the template config against a context (tool
result for the reducer; ``{session_id, state.data, args}`` for the
injection engine) and either:

- merge values into a session-scoped ``data: dict`` (reducer), or
- merge values into outgoing MCP tool arguments (injection).

Commerce flavour — knowing that ``cart.id`` from ``update_cart`` should
land at the ``cart_id`` state key, and that the ``update_cart`` tool's
``cart_id`` argument should be filled from that state — lives entirely
in template JSON. The Python here doesn't know what a cart is.

Sibling to :mod:`response_transform` (which mutates tool responses
declaratively) and :mod:`response_filter` (which projects them
declaratively).

Why we need this
----------------
Anthropic's tool loop discipline (see ``poc/claude-cookbooks/INSIGHTS.md``)
plus the canonical Shopify reference (``poc/shop-chat-agent``) plus the
A2A / AP2 / UCP specs all converge on the same rule: identifiers live
out-of-band, not in the LLM transcript. We preserve tool_use/tool_result
blocks so the LLM can self-recover; we also persist a canonical state
row so compaction and voice/chat parity have a single source of truth;
and we inject server-side identifiers into outgoing tool calls so the
LLM never has to guess.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import jmespath

from app.ai.voice.agents.breeze_buddy.template.types import (
    StateReducer,
    ToolArgInjection,
)
from app.core.logger import logger

# ---------------------------------------------------------------------------
# Value generators — commerce-agnostic
# ---------------------------------------------------------------------------
#
# Each generator is a zero-arg callable that returns the value to stamp
# onto an outgoing tool arg. Used for things the LLM shouldn't be asked
# to invent: idempotency keys, request ids, client-side timestamps.
#
# Wire-format note: values are serialised by the MCP layer downstream;
# strings here are intentional so they round-trip JSON cleanly.


def _gen_uuid_v4() -> str:
    return str(uuid.uuid4())


def _gen_uuid_v7() -> str:
    # Python's stdlib only ships v1/v3/v4/v5. uuid_v7 is monotonic +
    # time-ordered which is friendlier to log search; we fall back to
    # v4 if the runtime doesn't have the helper (older Python).
    fn = getattr(uuid, "uuid7", None)
    return str(fn() if fn else uuid.uuid4())


def _gen_timestamp_iso8601() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_timestamp_unix_ms() -> int:
    return int(time.time() * 1000)


_GENERATORS: Dict[str, Callable[[], Any]] = {
    "uuid_v4": _gen_uuid_v4,
    "uuid_v7": _gen_uuid_v7,
    "timestamp_iso8601": _gen_timestamp_iso8601,
    "timestamp_unix_ms": _gen_timestamp_unix_ms,
}


# Sentinel name for the context-aware idempotency-hash generator. Special-cased
# inside ``inject_tool_args`` because it depends on per-call context
# (args, tool_name, session_id, turn_id) — the zero-arg ``_GENERATORS``
# table can't carry that. Templates use:
#     "generators": {"idempotency_key": "idempotency_hash"}
# to opt in. The legacy "uuid_v4" choice still works but is NOT retry-safe
# (each dispatch produces a fresh value, defeating idempotency on retry).
_IDEMPOTENCY_HASH_GENERATOR = "idempotency_hash"


def _gen_idempotency_hash(
    *,
    tool_name: str,
    args: Dict[str, Any],
    session_id: str,
    turn_id: str,
) -> str:
    """Deterministic idempotency key for retry-safe UCP mutations.

    Hash inputs:
      - tool_name     — distinguishes ``create_cart`` from ``update_cart``
      - canonical args— sorted JSON so key ordering doesn't shift the hash
      - session_id    — scopes the key to one widget conversation
      - turn_id       — distinguishes "retry within the same turn"
                        (same intent, same key, idempotent) from "user
                        re-issues the same action in a later turn"
                        (legitimately a new operation, different key)

    Output is the first 128 bits of SHA-256 in hex (32 chars), matching
    UUID-string width and easy to grep for. SHA-256 prefix is collision-
    resistant well past the lifetime of any reasonable widget session.

    Same intent → same key → upstream returns the prior result on retry.
    Different intent (or different turn) → different key → fresh op.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    # Version prefix so a future scheme change can be detected and
    # transitioned without ambiguity.
    h.update(b"breeze-buddy.idempotency.v1\0")
    h.update(tool_name.encode("utf-8"))
    h.update(b"\0")
    h.update(session_id.encode("utf-8"))
    h.update(b"\0")
    h.update(turn_id.encode("utf-8"))
    h.update(b"\0")
    h.update(canonical.encode("utf-8"))
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Reducer engine — tool result → state delta
# ---------------------------------------------------------------------------


def apply_state_reducers(
    state_data: Dict[str, Any],
    tool_name: str,
    tool_result: Any,
    reducers: Optional[List[StateReducer]],
) -> Dict[str, Any]:
    """Compute the next state from the current state + a tool result.

    Returns a new dict (does not mutate the input). Reducers with a
    non-matching ``tool_name`` are skipped. ``only_on_success`` rules
    skip on tool error envelopes. Missing JMESPath matches and lookup
    failures are silently skipped (a stale rule never crashes a turn).
    """
    if not reducers:
        return dict(state_data)

    next_state = dict(state_data)
    payload = _unwrap_tool_payload(tool_result)

    for rule in reducers:
        if rule.tool_name != tool_name:
            continue
        if rule.only_on_success and not _is_tool_success(tool_result):
            continue
        if payload is None:
            continue

        for state_key, expr in rule.set_paths.items():
            try:
                value = jmespath.search(expr, payload)
            except Exception as e:
                logger.warning(
                    f"[session_state] reducer {tool_name!r} key {state_key!r} "
                    f"JMESPath failed: {e}"
                )
                continue
            if value is None:
                continue
            next_state[state_key] = value

    return next_state


# ---------------------------------------------------------------------------
# Tool-arg injection engine — state → outgoing tool arguments
# ---------------------------------------------------------------------------


def inject_tool_args(
    tool_name: str,
    args: Dict[str, Any],
    state_data: Dict[str, Any],
    chat_session_id: str,
    injections: Optional[List[ToolArgInjection]],
    turn_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply matching injection rules to a tool's argument dict.

    Returns a new dict (does not mutate ``args``). ``only_if_missing``
    rules (default) skip arg keys the LLM already provided — the LLM's
    intent wins when it explicitly passes a value, the state-driven
    default fills in when it doesn't. This is the discipline every
    reference implementation in our research adopted (see e.g. Stripe's
    ``StripeMcpClient.callTool`` priority: per-call override > session
    context > none).

    ``turn_id`` is required for the ``idempotency_hash`` generator (the
    hash includes turn_id so that the same intent across distinct turns
    gets distinct keys). Passing None falls back to ``chat_session_id``
    as the turn discriminator — degrades the hash to "session-scoped"
    semantics, which is OK for inert callers but breaks retry safety for
    repeat-the-same-action flows. Chat agent always passes a fresh turn
    uuid from ``ChatAgent.run_turn``.
    """
    if not injections:
        return dict(args)

    next_args = dict(args)
    eval_context = {
        "session_id": chat_session_id,
        "state": {"data": state_data},
        "args": dict(args),
    }

    for rule in injections:
        if rule.tool_name != tool_name:
            continue
        for arg_key, expr in rule.set_paths.items():
            if rule.only_if_missing and next_args.get(arg_key) is not None:
                continue
            try:
                value = jmespath.search(expr, eval_context)
            except Exception as e:
                logger.warning(
                    f"[session_state] injection {tool_name!r} arg {arg_key!r} "
                    f"JMESPath failed: {e}"
                )
                continue
            if value is None:
                continue
            next_args[arg_key] = value

        for arg_key, generator_name in rule.generators.items():
            if rule.only_if_missing and next_args.get(arg_key) is not None:
                continue
            # Context-aware idempotency hash. Special-cased because the
            # zero-arg _GENERATORS table can't carry per-call context.
            # Hash uses ``next_args`` (post-injection) rather than the
            # original LLM ``args`` so server-injected paths like
            # ``cart_id`` are part of the digest — without this, two
            # materially different dispatched requests in the same turn
            # would collide on the same idempotency key.
            if generator_name == _IDEMPOTENCY_HASH_GENERATOR:
                try:
                    next_args[arg_key] = _gen_idempotency_hash(
                        tool_name=tool_name,
                        args=next_args,
                        session_id=chat_session_id,
                        turn_id=turn_id or chat_session_id,
                    )
                except Exception as e:
                    logger.warning(
                        f"[session_state] injection {tool_name!r} arg {arg_key!r} "
                        f"idempotency_hash failed: {e}"
                    )
                continue
            fn = _GENERATORS.get(generator_name)
            if fn is None:
                logger.warning(
                    f"[session_state] injection {tool_name!r} arg {arg_key!r} "
                    f"unknown generator {generator_name!r}; supported: "
                    f"{sorted(_GENERATORS) + [_IDEMPOTENCY_HASH_GENERATOR]}"
                )
                continue
            try:
                next_args[arg_key] = fn()
            except Exception as e:
                logger.warning(
                    f"[session_state] injection {tool_name!r} arg {arg_key!r} "
                    f"generator {generator_name!r} failed: {e}"
                )

    return next_args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap_tool_payload(tool_result: Any) -> Optional[Any]:
    """Pull the structured payload out of our standard ``FlowResult`` envelope.

    MCP tool handlers (see :mod:`mcp`) wrap the upstream response as
    ``{"status": "success"|"error", "data": "<json-string-or-text>"}``.
    Reducers operate on the *parsed* upstream payload, not the envelope.

    Returns the parsed payload, or ``None`` if the envelope is missing
    or malformed (in which case all reducers silently no-op).
    """
    if not isinstance(tool_result, dict):
        return tool_result
    if "data" not in tool_result:
        return tool_result
    data = tool_result["data"]
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (ValueError, json.JSONDecodeError):
            return data
    return data


def _is_tool_success(tool_result: Any) -> bool:
    """Treat an explicit ``status="error"`` as a failure; anything else
    (including missing-status / non-dict results) as success.

    Conservative on purpose — we don't want to skip a reducer that would
    have set state just because the envelope is a plain string.
    """
    if isinstance(tool_result, dict):
        status = tool_result.get("status")
        if status is None:
            return True
        return status not in ("error", "failed")
    return True


__all__ = [
    "apply_state_reducers",
    "inject_tool_args",
]
