"""Template generator service — streams a Claude conversation via Vertex AI.

``TemplateGeneratorService.stream()`` is an async generator that yields
SSE-formatted strings (ready to forward directly to the client). It handles:

- Detecting when Claude starts emitting a JSON code fence and firing a
  ``template_start`` event so the UI can switch to a side-by-side preview.
- Yielding ``delta`` events for each text chunk.
- Yielding a final ``done`` event with the extracted template JSON (if any)
  so the UI can wire up the Save/Copy buttons without re-parsing the stream.

Error handling:
- Vertex credential errors → ``error`` event with ``code: "auth_error"``
- Anthropic API errors → ``error`` event with ``code: "api_error"``
- All errors end with a final ``done`` event so the client-side stream
  reader always gets a clean termination.

Design note — sync vs. async client:
  ``get_anthropic_vertex_client()`` (``_pools.py``) returns a *sync*
  ``AsyncAnthropicVertex`` instance (i.e. the Anthropic Python SDK's async
  variant). We call ``await client.messages.stream(...)`` to get an async
  context manager, which is the correct async streaming API.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, cast

from app.ai.voice.llm._pools import get_anthropic_vertex_client
from app.core.config import dynamic as dyn, static
from app.core.logger import logger

from .prompts import build_system_prompt

# Maximum tokens Claude may generate per chat turn.  Large enough to fit a
# complete template JSON (typically 800–2500 tokens) plus surrounding prose.
_MAX_TOKENS = static.TEMPLATE_BUILDER_MAX_TOKENS

# Regex that matches the opening of a ```json code fence (possibly with
# surrounding whitespace / newline).  Used to detect the moment Claude
# transitions from prose to template JSON so we can fire `template_start`.
_JSON_FENCE_OPEN_RE = re.compile(r"```json\s*\n?")
_JSON_FENCE_CLOSE_RE = re.compile(r"\n?```")

# Regex for the <payload_suggestion>…</payload_suggestion> XML block that
# Claude emits when it is ready to suggest payload / response fields.
_PAYLOAD_SUGGESTION_RE = re.compile(
    r"<payload_suggestion>\s*([\s\S]*?)\s*</payload_suggestion>",
    re.IGNORECASE,
)


def _format_sse(event: str, data: dict) -> str:
    """Format a single SSE frame (matches the wire format used by chat/sse.py)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _extract_json_from_text(text: str) -> str | None:
    """Extract the first ```json...``` block from ``text``.

    Returns the raw JSON string (without fences), or ``None`` if not found.
    """
    match = re.search(r"```json\s*\n([\s\S]*?)\n?```", text)
    if match:
        return match.group(1).strip()
    return None


def _extract_payload_suggestion(text: str) -> dict | None:
    """Extract and parse a <payload_suggestion>…</payload_suggestion> block.

    Returns the parsed dict (expected to have ``payload`` and ``response``
    keys, each a list of field objects), or ``None`` if not found or not
    valid JSON.
    """
    match = _PAYLOAD_SUGGESTION_RE.search(text)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and (
            isinstance(parsed.get("payload"), list)
            or isinstance(parsed.get("response"), list)
        ):
            return parsed
        return None
    except (json.JSONDecodeError, ValueError):
        return None


