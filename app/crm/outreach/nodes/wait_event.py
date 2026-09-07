"""wait_event — the alarm OR an event, whichever comes first (W5).

The consumer writes the answer into ``context[reply_<node>]`` and wakes the
run; the edge whose ``on`` equals the answer is taken, else "timeout". No
execute, for the same reason a plain wait has none.
"""

from typing import List

from app.crm.outreach.schemas import WorkflowDefinition, WorkflowNode

# The one $-word a wait_event may branch on (rollout phase 15): the
# event's TOPIC rather than a payload field.
TOPIC_KEY = "$topic"
# The answer a wait_event square resolves on when its alarm fires first —
# the label of the arrow a timeout takes. The validator's edge laws, the
# walker's pick_next and the ladder's expansion all spell it from here.
TIMEOUT = "timeout"
# The catch-all arrow, ELSE, is walker vocabulary shared with the condition
# square and the walker: it lives in nodes/spec.py beside NodeParked.


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    problems = []
    if not (node.minutes and node.minutes > 0):
        problems.append(f"wait_event node {node.id} needs minutes > 0")
    if not node.topics:
        problems.append(f"wait_event node {node.id} needs topics")
    if not node.key:
        problems.append(f"wait_event node {node.id} needs a payload key")
    elif node.key.startswith("$") and node.key != TOPIC_KEY:
        problems.append(
            f"wait_event node {node.id}: key {node.key!r} — the only $-word is "
            f"{TOPIC_KEY} (branch on the event's topic)"
        )
    return problems
