"""Plan lifecycle (W1): create draft -> edit draft -> publish -> pause /
archive. The publish validator is what makes the walker's live reads safe
— it blocks the unsafe edit classes (canon T19): a document that strands
waiting tokens, an edge into nowhere, vocabulary the walker doesn't speak.

gather -> decide (PURE, returns the problems) -> apply.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.crm.outreach.db import DbTxn, accessor, atomically
from app.crm.outreach.nodes import NODE_TYPES
from app.crm.outreach.schemas import Workflow, WorkflowDefinition, WorkflowSummary

TIMEOUT = "timeout"


def validate_definition(
    raw: Dict[str, Any],
    occupied_nodes: Optional[List[str]] = None,
    live_entry: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """PURE decide: every law the document must satisfy, as a list of
    human-readable problems (empty = valid). occupied_nodes are the
    squares open tokens stand on — publish must not delete one, and while
    any exist the entry rule (live_entry) must not change under them
    (canon T19: no changing entry semantics mid-flight).
    """
    try:
        definition = WorkflowDefinition.model_validate(raw)
    except Exception as e:  # pydantic's message is already precise
        return [f"definition shape invalid: {e}"]

    problems: List[str] = []
    node_ids = [node.id for node in definition.nodes]
    seen = set()
    for node_id in node_ids:
        if node_id in seen:
            problems.append(f"duplicate node id: {node_id}")
        seen.add(node_id)

    # Per-type laws come from the registry (nodes.py) — the same table the
    # walker executes from, so validator and walker cannot disagree.
    for node in definition.nodes:
        problems.extend(NODE_TYPES[node.type].validate(node, definition))

    node_types = {node.id: node.type for node in definition.nodes}
    for src, dst in ((edge[0], edge[1]) for edge in definition.edges):
        if src not in seen:
            problems.append(f"edge from unknown node: {src}")
        if dst not in seen:
            problems.append(f"edge to unknown node: {dst}")
    for src, arrows in definition.outgoing().items():
        labels = [on for _, on in arrows]
        if node_types.get(src) == "wait_event":
            if None in labels:
                problems.append(f"every edge out of wait_event {src} needs an on")
            if len(set(labels)) != len(labels):
                problems.append(f"wait_event {src} has two edges with the same on")
        else:
            if any(on is not None for on in labels):
                problems.append(f"only a wait_event node may label its edges ({src})")
            if len(arrows) > 1:
                problems.append(f"node {src} has {len(arrows)} outgoing edges")

    if occupied_nodes and live_entry is not None:
        if raw.get("entry") != live_entry:
            problems.append(
                "entry rule changed while runs are open — pause the plan and "
                "let them finish, or publish the entry change as a new plan"
            )

    for occupied in occupied_nodes or []:
        if occupied not in seen:
            problems.append(
                f"node {occupied} has waiting runs standing on it — "
                "publishing a document without it strands every one"
            )

    return problems


async def create_workflow(
    merchant_id: str, name: str, definition: Dict[str, Any], created_by: Optional[str]
) -> Workflow:
    """A new plan is born as a draft. Shape/law problems are rejected at
    the door — a draft may be imperfect only in ways publish will catch,
    never in ways that break the editor."""
    problems = validate_definition(definition)
    if problems:
        raise WorkflowValidationError(problems)
    return await accessor.insert_workflow(merchant_id, name, definition, created_by)


async def update_draft(
    merchant_id: str,
    workflow_id: str,
    definition: Dict[str, Any],
    updated_by: Optional[str] = None,
) -> Optional[Workflow]:
    problems = validate_definition(definition)
    if problems:
        raise WorkflowValidationError(problems)
    return await accessor.update_draft(merchant_id, workflow_id, definition, updated_by)


async def publish_workflow(
    merchant_id: str, workflow_id: str, updated_by: Optional[str] = None
) -> Workflow:
    return await atomically(_publish_in_txn, merchant_id, workflow_id, updated_by)


async def _publish_in_txn(
    txn: DbTxn,
    merchant_id: str,
    workflow_id: str,
    updated_by: Optional[str] = None,
) -> Workflow:
    """ATOMIC: the validate and the copy share one fate — the document the
    validator approved must be the exact document that becomes live, and
    the occupied-squares read must not race a walker moving tokens."""
    workflow = await accessor.workflow_for_publish(txn, merchant_id, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    if not workflow.draft:
        raise WorkflowValidationError(["nothing to publish — draft is empty"])
    occupied = await accessor.occupied_nodes(txn, merchant_id, workflow_id)
    live_entry = (workflow.definition or {}).get("entry")
    problems = validate_definition(
        workflow.draft, occupied_nodes=occupied, live_entry=live_entry
    )
    if problems:
        raise WorkflowValidationError(problems)
    published = await accessor.apply_publish(txn, merchant_id, workflow_id, updated_by)
    if published is None:  # a racing publish consumed the draft first
        raise WorkflowValidationError(["draft already published"])
    logger.info(
        f"workflow published: {workflow_id} v{published.version} "
        f"(merchant {merchant_id})"
    )
    return published


async def set_status(
    merchant_id: str, workflow_id: str, status: str, updated_by: Optional[str] = None
) -> Optional[Workflow]:
    """live <-> paused, or archived (terminal). Archiving force-exits open
    runs as 'ejected' at the walker's next claim — the paused/archived
    check happens there, so no sweep is needed here."""
    if status not in ("live", "paused", "archived"):
        raise WorkflowValidationError([f"unknown status: {status}"])
    return await accessor.set_workflow_status(
        merchant_id, workflow_id, status, updated_by
    )


async def get_workflow(merchant_id: str, workflow_id: str) -> Optional[Workflow]:
    return await accessor.get_workflow(merchant_id, workflow_id)


async def list_workflows(
    merchant_id: str, limit: int, offset: int
) -> List[WorkflowSummary]:
    return await accessor.list_workflows(merchant_id, limit, offset)


class WorkflowValidationError(Exception):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class WorkflowNotFound(Exception):
    pass
