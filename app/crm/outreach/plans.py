"""Plan lifecycle (W1): create draft -> edit draft -> publish -> pause /
archive. The publish validator is what makes the walker's live reads safe
— it blocks the unsafe edit classes (canon T19): a document that strands
waiting tokens, an edge into nowhere, vocabulary the walker doesn't speak.

gather -> decide (PURE, returns the problems) -> apply.
"""

from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.crm.connectivity.contracts import registers_templates_for, template_status
from app.crm.outreach.catalog_laws import (
    Catalogs,
    WorkflowValidationError,
    entry_against_catalog,
    gather_catalogs as _gather_catalogs,
)
from app.crm.outreach.db import DbTxn, atomically
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    version as version_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.ladder import LadderProblem, expand_stages
from app.crm.outreach.nodes import NODE_TYPES, is_wait
from app.crm.outreach.nodes.wait_event import ELSE, TIMEOUT
from app.crm.outreach.repeat import parse_repeat_policy
from app.crm.outreach.schemas import (
    GOAL_EXIT_REASONS,
    Workflow,
    WorkflowDefinition,
    WorkflowEntry,
    WorkflowEntryAt,
    WorkflowSummary,
)
from app.crm.record.contracts import (
    topic_counts,
)

SEEN_WINDOW_DAYS = 7


