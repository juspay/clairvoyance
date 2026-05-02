"""Business logic handlers for Blueprint chat endpoints.

Wires the REST and SSE API to the Blueprint LangGraph agent. SSE is the
only streaming transport — the WebSocket handler was removed in Phase 6
(Loom was never calling it; SSE covers the one-way streaming we need).

Two backward-compat dimensions matter here:

* **Runtime context** (langgraph 0.6+): session-fixed values (mode,
  reseller, existing template id, available outbound numbers) ride on
  ``BlueprintContext`` and are passed via the ``context=`` kwarg on
  ``ainvoke`` / ``astream``. They no longer live on state.
* **Interrupt API** (langgraph 1.x): the graph's ``await_approval``
  node calls ``interrupt(...)`` whenever ``pending_approval_for`` is
  set. Programmatic SDK consumers see ``__interrupt__`` events in the
  stream and can ``Command(resume=<reply>)`` to continue. Chat UIs
  (Loom) keep working because the state flag is still committed by the
  preceding ``tick`` node and visible in ``aget_state`` snapshots; the
  next regular ``HumanMessage`` from the user implicitly answers the
  approval (the LLM reads it from the transcript).

See ``docs/blueprint/TEMPLATE_CREATION_AGENT.md`` §3 for the graph's public contract.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional, Union

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from starlette.responses import StreamingResponse

from app.ai.text.agents.blueprint import create_blueprint_agent
from app.ai.text.agents.blueprint.agent.checkpointer import get_checkpointer
from app.ai.text.agents.blueprint.agent.state import BlueprintContext
from app.core.logger import logger
from app.database.accessor.blueprint.sessions import get_session_by_id, update_session
from app.database.accessor.breeze_buddy.outbound_number import get_all_outbound_numbers
from app.schemas import UserInfo
from app.schemas.blueprint.chat import (
    ChatMessage,
    MessageRole,
    SendMessageRequest,
    SendMessageResponse,
)

# ---------------------------------------------------------------------------
# Agent compilation.
#
# Lazy: the first request compiles the graph with whichever checkpointer
# ``get_checkpointer()`` returns (Postgres if the lifespan has initialized
# it, otherwise the process-local MemorySaver fallback). Module-level
# compilation would run at import time before the pool is open.
# ---------------------------------------------------------------------------
_compiled_agent = None


def _get_agent():
    """Return the compiled agent, compiling lazily on first use."""
    global _compiled_agent
    if _compiled_agent is None:
        _compiled_agent = create_blueprint_agent(checkpointer=get_checkpointer())
    return _compiled_agent


async def _build_context(session) -> BlueprintContext:
    """Assemble the runtime context for one Blueprint session.

    Session-fixed values that the graph needs every tick — fetched from
    the session row plus a one-shot DB query for available numbers. The
    same context is passed on every ``ainvoke`` for this thread; it does
    not bloat the checkpoint history because it lives outside state.
    """
    return BlueprintContext(
        mode=session.mode,
        reseller_id=session.reseller_id,
        existing_template_id=session.template_id if session.template_id else None,
        available_outbound_numbers=await _fetch_outbound_numbers(session.reseller_id),
    )


async def _fetch_outbound_numbers(reseller_id: str) -> list[dict[str, Any]]:
    """Query active outbound numbers for this reseller."""
    try:
        all_numbers = await get_all_outbound_numbers()
        return [
            {
                "id": n.id,
                "number": n.number,
                "provider": (
                    n.provider.value
                    if hasattr(n.provider, "value")
                    else str(n.provider)
                ),
            }
            for n in all_numbers
            if (n.reseller_id == reseller_id or reseller_id == "*")
            and getattr(n, "status", None)
            and (
                n.status.value == "active"
                if hasattr(n.status, "value")
                else str(n.status) == "active"
            )
        ]
    except Exception as exc:
        logger.warning(f"[blueprint] Failed to fetch outbound numbers: {exc}")
        return []


def _has_outstanding_interrupt(snapshot: Any) -> bool:
    """True if the latest checkpoint has a pending interrupt waiting on resume."""
    interrupts = getattr(snapshot, "interrupts", None) or ()
    return bool(interrupts)


def _resolve_input(snapshot: Any, content: str) -> Union[Command, dict[str, Any]]:
    """Decide whether to send the user's content as a resume or fresh message.

    If the latest snapshot has both ``pending_approval_for`` set AND an
    active ``__interrupt__`` (i.e. a programmatic consumer paused the
    graph mid-run), treat the incoming content as a ``Command(resume=...)``
    payload so ``await_approval`` returns it from ``interrupt()``.
    Otherwise, send it as a regular ``HumanMessage`` for ``run_turn`` to
    process — this is the chat UI path, where Loom never pauses the run.
    """
    values = getattr(snapshot, "values", None) or {}
    if values.get("pending_approval_for") and _has_outstanding_interrupt(snapshot):
        return Command(resume=content)
    return {"messages": [HumanMessage(content=content)]}


def _preview_from_state(values: dict[str, Any]) -> dict | None:
    """Template preview surfaced to the client.

    Finished template wins; otherwise send the live draft so the UI can show
    progress as groups are filled.
    """
    return values.get("template_json") or values.get("draft") or None


def _interrupt_payload(snapshot: Any) -> Optional[dict[str, Any]]:
    """Render the latest pending interrupt for SSE clients, if any."""
    interrupts = getattr(snapshot, "interrupts", None) or ()
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return {"id": getattr(first, "id", None), "value": value}


# ---------------------------------------------------------------------------
# REST handler
# ---------------------------------------------------------------------------


async def send_message_handler(
    session_id: str,
    request: SendMessageRequest,
    current_user: UserInfo,
) -> SendMessageResponse:
    """Invoke the Blueprint agent once and return its reply."""
    logger.info(
        f"User {current_user.username} sending message to blueprint session: {session_id}"
    )

    try:
        session = await get_session_by_id(session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Blueprint session not found: {session_id}",
            )
        if current_user.role != "admin" and session.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this blueprint session",
            )

        try:
            agent = _get_agent()
        except Exception as agent_err:
            logger.error(
                "Failed to compile Blueprint agent graph",
                error=str(agent_err),
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Blueprint agent is not available: {agent_err}",
            )

        config: RunnableConfig = {
            "configurable": {"thread_id": session.langgraph_thread_id}
        }
        context = await _build_context(session)

        snapshot = await agent.aget_state(config)
        prev_msg_count = len(snapshot.values.get("messages", []))
        agent_input = _resolve_input(snapshot, request.content)

        result = await agent.ainvoke(
            agent_input, config=config, context=context, durability="async"
        )

        # v2: turn handler no longer tracks current_group explicitly —
        # pending_approval_for is the only group-name signal the UI
        # reads, and it doubles as the progress indicator.
        pending_approval_for = result.get("pending_approval_for")

        all_messages = result.get("messages", [])
        # Skip the user message we just appended, then everything before it.
        new_messages = all_messages[prev_msg_count + 1 :]

        response_messages: list[ChatMessage] = [
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=msg.content,
                step=pending_approval_for,
            )
            for msg in new_messages
            if getattr(msg, "type", None) == "ai" and getattr(msg, "content", "")
        ]
        if not response_messages:
            response_messages.append(
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="Processing complete.",
                    step=pending_approval_for,
                )
            )

        if pending_approval_for:
            await update_session(
                session_id=session_id,
                current_step=pending_approval_for,
                status=None,
                result_template_id=None,
                updated_at=datetime.now(timezone.utc),
            )

        return SendMessageResponse(
            messages=response_messages,
            current_step=pending_approval_for,
            approval_required=pending_approval_for is not None,
            preview=_preview_from_state(result),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message to blueprint session: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error sending message: {str(e)}",
        )


# ---------------------------------------------------------------------------
# SSE streaming handler
# ---------------------------------------------------------------------------


def _sse_event(event: str, data: dict) -> str:
    """Format a Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _extract_text_from_state(values: dict) -> str:
    """Latest AI text from state — fallback when no tokens streamed."""
    for msg in reversed(values.get("messages", [])):
        msg_content = getattr(msg, "content", "")
        if getattr(msg, "type", None) != "ai" or not msg_content:
            continue
        if isinstance(msg_content, str):
            return msg_content
        if isinstance(msg_content, list):
            parts = []
            for block in msg_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            text = "".join(parts)
            if text:
                return text
    return "Processing complete."


