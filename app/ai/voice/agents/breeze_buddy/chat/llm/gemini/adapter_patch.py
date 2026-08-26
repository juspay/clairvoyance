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

THIRD correction (live 2026-08-26): upstream's
``_apply_thought_signatures_to_messages`` probes ONLY ``parts[-1]`` of
each model message when re-attaching a captured signature by bookmark.
The chat cycle loop appends ONE assistant message carrying ALL of a
cycle's parallel tool_calls, which adapts to ONE model Content with N
functionCall parts — and Gemini attaches the thought signature "only to
the first functionCall part" of a parallel batch (documented). So for
any parallel cycle (N > 1) the bookmark references the FIRST part while
upstream probes the LAST: the captured signature silently fails to
re-attach ("Thought signatures to apply: 2 / Applied 1" in the crash
logs), the whole batch reaches the wire unsigned, and Gemini 3 rejects
the request:

    400 INVALID_ARGUMENT: "Function call is missing a thought_signature
    in functionCall parts. This is required for tools to work
    correctly..."

Fix: function_call-bookmarked signatures match against EVERY part of a
candidate message (attaching to the exact part whose id matches — the
part the signature was received on, as the docs require); text /
inline_data bookmarks keep upstream's last-part semantics.

FOURTH correction (same incident, defense in depth): after re-attachment
and the merge pass, any model message that still carries functionCall
parts with NO thought signature anywhere — Gemini emitted none for that
cycle, a degenerate bookmark, or an INJECTED call (widget direct
intents) — is stamped with Google's documented placeholder signature for
client-modified histories, which "skips validation" server-side. This is
a last-resort fallback: it never runs when the message already carries a
real signature, and it only runs at all when a signature regime is
active (at least one signature was captured this session). Anomalous
stamps (anything that is not an injected direct intent) log one
structured warning per request build.
"""

from __future__ import annotations

import base64
from collections import Counter
from typing import List

from google.genai.types import Content
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter

from app.core.logger import logger

__all__ = [
    "AdjacentMergeGeminiAdapter",
    "GEMINI_PLACEHOLDER_SIGNATURE_WIRE",
    "PLACEHOLDER_THOUGHT_SIGNATURE",
]

# Google's documented dummy signature for histories the client has edited
# or injected function calls into ("context engineering") — Gemini skips
# strict thought-signature validation for parts carrying it. The docs
# show it as this LITERAL string in the request JSON; the SDK's
# ``Part.thought_signature`` is ``bytes`` serialized as UNPADDED
# URL-SAFE base64, so the stored bytes are the urlsafe-b64 DECODE of the
# literal — round-tripping to exactly the documented wire form (pinned
# by test, and validated live against Vertex 2026-08-26: an injected
# unsigned functionCall history 400s bare and passes with this stamp).
GEMINI_PLACEHOLDER_SIGNATURE_WIRE = "context_engineering_is_the_way_to_go"
PLACEHOLDER_THOUGHT_SIGNATURE: bytes = base64.urlsafe_b64decode(
    GEMINI_PLACEHOLDER_SIGNATURE_WIRE
)

# tool_call_id prefix of widget direct-intent INJECTED calls
# (chat/agent/direct.py) — unsigned by construction, so stamping them is
# expected and never warns.
_INJECTED_CALL_ID_PREFIX = "intent_"


class AdjacentMergeGeminiAdapter(GeminiLLMAdapter):
    """Gemini adapter whose parallel-call merge requires adjacency, whose
    adapted contents coalesce parallel-call responses into the one user
    turn Vertex requires, whose signature re-attachment probes every part
    of a parallel batch, and which stamps Google's documented placeholder
    signature on any functionCall message still unsigned after all of the
    above (see module docstring)."""

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

    def _apply_thought_signatures_to_messages(
        self, thought_signature_dicts: list[dict], messages: List[Content]
    ) -> None:
        """Re-attach captured signatures by bookmark — EVERY part probed.

        Third correction (module docstring): upstream probes only
        ``parts[-1]`` per model message, which misses the first part of a
        parallel batch — the exact part Gemini signs. Function_call
        bookmarks here match against the part whose ``function_call.id``
        equals the bookmark id, wherever it sits; text / inline_data
        bookmarks keep upstream's last-part probe (a trailing signature
        rides the last part by construction). Same in-order,
        monotonically-advancing search as upstream otherwise.
        """
        if not thought_signature_dicts:
            return

        logger.debug(f"Thought signatures to apply: {len(thought_signature_dicts)}")

        assistant_messages = [
            message
            for message in messages
            if isinstance(message, Content) and message.role == "model"
        ]

        applied = 0
        message_start_index = 0
        for thought_signature_dict in thought_signature_dicts:
            signature = thought_signature_dict.get("signature")
            bookmark = thought_signature_dict.get("bookmark")
            if not signature or not bookmark:
                continue
            function_call_id = bookmark.get("function_call")

            for i in range(message_start_index, len(assistant_messages)):
                message = assistant_messages[i]
                if not message.parts:
                    continue
                matched_part = None
                if function_call_id:
                    for part in message.parts:
                        fc = part.function_call
                        if fc is not None and fc.id == function_call_id:
                            matched_part = part
                            break
                elif self._thought_signature_bookmark_matches_part(
                    bookmark, message.parts[-1]
                ):
                    matched_part = message.parts[-1]
                if matched_part is not None:
                    matched_part.thought_signature = signature
                    applied += 1
                    message_start_index = i + 1
                    break

        logger.debug(f"Applied {applied} thought signatures.")

    def _merge_parallel_tool_calls_for_thinking(
        self, thought_signature_dicts: list[dict], messages: List[Content]
    ) -> List[Content]:
        if not messages:
            return messages

        # Same fast-exit as upstream: no function-call signatures means
        # either thinking is off or there are no function calls — merging
        # is irrelevant either way. (The placeholder stamp below still
        # runs whenever ANY signature was captured: a text-bookmarked
        # signature also proves the signature regime is active.)
        has_function_call_signatures = any(
            ts.get("bookmark", {}).get("function_call")
            for ts in thought_signature_dicts
        )
        if has_function_call_signatures:
            messages = self._adjacent_merge(messages)

        # Fourth correction (module docstring): last-resort placeholder
        # stamp for functionCall messages that are STILL unsigned after
        # re-attachment + merge. Only under an active signature regime —
        # with no captured signatures at all (thinking off / non-signing
        # model) behavior is unchanged.
        if thought_signature_dicts:
            self._stamp_unsigned_function_call_messages(
                thought_signature_dicts, messages
            )

        return messages

    @staticmethod
    def _adjacent_merge(messages: List[Content]) -> List[Content]:
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

    def _stamp_unsigned_function_call_messages(
        self, thought_signature_dicts: list[dict], messages: List[Content]
    ) -> None:
        """Stamp Google's placeholder signature on still-unsigned batches.

        A model message whose parts include functionCalls but carry NO
        thought signature anywhere would 400 under Gemini 3's strict
        current-turn validation. Messages with a real signature are never
        touched — Gemini signs only the first part of a parallel batch,
        and the documented contract is to echo that exact shape back.

        Case per stamped message (for the warning):
        - ``captured_not_reattached``: a captured signature's bookmark
          references a call id INSIDE this message, yet nothing attached
          — the re-attachment above regressed (tripwire; should be
          unreachable now).
        - ``injected_direct_intent``: every call id carries the widget
          direct-intent prefix — an expected client injection; stamped
          silently (this is the placeholder's documented purpose).
        - ``no_signature_captured``: no captured signature references
          this message — Gemini emitted none for that cycle, or the
          bookmark id diverged from the persisted call id.

        One structured warning per request build, and only when at least
        one stamped message is anomalous (non-injected).
        """
        bookmarked_call_ids = {
            (ts.get("bookmark") or {}).get("function_call")
            for ts in thought_signature_dicts
        }
        bookmarked_call_ids.discard(None)

        case_counts: Counter[str] = Counter()
        function_names: set[str] = set()
        total_parts = 0
        for message in messages:
            if not (
                isinstance(message, Content)
                and message.role == "model"
                and message.parts
            ):
                continue
            if any(part.thought_signature for part in message.parts):
                continue
            fc_parts = []
            call_ids = set()
            for part in message.parts:
                fc = part.function_call
                if fc is None:
                    continue
                fc_parts.append(part)
                if fc.id:
                    call_ids.add(fc.id)
                if fc.name:
                    function_names.add(fc.name)
            if not fc_parts:
                continue

            if call_ids & bookmarked_call_ids:
                case = "captured_not_reattached"
            elif call_ids and all(
                call_id.startswith(_INJECTED_CALL_ID_PREFIX) for call_id in call_ids
            ):
                case = "injected_direct_intent"
            else:
                case = "no_signature_captured"

            for part in fc_parts:
                part.thought_signature = PLACEHOLDER_THOUGHT_SIGNATURE
            case_counts[case] += 1
            total_parts += len(fc_parts)

        if not case_counts:
            return
        if set(case_counts) == {"injected_direct_intent"}:
            return
        logger.warning(
            f"[gemini_adapter] request built with {sum(case_counts.values())} "
            f"unsigned functionCall message(s) ({total_parts} part(s); "
            f"functions={sorted(function_names)}; cases={dict(case_counts)}; "
            f"captured_signatures={len(thought_signature_dicts)}) — stamped "
            f"placeholder thought_signature "
            f"'{GEMINI_PLACEHOLDER_SIGNATURE_WIRE}'"
        )


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