class TemplateGeneratorService:
    """Stateless service — instantiate once per request, call ``stream()``."""

    def __init__(
        self,
        *,
        messages: list[Any],
        reseller_ids: list[str] = [],
        merchant_ids: list[str] = [],
        current_template: dict | None = None,
    ) -> None:
        """
        Args:
            messages: Conversation history in Anthropic message format:
                ``[{"role": "user"|"assistant", "content": "..."}]``.
                The caller must NOT include a system message — it is injected
                here from ``build_system_prompt()``.
            reseller_id: Reseller namespace for the template being generated.
                Injected into the system prompt so Claude embeds the correct
                ``reseller_id`` in the output JSON automatically.
            current_template: When provided, this is a refinement session.
                The template dict is injected as the first user message so
                Claude can see what it is refining.
        """
        self._messages = messages
        self._reseller_ids = reseller_ids
        self._merchant_ids = merchant_ids
        self._current_template = current_template

    async def stream(self) -> AsyncIterator[str]:
        """Async generator that yields SSE-formatted strings.

        Yields:
            ``event: delta`` — text chunk from Claude.
            ``event: template_start`` — fired once when the first ```json
                fence is detected; signals the UI to open the preview pane.
            ``event: done`` — final event. ``data.template`` is the extracted
                JSON string if Claude produced one, else ``null``.
        """
        try:
            credentials_json = await dyn.GOOGLE_VERTEX_CREDENTIALS_JSON()
            project_id = await dyn.GOOGLE_VERTEX_PROJECT_ID()
        except Exception as exc:
            logger.error(f"TemplateGeneratorService: failed to load credentials: {exc}")
            yield _format_sse(
                "error",
                {"code": "auth_error", "message": "Failed to load Vertex credentials"},
            )
            yield _format_sse("done", {"template": None})
            return

        try:
            client = await asyncio.to_thread(
                get_anthropic_vertex_client,
                credentials_json=credentials_json,
                project_id=project_id,
                region=static.TEMPLATE_BUILDER_VERTEX_REGION,
            )
        except ValueError as exc:
            logger.error(
                f"TemplateGeneratorService: Vertex credentials unavailable: {exc}"
            )
            yield _format_sse(
                "error",
                {
                    "code": "auth_error",
                    "message": (
                        "Vertex authentication is unavailable; configure Google "
                        "ADC or fallback credentials"
                    ),
                },
            )
            yield _format_sse("done", {"template": None})
            return
        except Exception as exc:
            logger.error(f"TemplateGeneratorService: failed to build client: {exc}")
            yield _format_sse(
                "error",
                {"code": "auth_error", "message": "Failed to initialise Vertex client"},
            )
            yield _format_sse("done", {"template": None})
            return

        # Build the message list to send.  If this is a refinement session,
        # prepend the current template as the first user message.
        messages = list(self._messages)
        if self._current_template is not None:
            current_template_str = json.dumps(
                self._current_template, indent=2, ensure_ascii=False
            )
            refinement_prefix = (
                "Here is my current template. Please help me refine it:\n\n"
                f"```json\n{current_template_str}\n```"
            )
            # Inject before all other messages so the first human turn
            # already shows the template context.
            messages = [
                {"role": "user", "content": refinement_prefix},
                {
                    "role": "assistant",
                    "content": "I can see your template. What changes would you like to make?",
                },
                *messages,
            ]

        refinement_mode = self._current_template is not None
        system_prompt = build_system_prompt(refinement_mode=refinement_mode)

        # Append reseller context so Claude always embeds the correct
        # reseller_id in generated templates without the user having to
        # state it in the conversation.
        if self._reseller_ids or self._merchant_ids:
            context_lines = ["## Session context\n"]

            reseller_is_wildcard = self._reseller_ids == ["*"]
            merchant_is_wildcard = self._merchant_ids == ["*"]

            if reseller_is_wildcard:
                context_lines.append(
                    "This user has access to ALL resellers. "
                    "Ask the user to specify which reseller_id they want to use for this template "
                    "before generating. Once confirmed, set it at the top level of every template."
                )
            elif len(self._reseller_ids) == 1:
                context_lines.append(
                    f"The reseller_id is ``{self._reseller_ids[0]}``. Always set "
                    f'``"reseller_id": "{self._reseller_ids[0]}"`` at the top level of every template.'
                )
            elif len(self._reseller_ids) > 1:
                ids = ", ".join(f"``{r}``" for r in self._reseller_ids)
                context_lines.append(
                    f"The user belongs to multiple resellers: {ids}. "
                    f"Ask the user which reseller_id to use before generating. "
                    f"Once confirmed, set it at the top level of every template."
                )

            if merchant_is_wildcard:
                context_lines.append(
                    "This user has access to ALL merchants. "
                    "Ask the user to specify which merchant_id they want to use, or confirm "
                    "if no merchant_id is needed (reseller-level template). "
                    "Once confirmed, set it at the top level or omit it accordingly."
                )
            elif len(self._merchant_ids) == 1:
                context_lines.append(
                    f"The merchant_id is ``{self._merchant_ids[0]}``. Always set "
                    f'``"merchant_id": "{self._merchant_ids[0]}"`` at the top level of every template.'
                )
            elif len(self._merchant_ids) > 1:
                ids = ", ".join(f"``{m}``" for m in self._merchant_ids)
                context_lines.append(
                    f"The user belongs to multiple merchants: {ids}. "
                    f"Ask the user which merchant_id to use before generating. "
                    f"Once confirmed, set it at the top level of every template."
                )
            elif not self._merchant_ids:
                context_lines.append(
                    "No merchant_id is associated with this user — omit the ``merchant_id`` field entirely."
                )

            system_prompt = system_prompt + "\n\n" + "\n".join(context_lines)

        # Accumulated full response text — used at the end to extract
        # the template JSON for the ``done`` event payload.
        full_text = ""
        template_start_fired = False

        try:
            async with client.messages.stream(
                model=static.TEMPLATE_BUILDER_VERTEX_CLAUDE_MODEL,
                max_tokens=_MAX_TOKENS,
                system=system_prompt,
                messages=cast(Any, messages),
            ) as stream_ctx:
                async for text_chunk in stream_ctx.text_stream:
                    full_text += text_chunk

                    # Detect the opening ```json fence to fire template_start.
                    if not template_start_fired and _JSON_FENCE_OPEN_RE.search(
                        full_text
                    ):
                        template_start_fired = True
                        yield _format_sse("template_start", {})

                    yield _format_sse("delta", {"text": text_chunk})

        except Exception as exc:
            logger.error(
                f"TemplateGeneratorService: Anthropic API error: {exc}",
                exc_info=True,
            )
            yield _format_sse(
                "error",
                {
                    "code": "api_error",
                    "message": "Claude API request failed",
                },
            )
            yield _format_sse("done", {"template": None})
            return

        extracted = _extract_json_from_text(full_text)
        payload_suggestion = _extract_payload_suggestion(full_text)
        if payload_suggestion is not None:
            yield _format_sse("payload_suggestion", payload_suggestion)
        yield _format_sse("done", {"template": extracted})


__all__ = ["TemplateGeneratorService"]