async def stream_message_handler(
    session_id: str,
    request: SendMessageRequest,
    current_user: UserInfo,
) -> StreamingResponse:
    """Stream Blueprint agent responses via Server-Sent Events."""

    async def _generate() -> AsyncGenerator[str, None]:
        try:
            yield ": keepalive\n\n"

            session = await get_session_by_id(session_id)
            if not session:
                yield _sse_event(
                    "error", {"message": f"Session not found: {session_id}"}
                )
                return
            if current_user.role != "admin" and session.user_id != current_user.id:
                yield _sse_event("error", {"message": "Access denied"})
                return

            try:
                agent = _get_agent()
            except Exception as agent_err:
                yield _sse_event(
                    "error", {"message": f"Agent unavailable: {agent_err}"}
                )
                return

            config: RunnableConfig = {
                "configurable": {"thread_id": session.langgraph_thread_id}
            }
            context = await _build_context(session)
            snapshot = await agent.aget_state(config)
            agent_input = _resolve_input(snapshot, request.content)

            t0 = time.monotonic()

            # v2: the turn handler uses ``with_structured_output``, so the
            # underlying model stream is partial JSON — never user prose.
            # Streaming those tokens to the chat dumps raw JSON into the
            # UI. Use ``astream`` with ``stream_mode="updates"`` so we can
            # surface ``__interrupt__`` events to programmatic clients
            # while still sending the final ``message_to_user`` from
            # state in the ``done`` event.
            yield _sse_event("thinking", {"content": ""})
            try:
                async for chunk in agent.astream(
                    agent_input,
                    config=config,
                    context=context,
                    stream_mode="updates",
                    durability="async",
                ):
                    if not isinstance(chunk, dict):
                        continue
                    interrupts = chunk.get("__interrupt__")
                    if interrupts:
                        first = interrupts[0]
                        yield _sse_event(
                            "interrupt",
                            {
                                "id": getattr(first, "id", None),
                                "value": getattr(first, "value", None),
                            },
                        )
            except Exception as invoke_err:
                logger.warning(
                    f"[blueprint] turn invoke failed: "
                    f"{type(invoke_err).__name__}: {invoke_err}"
                )

            logger.info(f"[blueprint] Turn complete in {time.monotonic() - t0:.1f}s")

            final_snapshot = await agent.aget_state(config)
            final_values = final_snapshot.values
            pending_approval_for = final_values.get("pending_approval_for")
            completed_groups = final_values.get("completed_groups", []) or []
            completed_step = (
                completed_groups[-1] if completed_groups else pending_approval_for
            )

            if pending_approval_for:
                yield _sse_event("step", {"step": pending_approval_for})

            # The turn LLM writes its user-facing text into the latest AI
            # message on state — that's the only thing the chat should show.
            final_content = _extract_text_from_state(final_values)

            if pending_approval_for:
                await update_session(
                    session_id=session_id,
                    current_step=pending_approval_for,
                    status=None,
                    result_template_id=None,
                    updated_at=datetime.now(timezone.utc),
                )

            yield _sse_event(
                "done",
                {
                    "content": final_content,
                    "current_step": pending_approval_for,
                    "completed_step": completed_step,
                    "pending_approval_for": pending_approval_for,
                    "preview": _preview_from_state(final_values),
                    "interrupt_payload": _interrupt_payload(final_snapshot),
                },
            )

        except Exception as e:
            logger.error(
                f"SSE stream error for session {session_id}: {e}", exc_info=True
            )
            yield _sse_event("error", {"message": str(e)})

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
