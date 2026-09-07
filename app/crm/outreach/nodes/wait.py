"""wait — time passes; the alarm was set on arrival (arrival scheduling).

No execute: landing on the square IS the action, which is the whole
content of ``is_wait``.
"""

from typing import List

from app.crm.outreach.schemas import WorkflowDefinition, WorkflowNode


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    if not (node.minutes and node.minutes > 0):
        return [f"wait node {node.id} needs minutes > 0"]
    return []
