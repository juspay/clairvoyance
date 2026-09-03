"""Plan lifecycle (W1): create draft -> edit draft -> publish -> pause /
archive. The publish validator is what makes the walker's live reads safe
— it blocks the unsafe edit classes (canon T19): a document that strands
waiting tokens, an edge into nowhere, vocabulary the walker doesn't speak.

gather -> decide (PURE, returns the problems) -> apply.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.crm.connectivity.contracts import registers_templates_for, template_status
from app.crm.outreach.db import DbTxn, accessor, atomically
from app.crm.outreach.nodes import NODE_TYPES, is_wait
from app.crm.outreach.repeat import parse_repeat_policy
from app.crm.outreach.schemas import (
    GOAL_EXIT_REASONS,
    Workflow,
    WorkflowDefinition,
    WorkflowEntry,
    WorkflowSummary,
)

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

    # Repeat-entry words (repeat.py owns the vocabulary). debounce slides
    # "the entry wait's alarm" — a plan whose first square is an action has
    # no alarm to slide.
    if parse_repeat_policy(definition.entry.on_repeat) is None:
        problems.append(
            f"entry.on_repeat {definition.entry.on_repeat!r} is not a policy "
            "(ignore · refresh_latest · refresh_max(<field>) · accumulate)"
        )
    if definition.entry.debounce_minutes > 0 and not is_wait(definition.nodes[0]):
        problems.append(
            "entry.debounce_minutes needs a wait as the first node — there is "
            "no entry alarm to slide otherwise"
        )

    # Goal tiers (phase 06): the reason is vocabulary, and one tier per
    # reason — two tiers claiming goal_met could never be told apart.
    reasons_seen = set()
    for index, tier in enumerate(definition.goals):
        if tier.exit_reason not in GOAL_EXIT_REASONS:
            problems.append(
                f"goal tier {index}: exit_reason {tier.exit_reason!r} is not one "
                f"of {' · '.join(GOAL_EXIT_REASONS)}"
            )
        elif tier.exit_reason in reasons_seen:
            problems.append(
                f"goal tier {index}: exit_reason {tier.exit_reason!r} is already "
                "used by an earlier tier — one tier per reason"
            )
        reasons_seen.add(tier.exit_reason)

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

    # The stranding laws are migrate-mode preconditions (ADR 0023): only a
    # document that will be pushed UNDER the open runs can strand them.
    # Under pin they keep their own version, and the new one is theirs to
    # ignore — the checks below do not apply.
    if definition.on_publish == "migrate":
        if occupied_nodes and live_entry is not None:
            if _entry_changed(raw.get("entry"), live_entry):
                problems.append(
                    "entry rule changed while runs are open — pause the plan "
                    "and let them finish, publish the entry change as a new "
                    "plan, or publish with on_publish: pin"
                )

        for occupied in occupied_nodes or []:
            if occupied not in seen:
                problems.append(
                    f"node {occupied} has waiting runs standing on it — "
                    "migrating a document without it strands every one"
                )

    return problems


def _entry_changed(raw_entry: Any, live_entry: Dict[str, Any]) -> bool:
    """PURE: does the draft's entry MEAN something different from the live
    one? Compared as validated models, so a draft that omits the defaults
    and a live entry that spells them out read equal (B3, rollout phase
    01) — a raw-dict compare refused every re-publish that changed nothing
    about admission. A live entry that no longer parses (a legacy row from
    before a word was added) cannot be normalised: then the raw dicts are
    compared, exactly as before."""
    try:
        draft = WorkflowEntry.model_validate(raw_entry).model_dump()
        live = WorkflowEntry.model_validate(live_entry).model_dump()
    except Exception:
        return raw_entry != live_entry
    return draft != live


def validate_migration(
    from_doc: Dict[str, Any], to_doc: Dict[str, Any], occupied_nodes: List[str]
) -> List[str]:
    """PURE decide (rollout phase 14): may the open runs pinned to from_doc
    be moved under to_doc? The stranding laws as a function — the same two
    a migrate-mode publish enforces: every square those runs stand on
    exists in the target, and the target's entry means the same (by
    meaning, B3). Both documents are published versions, so their per-node
    laws already held at their own publish."""
    try:
        WorkflowDefinition.model_validate(from_doc)
        target = WorkflowDefinition.model_validate(to_doc)
    except Exception as e:  # pydantic's message is already precise
        return [f"definition shape invalid: {e}"]

    problems: List[str] = []
    # Both laws guard runs IN FLIGHT (the same condition the migrate-mode
    # publish applies): with no open run pinned to the source, there is
    # nothing to strand and nothing to re-admit — the move is a no-op and
    # is allowed as one.
    if occupied_nodes and _entry_changed(
        to_doc.get("entry"), from_doc.get("entry") or {}
    ):
        problems.append(
            "entry rule differs between the two versions — runs cannot move "
            "under a different admission rule; publish the entry change as a "
            "new plan"
        )
    squares = {node.id for node in target.nodes}
    for occupied in occupied_nodes:
        if occupied not in squares:
            problems.append(
                f"node {occupied} has waiting runs standing on it — the target "
                "version does not have it and would strand every one"
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
    merchant_id: str, workflow_id: str, definition: Dict[str, Any]
) -> Optional[Workflow]:
    problems = validate_definition(definition)
    if problems:
        raise WorkflowValidationError(problems)
    return await accessor.update_draft(merchant_id, workflow_id, definition)


async def publish_workflow(
    merchant_id: str, workflow_id: str, published_by: Optional[str] = None
) -> Workflow:
    return await atomically(_publish_in_txn, merchant_id, workflow_id, published_by)


async def _publish_in_txn(
    txn: DbTxn, merchant_id: str, workflow_id: str, published_by: Optional[str]
) -> Workflow:
    """ATOMIC: the validate, the copy, the version row and (migrate) the
    re-pin share one fate — the document the validator approved must be
    the exact document that becomes live AND the one the new version row
    holds, the occupied-squares read must not race a walker moving tokens,
    and a migrate must never leave a run pointing at a version that did
    not get written (ADR 0023). The templates the draft sends are held
    SHARED (shared/locks.py) from before the approval check, so a
    retirement cannot slip between "approved" and the version row."""
    workflow = await accessor.workflow_for_publish(txn, merchant_id, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    draft = workflow.draft
    if not draft:
        raise WorkflowValidationError(["nothing to publish — draft is empty"])
    occupied = await accessor.occupied_nodes(txn, merchant_id, workflow_id)
    live_entry = (workflow.definition or {}).get("entry")
    problems = validate_definition(
        draft, occupied_nodes=occupied, live_entry=live_entry
    )
    if problems:
        raise WorkflowValidationError(problems)
    definition = WorkflowDefinition.model_validate(draft)
    await accessor.lock_templates_shared(txn, merchant_id, definition.send_templates())
    problems = await _template_problems(merchant_id, definition)
    if problems:
        raise WorkflowValidationError(problems)
    published = await accessor.apply_publish(txn, merchant_id, workflow_id)
    if published is None:  # a racing publish consumed the draft first
        raise WorkflowValidationError(["draft already published"])
    # The version row holds the document that just became live — the draft
    # apply_publish copied verbatim — under the mode it declared.
    await accessor.insert_version(
        txn,
        merchant_id,
        workflow_id,
        published.version,
        draft,
        definition.on_publish,
        published_by,
    )
    repinned = 0
    if definition.on_publish == "migrate":
        repinned = await accessor.repin_open_runs(
            txn, merchant_id, workflow_id, published.version
        )
    logger.info(
        f"workflow published: {workflow_id} v{published.version} "
        f"({definition.on_publish}; {repinned} open runs re-pinned; "
        f"merchant {merchant_id})"
    )
    return published


async def _template_problems(
    merchant_id: str, definition: WorkflowDefinition
) -> List[str]:
    """GATHER for the publish atom (rollout phase 08, G12): every send node
    on a channel that registers templates must name one the registry knows
    AND has approved — otherwise the first sign of a wrong name is a
    blocked send at dispatch, hours after publish. A lookup, so it lives
    here beside the atom and validate_definition stays PURE. Drafts are
    not checked (create/update): a draft may precede approval. The
    contract takes its own pooled connection beside the atom's — the
    resolve()-inside-the-pass precedent."""
    problems: List[str] = []
    for node in definition.nodes:
        if node.type != "send" or not node.channel or not node.template:
            continue  # the validator already demands both on a send node
        if not registers_templates_for(node.channel):
            continue  # a channel with no registry (email) has nothing to ask
        status = await template_status(merchant_id, node.channel, node.template)
        if status is None:
            problems.append(
                f"send node {node.id}: template '{node.template}' is not "
                f"registered on {node.channel} for this merchant"
            )
        elif status != "approved":
            problems.append(
                f"send node {node.id}: template '{node.template}' is "
                f"'{status}', not approved"
            )
    return problems


async def set_status(
    merchant_id: str, workflow_id: str, status: str
) -> Optional[Workflow]:
    """live <-> paused, or archived (terminal). Archiving force-exits open
    runs as 'ejected' at the walker's next claim — the paused/archived
    check happens there, so no sweep is needed here.

    Returns None for an unknown, foreign or archived plan (the door's
    404, as before). Leaving 'draft' needs a published document: migration
    057's CHECK (status = 'draft' OR definition IS NOT NULL) admits a NULL
    definition only while the plan is a draft, so live, paused and
    archived on a never-published draft all used to surface as a driver
    error — a 500. The pre-read decides it here as one validation miss
    (B4, rollout phase 01); a driver exception is never caught in logic."""
    if status not in ("live", "paused", "archived"):
        raise WorkflowValidationError([f"unknown status: {status}"])
    workflow = await accessor.get_workflow(merchant_id, workflow_id)
    if workflow is None or workflow.status == "archived":
        return None
    if not workflow.definition:
        verb = {
            "live": "going live",
            "paused": "pausing it",
            "archived": "archiving it",
        }
        raise WorkflowValidationError([f"publish a draft before {verb[status]}"])
    return await accessor.set_workflow_status(merchant_id, workflow_id, status)


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
