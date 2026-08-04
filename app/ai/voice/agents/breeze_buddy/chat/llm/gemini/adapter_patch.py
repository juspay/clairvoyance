"""GeminiLLMAdapter with a corrected parallel-tool-call merge.

Chat-mode only, which is why this lives under ``chat/`` next to
``llm_driver`` (which swaps it in per service instance via
``ensure_chat_gemini_adapter``): the corrections below exist for
persisted/replayed chat histories with injected function calls. Voice
builds its context live inside pipecat and stays on the stock adapter —
nothing in the shared ``app/ai/voice/llm/`` tree references this module.

Upstream pipecat's ``_merge_parallel_tool_calls_for_thinking`` merges any
UNSIGNED tool-call-only model message into the previous SIGNED tool-call
group "regardless of what messages appear in between". That invariant
only holds for histories pipecat's own runtime wrote (where a split
parallel batch is always adjacent, signature on the first call only).

Breeze Buddy histories can additionally contain INJECTED function calls
that no model ever produced — widget direct intents (add_to_cart etc.)
are persisted as an assistant ``tool_use`` + user ``tool_result`` row
pair so the next agent turn sees the cart mutation. Injected calls carry
no thought signature, so upstream's merge hoists them across interleaved
text / functionResponse turns into the previous signed group. The
function-call turn then has more functionCall parts than the following
turn has functionResponse parts, and Vertex rejects the request:

    400 INVALID_ARGUMENT: "Please ensure that the number of function
    response parts is equal to the number of function call parts of the
    function call turn."

(Live repro: any agent turn after a direct-intent cart add, once an
earlier signed tool call exists in history.)

Fix: only merge unsigned tool-call messages that are IMMEDIATELY
adjacent to the signed group head — the only layout a genuinely split
parallel batch can have. Anything separated by other messages stays in
place, unsigned, which Vertex accepts for non-current turns.

SECOND correction (live 2026-07-31, the mirror-image bug): when a model
turn carries PARALLEL calls (e.g. two ``render_ui`` calls in one cycle),
the universal history shape persists one tool-result message PER call —
correct for OpenAI, but the stock adaptation emits them as N separate
single-response user contents. Vertex then counts 2 functionCall parts
against a 1-response following turn and rejects with the same 400.
``get_llm_invocation_params`` therefore post-processes the adapted
contents, coalescing CONSECUTIVE response-only user contents into one —
adjacency of response-only user turns occurs exactly and only when one
model turn made parallel calls (sequential exchanges always interleave a
model turn between responses), so the merge can never join unrelated
exchanges.
"""

from __future__ import annotations

from typing import List

from google.genai.types import Content
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter

__all__ = ["AdjacentMergeGeminiAdapter"]


class AdjacentMergeGeminiAdapter(GeminiLLMAdapter):
    """Gemini adapter whose parallel-call merge requires adjacency, and
    whose adapted contents coalesce parallel-call responses into the one
    user turn Vertex requires (see module docstring)."""

    def get_llm_invocation_params(self, *args, **kwargs):
        params = super().get_llm_invocation_params(*args, **kwargs)
        messages = params.get("messages")
        if isinstance(messages, list):
            params["messages"] = self._coalesce_function_responses(messages)
        return params

    @staticmethod
    def _is_response_only_user(msg: Content) -> bool:
        return bool(
            msg.role == "user"
            and msg.parts
            and all(getattr(part, "function_response", None) for part in msg.parts)
        )

    def _coalesce_function_responses(self, messages: List[Content]) -> List[Content]:
        """Merge runs of consecutive response-only user contents into one
        user content — the shape Vertex requires after a parallel-call
        model turn. Mixed user contents (text + response) never merge."""
        out: List[Content] = []
        i = 0
        while i < len(messages):
            current = messages[i]
            if self._is_response_only_user(current):
                parts = list(current.parts or [])
                j = i + 1
                while j < len(messages) and self._is_response_only_user(messages[j]):
                    parts.extend(messages[j].parts or [])
                    j += 1
                out.append(Content(role="user", parts=parts) if j > i + 1 else current)
                i = j
            else:
                out.append(current)
                i += 1
        return out

    def _merge_parallel_tool_calls_for_thinking(
        self, thought_signature_dicts: list[dict], messages: List[Content]
    ) -> List[Content]:
        if not messages:
            return messages

        # Same fast-exit as upstream: no function-call signatures means
        # either thinking is off or there are no function calls — merging
        # is irrelevant either way.
        has_function_call_signatures = any(
            ts.get("bookmark", {}).get("function_call")
            for ts in thought_signature_dicts
        )
        if not has_function_call_signatures:
            return messages

        def is_tool_call_message(msg: Content) -> bool:
            return bool(
                msg.role == "model"
                and msg.parts
                and all(getattr(part, "function_call", None) for part in msg.parts)
            )

        def has_thought_signature(msg: Content) -> bool:
            return any(
                getattr(part, "thought_signature", None) for part in (msg.parts or [])
            )

        merged_messages: List[Content] = []
        i = 0
        while i < len(messages):
            current = messages[i]
            if is_tool_call_message(current) and has_thought_signature(current):
                merged_parts = list(current.parts or [])
                j = i + 1
                # Merge ONLY while the very next message is an unsigned
                # tool-call message. The first interleaved message of any
                # other kind ends the group (upstream instead skipped over
                # such messages and kept merging — the hoisting bug).
                while (
                    j < len(messages)
                    and is_tool_call_message(messages[j])
                    and not has_thought_signature(messages[j])
                ):
                    merged_parts.extend(messages[j].parts or [])
                    j += 1
                merged_messages.append(Content(role="model", parts=merged_parts))
                i = j
            else:
                merged_messages.append(current)
                i += 1

        return merged_messages


def ensure_chat_gemini_adapter(service):
    """Swap a Gemini service's stock adapter for AdjacentMergeGeminiAdapter
    — CHAT ONLY, applied per service instance by ``turn_core``.

    Chat replays persisted histories (one role=tool message per tool call,
    plus widget direct-intent injections), which need the parallel-call
    merge + function-response coalescing above. Voice builds its context
    live inside pipecat and stays on the stock service —
    ``get_llm_service`` / ``vertex.py`` are shared with the voice pipeline
    and must NOT carry this patch.

    Idempotent and instance-local: pipecat stores the adapter per instance
    (``self._adapter = self.adapter_class()`` in ``LLMService.__init__``),
    so swapping here never leaks into voice services. No-op for non-Gemini
    services and for anything already patched. Private-attr posture matches
    the chat driver's ``_client`` reads (pipecat 1.1 has no public seam;
    revisit on upgrade).
    """
    from pipecat.services.google.llm import GoogleLLMService

    if isinstance(service, GoogleLLMService) and not isinstance(
        service.get_llm_adapter(), AdjacentMergeGeminiAdapter
    ):
        service._adapter = AdjacentMergeGeminiAdapter()
    return service
