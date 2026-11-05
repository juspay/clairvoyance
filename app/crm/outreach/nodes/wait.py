"""wait — time passes, and, when it lists topics, a letter may cut it short.

Three forms of one word (ruled 17 Sep 2026; `wait_event` folded in), with
`topics` as the discriminant:
  - no topics: a plain timer;
  - topics: the alarm OR an event, whichever comes first (W5) — the
    consumer writes the answer into ``context[reply_<node>]`` and wakes the
    run; the edge whose ``on`` equals the answer is taken, else "timeout";
  - either one held to hours by a ``window``.
``minutes`` is optional: absent, a listening wait lasts the run's life and a
bare window waits only for the hours; the window then applies to whatever
that duration is — outreach/window.py.

No execute: landing on the square IS the action (the alarm), which is the
whole content of ``is_wait``.
"""

from typing import List

from app.crm.outreach.schemas import WorkflowDefinition, WorkflowNode

# The one $-word a listening wait may branch on (rollout phase 15): the
# event's TOPIC rather than a payload field.
TOPIC_KEY = "$topic"
# The answer a listening wait resolves on when its alarm fires first — the
# label of the arrow a timeout takes. The validator's edge laws, the
# walker's pick_next and the ladder's expansion all spell it from here.
TIMEOUT = "timeout"
# The catch-all arrow, ELSE, is walker vocabulary shared with the condition
# square and the walker: it lives in nodes/spec.py beside NodeParked.


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    problems = []
    if node.minutes is not None and not node.minutes > 0:
        problems.append(f"wait node {node.id}: minutes must be > 0, or left out")
    if node.topics:
        if not node.key:
            problems.append(
                f"wait node {node.id} lists topics, so it needs a payload key "
                f"(or {TOPIC_KEY} to branch on the event's name)"
            )
        elif node.key.startswith("$") and node.key != TOPIC_KEY:
            problems.append(
                f"wait node {node.id}: key {node.key!r} — the only $-word is "
                f"{TOPIC_KEY} (branch on the event's topic)"
            )
    else:
        if node.key:
            problems.append(
                f"wait node {node.id}: key belongs to a wait that lists topics"
            )
        if node.minutes is None and node.window is None:
            problems.append(
                f"wait node {node.id} waits for nothing — give it minutes, a "
                "window, or topics to listen for"
            )
    return problems
