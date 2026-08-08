"""The render_ui / revise_plan tool handlers and the rider-chips
flush — the agent-side half of the generative-UI pipeline.

Method bodies are verbatim from the monolithic agent.py (split 2026-08-05);
this mixin holds no state of its own — every attribute lives on
``ChatAgent`` (see ``core``)."""

from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import (  # noqa: F401
    _CHIPS_NUDGE,
    _MAX_TOOL_CYCLES,
    _chip_labels,
    _KbMessage,
    _partition_gated_calls,
    _PreparedTools,
    _summarize_result,
    _tools_schema,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import (
    SSEEvent,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    execute_render_ui,
    render_ui_components,
    resolve_render_ui_flavor_pack,
    summarize_render,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    insert_chat_message,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent


class RenderUiHandlerMixin:
    # Assigned here AND in core — annotated identically on both
    # classes so pyrefly sees one consistent member.
    _held_chips: Optional[List[str]]

    async def _render_ui_handler(
        self: "ChatAgent", args: Dict[str, Any], _flow_manager: Any = None
    ) -> Dict[str, Any]:
        """The ``render_ui`` tool handler — a thin wrapper over the existing
        hydration machinery. Returns the compact function response (the
        model's UI memory); hydrated ops ride ``_pending_ui_ops`` to the
        cycle loop's drain."""
        # Turn-level guard (live-observed 2026-07-29): mode=ANY can make
        # Flash spam DUPLICATE render_ui calls in one response, and replayed
        # duplicates then breed more (mimicry). Every call still gets a
        # response (function_call/response pairing is sacred), but past the
        # cap the response is a hard redirect to prose — deterministic loop
        # breaker. The forced final chips cycle is exempt (it has its own
        # 2-cycle bound) — a UI-heavy turn must not lose its chips to calls
        # it already spent mid-turn.
        chips_cycle = self._in_chips_cycle
        self._rui_calls_this_turn = getattr(self, "_rui_calls_this_turn", 0) + 1
        if self._rui_calls_this_turn > 3 and not chips_cycle:
            # The flag MUST drop here: it forces allowed=[render_ui] on the
            # next cycle, and the flag only clears on a SUCCESSFUL render_ui
            # — this error result isn't one. Without this line a model that
            # keeps failing render_ui (bad bind refs) stays locked to a tool
            # that only answers "reply in prose", which the constraint makes
            # impossible — the turn burns all 20 cycles and FAILs.
            self._need_render_ui_think = False
            return {
                "status": "error",
                "error": (
                    "render_ui already resolved this turn — do NOT call it "
                    "again; reply to the user in one short line of prose "
                    "now."
                ),
            }
        if chips_cycle and not self._chips_pending:
            # Duplicate call inside the SAME forced chips cycle (mode=ANY
            # spam): the chips slot already resolved — hard stop, and no
            # second component can ride the tail. ``soft``: the chips DID
            # render; the step rail must not paint a failure.
            return {
                "status": "error",
                "soft": True,
                "error": (
                    "quick replies already resolved — the turn is done; do "
                    "not call render_ui again."
                ),
            }
        # Rider harvest (2026-08-03 — replaces the mid-turn chips BAN):
        # chips are an annotation the model may attach to any render_ui
        # call; the server owns placement. A mid-turn `quick_replies` arg
        # (with a real component, with component=QuickReplies, or alone)
        # is HELD and flushed below the turn's final prose — skipping the
        # forced end-of-turn cycle entirely. The old ban wasted the call,
        # cost an extra cycle, and its error text derailed the model
        # (double-greeting family, live 2026-07-31).
        raw_args = dict(args or {})
        if self._quick_replies_mode == "forced_final" and not chips_cycle:
            rider_raw = raw_args.pop("quick_replies", None)
            rider_labels = _chip_labels(rider_raw)
            if rider_labels:
                self._held_chips = rider_labels  # last-wins across the turn
            if raw_args.get("component") == "QuickReplies" or (
                raw_args.get("component") is None and rider_raw is not None
            ):
                # Chips-only call: nothing else to render. Positive result
                # (not an error — errors bred rephrased-reply rambles); if
                # the REPLY already streamed, trailing prose is duplicate
                # sign-off and gets suppressed.
                #
                # Keyed on _turn_answered, not _turn_prose_streamed: an
                # opener ("Let me check…") is prose but not a reply, and
                # suppressing after one silenced the answer that had not
                # been written yet — the turn then ended with no answer
                # row, no assistant_message, and these very chips dropped.
                if self._turn_answered:
                    self._suppress_extra_prose = True
                return {
                    "status": "ok",
                    "deferred": True,
                    "note": (
                        "follow-ups saved — they will appear under your "
                        "final reply automatically; do not re-author them"
                    ),
                }
        elif (
            chips_cycle
            and raw_args.get("component") is None
            and raw_args.get("quick_replies")
        ):
            # Componentless chips call is the canonical chips-cycle shape
            # now that QuickReplies left the schema enum.
            raw_args["component"] = "QuickReplies"
        components = render_ui_components(self._ui_allowlist, self._catalog_v2)
        if self._quick_replies_mode == "off":
            components = [c for c in components if c != "QuickReplies"]
        if chips_cycle:
            # Chips are the turn's LAST frame in their OWN thread block —
            # the SDK splits ui blocks at the final bubble, so the chips op
            # must anchor a fresh tree (root, no parent), never join the
            # mid-turn tree that painted above the prose. Persistence
            # agrees: the chips op rides its own chips-only assistant row.
            op_id, parent = "root", None
        elif self._turn_rendered_root:
            self._rui_seq += 1
            op_id, parent = f"rui{self._rui_seq}", "root"
        else:
            op_id, parent = "root", None
        # Post-hydration projection policy (layout, CartView checkout
        # stamping off ui_intents roles) is the FLAVOR's — execute resolves
        # the registered pack from flavor_groups and hands it template +
        # state; nothing commerce lives in this loop.
        outcome = execute_render_ui(
            raw_args,
            store=self._binding_store,
            allowlist=self._ui_allowlist,
            components=components,
            op_id=op_id,
            parent=parent,
            trusted_urls=self._trusted_link_urls,
            restrict_to={"QuickReplies"} if chips_cycle else None,
            template=self.template,
            state_values=self.agent_state,
            flavor_groups=self._ui_flavor_groups,
        )
        if outcome.decision == "rendered" and outcome.component and outcome.ops:
            # Repeat-render merge is FLAVOR policy (commerce: a second
            # ProductGrid this turn merges value-level into the first and
            # the wire op becomes a `replace` — one combined display per
            # turn, never stacked surfaces). The engine only tracks the
            # first rendered op per component and asks the pack; the
            # previous op is still the pending in-memory dict (gate rows
            # hold tool ops back), so persistence gets the merged props
            # too.
            pack = resolve_render_ui_flavor_pack(self._ui_flavor_groups)
            merge_fn = pack.merge_repeat_render if pack else None
            if merge_fn is not None:
                new_op = outcome.ops[0]
                prev = self._turn_merge_ops.get(outcome.component)
                if prev is None:
                    self._turn_merge_ops[outcome.component] = new_op
                else:
                    merged = merge_fn(
                        outcome.component,
                        dict(prev.get("props") or {}),
                        dict(new_op.get("props") or {}),
                    )
                    if merged is not None:
                        merged_props, merged_note = merged
                        prev["props"] = merged_props
                        outcome.ops = [
                            {
                                "op": "replace",
                                "id": prev["id"],
                                "props": merged_props,
                                "v": 2,
                            }
                        ]
                        outcome.fn_result = summarize_render(
                            outcome.component,
                            {"props": merged_props},
                            self._ui_flavor_groups,
                        )
                        if merged_note:
                            outcome.fn_result["merged"] = merged_note
        if outcome.ops:
            self._pending_ui_ops.extend(outcome.ops)
            if op_id == "root":
                self._turn_rendered_root = True
        if outcome.decision == "rendered" and outcome.component == "QuickReplies":
            self._quick_replies_rendered = True
        if chips_cycle and outcome.decision in ("rendered", "no_ui"):
            # The chips slot resolved (chips painted or an explicit,
            # reasoned no-chips) — the cycle loop ends the turn on this.
            self._chips_pending = False
        decision_data: Dict[str, Any] = {"decision": outcome.decision}
        if outcome.component:
            decision_data["component"] = outcome.component
        if outcome.reason:
            decision_data["reason"] = outcome.reason[:200]
        self._pending_tool_sse.append(SSEEvent(event="ui_decision", data=decision_data))
        return outcome.fn_result

    async def _revise_plan_handler(
        self: "ChatAgent", args: Dict[str, Any], _flow_manager: Any = None
    ) -> Dict[str, Any]:
        """``revise_plan`` — the only path off an enforced plan. Replaces
        the REMAINING steps, queues the plan_updated SSE (honest step
        rail), and reports the effective remainder back to the model."""
        if not (self._plan_enforcement and self._plan_enforcer.active):
            return {"status": "error", "error": "no active plan to revise"}
        steps = args.get("steps") if isinstance(args, dict) else None
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            return {
                "status": "error",
                "error": "steps must be a list of tool names (may be empty)",
            }
        self._plan_enforcer.revise(steps, self._plan_known_tools)
        remaining = self._plan_enforcer.steps[self._plan_enforcer.cursor :]
        self._pending_tool_sse.append(self._plan_sse(remaining))
        return {"status": "ok", "remaining_steps": remaining}

    async def _flush_held_chips(self: "ChatAgent") -> AsyncIterator[SSEEvent]:
        """Render rider-harvested quick replies below the final prose (the
        chips slot), persisted as their own chips-only row — the forced
        chips cycle never runs for this turn. Same validation path as a
        chips-cycle call; any failure degrades to no chips, never a
        failed turn."""
        labels = self._held_chips or []
        self._held_chips = None
        self._chips_attempted = True
        try:
            outcome = execute_render_ui(
                {"component": "QuickReplies", "quick_replies": labels},
                store=self._binding_store,
                allowlist=self._ui_allowlist,
                components=["QuickReplies"],
                op_id="root",
                parent=None,
                trusted_urls=self._trusted_link_urls,
                restrict_to={"QuickReplies"},
                state_values=self.agent_state,
                flavor_groups=self._ui_flavor_groups,
            )
        except Exception:  # noqa: BLE001 — chips are decoration
            logger.exception(f"ChatAgent {self.session_id}: rider chips flush failed")
            return
        if outcome.decision != "rendered" or not outcome.ops:
            logger.warning(
                f"ChatAgent {self.session_id}: rider chips did not render "
                f"({outcome.decision}) — turn ends without chips"
            )
            return
        await insert_chat_message(
            session_id=self.session_id,
            role=ChatMessageRole.ASSISTANT,
            content=None,
            content_blocks=None,
            ui_blocks=outcome.ops,
        )
        self._quick_replies_rendered = True
        for op in outcome.ops:
            yield SSEEvent(event="ui_op", data={"op": op})