def validate_definition(
    raw: Dict[str, Any],
    occupied_nodes: Optional[List[str]] = None,
    live_entry: Optional[Dict[str, Any]] = None,
    catalogs: Catalogs = None,
) -> List[str]:
    """PURE decide: every law the document must satisfy, as a list of
    human-readable problems (empty = valid). occupied_nodes are the
    squares open tokens stand on — publish must not delete one, and while
    any exist the entry rule (live_entry) must not change under them
    (canon T19: no changing entry semantics mid-flight). catalogs maps each
    topic to its merged field map (gathered by the caller — this function
    never reads); a topic mapped to None is one no layer declares, so
    nothing may be filtered, keyed or templated on it.

    A ladder (phase 17) is expanded first, so every law below judges the
    board it means — and the stored form, the ladder beside its own
    expansion, reads as itself.
    """
    try:
        raw = expand_stages(raw)
    except LadderProblem as e:
        return [str(e)]
    except ValueError as e:  # pydantic: the ladder's own shape
        return [f"stages shape invalid: {e}"]
    try:
        definition = WorkflowDefinition.model_validate(raw)
    except Exception as e:  # pydantic's message is already precise
        return [f"definition shape invalid: {e}"]

    problems: List[str] = []
    raw_entry = raw.get("entry")
    raw_doors = raw_entry if isinstance(raw_entry, list) else [raw_entry]
    if any(
        isinstance(door, dict) and isinstance(door.get("where"), dict) and door["where"]
        for door in raw_doors
    ):
        # The model reads a map for the immutable rows 069 could not rewrite;
        # a document being WRITTEN today must speak the typed grammar.
        problems.append(
            "entry.where is a list of conditions [{field, op, value}] — the "
            "equality map is retired (migration 069)"
        )
    problems.extend(entry_against_catalog(definition, catalogs))
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
        if node.match is not None and not NODE_TYPES[node.type].listens:
            problems.append(
                f"node {node.id}: match belongs to a wait_event — only a "
                "listening square hears a letter"
            )

    # The doors (phase 15): one per topic, each starting on a real square.
    # Repeat-entry words per door (repeat.py owns the vocabulary); debounce
    # slides "the entry wait's alarm" — a door whose start is an action has
    # no alarm to slide.
    by_id = {node.id: node for node in definition.nodes}
    if not definition.entries:
        problems.append("entry: a plan needs at least one door (a topic and a start)")
    topics_seen = set()
    for door in definition.entries:
        if door.topic in topics_seen:
            problems.append(
                f"entry topic {door.topic!r} appears twice — one door per topic"
            )
        topics_seen.add(door.topic)
        start = by_id.get(door.start)
        if start is None:
            problems.append(f"entry {door.topic!r}: start {door.start!r} is not a node")
        if parse_repeat_policy(door.on_repeat) is None:
            problems.append(
                f"entry {door.topic!r}: on_repeat {door.on_repeat!r} is not a policy "
                "(ignore · refresh_latest · refresh_max(<field>) · accumulate)"
            )
        if door.debounce_minutes > 0 and (start is None or not is_wait(start)):
            problems.append(
                f"entry {door.topic!r}: debounce_minutes needs a wait as its start "
                "node — there is no entry alarm to slide otherwise"
            )
        if door.restart_on_repeat and door.debounce_minutes <= 0:
            problems.append(
                f"entry {door.topic!r}: restart_on_repeat needs debounce_minutes > 0 "
                "— there is nothing to re-arm otherwise"
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
        word = node_types.get(src)
        if word is not None and NODE_TYPES[word].branches:
            if None in labels:
                problems.append(f"every edge out of {word} {src} needs an on")
            if len(set(labels)) != len(labels):
                problems.append(f"{word} {src} has two edges with the same on")
        else:
            if any(on is not None for on in labels):
                problems.append(f"only a branching node may label its edges ({src})")
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


def definition_warnings(raw: Dict[str, Any]) -> List[str]:
    """PURE: what a document does that is LEGAL and probably not meant
    (enh A/06, N15 + N16). A warning never refuses — a document may be
    saved mid-edit, and the editor is the place to finish it — but it is
    said out loud on the create, draft and publish answers, because the
    alternative was found the hard way: a board of five complete squares
    and no arrows saved cleanly, published cleanly, and ran only the
    first one.

    Four readings, in the order an author meets them:
    - a square nothing leads to (no door starts there, no arrow reaches
      it), seeded from every door's start because a multi-door plan
      admits people onto mid-board squares;
    - a loop — legal and sometimes meant (a nudge that re-arms on every
      letter), but a run in one ends only by goal, timeout or max age;
    - a listening square with neither a "timeout" nor an "else" edge —
      when the alarm wins, the run ends there and the author may not
      have meant "give up";
    - a label no rule or arm of a self-deciding square answers — the
      edge is drawn, and it is dead.

    A document that does not validate has PROBLEMS, which
    validate_definition reports; this returns nothing for it rather than
    a second, contradictory list."""
    try:
        definition = WorkflowDefinition.model_validate(expand_stages(dict(raw)))
    except Exception:  # noqa: BLE001 — problems, not warnings
        return []
    nodes = {node.id: node for node in definition.nodes}
    if any(edge[0] not in nodes or edge[1] not in nodes for edge in definition.edges):
        return []  # an edge to nowhere is a PROBLEM; walking it would be noise
    warnings: List[str] = []
    outgoing = definition.outgoing()

    starts = [door.start for door in definition.entries if door.start in nodes]
    reachable: set = set()
    frontier = list(starts)
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(dst for dst, _ in outgoing.get(current, []))
    for node in definition.nodes:
        if node.id not in reachable:
            warnings.append(
                f"nothing leads to {node.id} — no door starts there and no arrow "
                "reaches it, so it never runs"
            )

    if _has_cycle(outgoing, list(nodes)):
        warnings.append(
            "the board loops back on itself — fine when meant (a nudge that "
            "re-arms on every letter); a run inside the loop ends only by a "
            "goal, a timeout edge, or the plan's max age"
        )

    # One line for all of them, not one per square: a ladder (phase 17)
    # ends every stage's listening window this way BY DESIGN, and four
    # copies of the same sentence on every publish of the loan board
    # would teach an author to stop reading warnings.
    deaf = [
        node.id
        for node in definition.nodes
        if NODE_TYPES[node.type].listens
        and not {on for _, on in outgoing.get(node.id, [])} & {TIMEOUT, ELSE}
    ]
    if deaf:
        warnings.append(
            f"{', '.join(deaf)}: listens with no 'timeout' or 'else' edge — when "
            "the alarm wins, the run ends there"
        )

    for node in definition.nodes:
        labels = {on for _, on in outgoing.get(node.id, [])}
        # N15: a self-deciding square's answers are its rules or its arms;
        # a label outside them is an edge the walker can never take. Read
        # from the fields, never from the type — a wait_event has neither.
        answers = {rule.on for rule in node.rules} | {arm.on for arm in node.arms}
        if answers:
            for on in sorted(o for o in labels if o and o not in answers | {ELSE}):
                warnings.append(
                    f"{node.id}: edge labelled {on!r} — no rule or arm of this "
                    "square answers that, so the edge is never taken"
                )
    return warnings


def _has_cycle(outgoing: Dict[str, List[Any]], node_ids: List[str]) -> bool:
    """PURE: does any arrow lead back to a square already on the path?
    Iterative three-colour walk; a board is small, but a recursive one
    would still be the wrong shape for a request handler."""
    white, grey, black = 0, 1, 2
    colour = {node_id: white for node_id in node_ids}
    for root in node_ids:
        if colour[root] != white:
            continue
        stack: List[Tuple[str, int]] = [(root, 0)]
        colour[root] = grey
        while stack:
            current, index = stack[-1]
            arrows = outgoing.get(current, [])
            if index < len(arrows):
                stack[-1] = (current, index + 1)
                dst = arrows[index][0]
                if colour.get(dst, black) == grey:
                    return True
                if colour.get(dst, black) == white:
                    colour[dst] = grey
                    stack.append((dst, 0))
            else:
                colour[current] = black
                stack.pop()
    return False


def _entry_changed(raw_entry: Any, live_entry: Any) -> bool:
    """PURE: does the draft's entry MEAN something different from the live
    one? Compared as validated models, so a draft that omits the defaults
    and a live entry that spells them out read equal (B3, rollout phase
    01) — a raw-dict compare refused every re-publish that changed nothing
    about admission. Doors (phase 15) compare as a list, order-free; a
    single object compares as one door with no explicit start, so the
    object and list spellings of the same topic read as different doors
    (conservative: under migrate that is a refusal, and pin is the way
    out). A live entry that no longer parses (a legacy row from before a
    word was added) cannot be normalised: then the raw values are
    compared, exactly as before."""
    try:
        return _doors_by_meaning(raw_entry) != _doors_by_meaning(live_entry)
    except Exception:
        return raw_entry != live_entry


def _doors_by_meaning(entry: Any) -> List[Dict[str, Any]]:
    """PURE: an entry object or door list as sorted, fully-spelled doors."""
    items = entry if isinstance(entry, list) else [entry]
    doors: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and "start" in item:
            doors.append(WorkflowEntryAt.model_validate(item).model_dump())
        else:
            doors.append(
                {**WorkflowEntry.model_validate(item).model_dump(), "start": None}
            )
    return sorted(doors, key=lambda door: door["topic"])


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
    never in ways that break the editor. A ladder (phase 17) is stored
    with the board it expands to: the author's intent beside what the
    walker reads."""
    catalogs = await _gather_catalogs(merchant_id, definition)
    problems = validate_definition(definition, catalogs=catalogs)
    if problems:
        raise WorkflowValidationError(problems)
    workflow = await workflow_accessor.insert_workflow(
        merchant_id, name, expand_stages(definition), created_by
    )
    return workflow.model_copy(update={"warnings": definition_warnings(definition)})


async def update_draft(
    merchant_id: str, workflow_id: str, definition: Dict[str, Any]
) -> Optional[Workflow]:
    catalogs = await _gather_catalogs(merchant_id, definition)
    problems = validate_definition(definition, catalogs=catalogs)
    if problems:
        raise WorkflowValidationError(problems)
    workflow = await workflow_accessor.update_draft(
        merchant_id, workflow_id, expand_stages(definition)
    )
    if workflow is None:
        return None
    return workflow.model_copy(update={"warnings": definition_warnings(definition)})


async def publish_workflow(
    merchant_id: str, workflow_id: str, published_by: Optional[str] = None
) -> Workflow:
    draft = await workflow_accessor.get_workflow(merchant_id, workflow_id)
    catalogs = await _gather_catalogs(
        merchant_id, (draft.draft if draft else None) or {}
    )
    published = await atomically(
        _publish_in_txn, merchant_id, workflow_id, published_by, catalogs
    )
    return published.model_copy(
        update={"warnings": definition_warnings(published.definition or {})}
    )


async def _publish_in_txn(
    txn: DbTxn,
    merchant_id: str,
    workflow_id: str,
    published_by: Optional[str],
    catalogs: Catalogs = None,
) -> Workflow:
    """ATOMIC: the validate, the copy, the version row and (migrate) the
    re-pin share one fate — the document the validator approved must be
    the exact document that becomes live AND the one the new version row
    holds, the occupied-squares read must not race a walker moving tokens,
    and a migrate must never leave a run pointing at a version that did
    not get written (ADR 0023). The templates the draft sends are held
    SHARED (shared/locks.py) from before the approval check, so a
    retirement cannot slip between "approved" and the version row. A
    ladder draft must already carry its board (phase 17): the copy is
    verbatim, so a ladder saved without one would go live without
    squares."""
    workflow = await workflow_accessor.workflow_for_publish(
        txn, merchant_id, workflow_id
    )
    if workflow is None:
        raise WorkflowNotFound(workflow_id)
    draft = workflow.draft
    if not draft:
        raise WorkflowValidationError(["nothing to publish — draft is empty"])
    occupied = await enrollment_accessor.occupied_nodes(txn, merchant_id, workflow_id)
    live_entry = (workflow.definition or {}).get("entry")
    problems = validate_definition(
        draft,
        occupied_nodes=occupied,
        live_entry=live_entry,
        catalogs=catalogs,
    )
    if problems:
        raise WorkflowValidationError(problems)
    if expand_stages(draft) != draft:
        raise WorkflowValidationError(
            [
                "draft is a ladder saved without its board — save the draft "
                "again (PUT /draft) before publishing; publish copies it verbatim"
            ]
        )
    definition = WorkflowDefinition.model_validate(draft)
    await version_accessor.lock_templates_shared(
        txn, merchant_id, definition.send_templates()
    )
    problems = await _template_problems(merchant_id, definition)
    if problems:
        raise WorkflowValidationError(problems)
    published = await workflow_accessor.apply_publish(txn, merchant_id, workflow_id)
    if published is None:  # a racing publish consumed the draft first
        raise WorkflowValidationError(["draft already published"])
    # The version row holds the document that just became live — the draft
    # apply_publish copied verbatim — under the mode it declared.
    await version_accessor.insert_version(
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
        repinned = await enrollment_accessor.repin_open_runs(
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
        verdict = await template_status(merchant_id, node.channel, node.template)
        if not verdict.publishable:
            # The registry's own words for why (its reason clause); outreach
            # never compares a status word across the seam.
            problems.append(
                f"send node {node.id}: template '{node.template}' {verdict.reason}"
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
    workflow = await workflow_accessor.get_workflow(merchant_id, workflow_id)
    if workflow is None or workflow.status == "archived":
        return None
    if not workflow.definition:
        verb = {
            "live": "going live",
            "paused": "pausing it",
            "archived": "archiving it",
        }
        raise WorkflowValidationError([f"publish a draft before {verb[status]}"])
    return await workflow_accessor.set_workflow_status(merchant_id, workflow_id, status)


async def get_workflow(merchant_id: str, workflow_id: str) -> Optional[Workflow]:
    return await workflow_accessor.get_workflow(merchant_id, workflow_id)


async def list_workflows(
    merchant_id: str, limit: int, offset: int
) -> List[WorkflowSummary]:
    """The list, decorated with seen-vs-matched for the window: events on
    each plan's entry topic (record's count, any source) against runs it
    started — "saw 240 · matched 3" is a dashboard fact, never a stored one."""
    summaries = await workflow_accessor.list_workflows(merchant_id, limit, offset)
    if not summaries:
        return summaries
    seen: Dict[str, int] = {}
    for count in await topic_counts(merchant_id, SEEN_WINDOW_DAYS):
        seen[count.topic] = seen.get(count.topic, 0) + count.seen
    started = await enrollment_accessor.enrollment_counts(merchant_id, SEEN_WINDOW_DAYS)
    return [
        s.model_copy(
            update={
                "seen_7d": seen.get(s.entry_topic or "", 0),
                "matched_7d": started.get(str(s.id), 0),
            }
        )
        for s in summaries
    ]


class WorkflowNotFound(Exception):
    pass
