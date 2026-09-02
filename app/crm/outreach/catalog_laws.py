"""The catalog laws of a plan (design/event-catalog.md §Ownership; canon
T24): conditions, entry.key and a send node's template variables come
ONLY from declared fields, an op only from the field's type, a deprecated
field is a warning. One pure concern, kept apart from the plan lifecycle
(plans.py: gather -> validate -> apply) so each file stays one thing.

gather_catalogs is the cold read; everything else is pure and takes what
it gathered as an argument.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.crm.outreach.ladder import expand_stages
from app.crm.outreach.schemas import WorkflowDefinition
from app.crm.record.contracts import (
    AmbiguousTopic,
    CatalogField,
    canonical_path,
    catalog_fields,
    variable_name,
)
from app.crm.shared.predicate import Condition, as_number

# The catalog handed to validate_definition: topic -> (path -> field, both
# layers), with None for a topic no layer knows — every door's topic and
# every listened topic. None altogether = the caller gathered nothing
# (unit callers): shape laws only.
Catalogs = Optional[Dict[str, Optional[Dict[str, CatalogField]]]]


class WorkflowValidationError(Exception):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def entry_against_catalog(
    definition: WorkflowDefinition, catalogs: Catalogs
) -> List[str]:
    """The catalog laws (event-catalog.md §Ownership): conditions,
    entry.key and the send nodes' template variables come ONLY from
    declared fields; an op only from the field's type; a deprecated field
    is a warning, not a refusal. One pass per door (phase 15): each door's
    topic is the catalog its words are checked against — a send node's
    mapped facts must be declared on EVERY door's topic, since a run may
    enter through any of them."""
    if catalogs is None:
        return []  # nothing gathered (pure-unit callers): shape laws only
    problems: List[str] = []
    mapped = [
        (node.id, blank, fact)
        for node in definition.nodes
        if node.type == "send"
        for blank, fact in node.variables.items()
    ]
    listened = listened_facts(definition, catalogs)
    for entry in definition.entries:
        topic = entry.topic
        fields = catalogs.get(topic)
        if fields is None:
            if entry.where or entry.key or mapped:
                problems.append(
                    f"topic {topic!r} is not in the catalog — register its "
                    "schema (or declare it in code) before filtering, keying "
                    "or templating on it"
                )
            continue
        declared = {variable_name(f.path) for f in fields.values() if f.variable}
        allowed = declared | _WALKER_FACTS | listened
        for node_id, blank, fact in mapped:
            if fact not in allowed:
                problems.append(
                    f"send node {node_id}: variable {blank!r} <- {fact!r} is not "
                    f"a declared variable field (topic {topic!r}; declared: "
                    f"{', '.join(sorted(declared)) or 'none'})"
                )
        for condition in entry.where:
            problems.extend(
                f"{p} (topic {topic!r})"
                for p in condition_against_catalog(condition, fields)
            )
        if entry.key:
            key_path = canonical_path(entry.key)
            field = fields.get(key_path)
            if field is None:
                problems.append(
                    f"entry.key {entry.key!r} is not a declared field (topic {topic!r})"
                )
            elif not field.keyable:
                problems.append(
                    f"entry.key {entry.key!r} is not keyable (topic {topic!r})"
                )
    return problems


# Facts the walker computes for a template at the square (nodes.run_facts,
# phase 16) — never a producer's, so no catalog declares them.
_WALKER_FACTS = frozenset({"current_node", "current_stage"})


def listened_facts(definition: WorkflowDefinition, catalogs: Catalogs) -> set:
    """PURE: every facts_<square>_<key> a send may name — a wait_event
    square's letter (phase 16: kept under context.facts.<square>) exposes
    the variable fields declared for ANY topic that square listens on."""
    names: set = set()
    if catalogs is None:
        return names
    for node in definition.nodes:
        if node.type != "wait_event":
            continue
        for topic in node.topics:
            for field in (catalogs.get(topic) or {}).values():
                if field.variable:
                    names.add(f"facts_{node.id}_{variable_name(field.path)}")
    return names


def condition_against_catalog(
    condition: Condition, fields: Dict[str, CatalogField]
) -> List[str]:
    field = fields.get(condition.field)
    if field is None:
        return [f"where: {condition.field!r} is not a declared field"]
    if condition.op not in field.ops:
        return [
            f"where: {condition.op!r} is not an op for {condition.field!r} "
            f"({field.type}; allowed: {', '.join(field.ops) or 'none'})"
        ]
    if field.deprecated:
        logger.warning(f"where: {condition.field!r} is deprecated in the catalog")
    values = condition.value if condition.op == "in" else [condition.value]
    if condition.op == "exists":
        return []
    if field.type == "choice" and field.values:
        bad = [v for v in values if str(v) not in field.values]
        if bad:
            return [f"where: {condition.field!r} has no value {bad[0]!r}"]
    if field.type == "number" and any(as_number(v) is None for v in values):
        return [f"where: {condition.field!r} is a number"]
    if field.type == "boolean" and any(not isinstance(v, bool) for v in values):
        return [f"where: {condition.field!r} is a yes/no"]
    return []


async def gather_catalogs(merchant_id: str, raw: Dict[str, Any]) -> Catalogs:
    """topic -> merged field map (None = unknown topic) for every door's
    topic and every listened topic — the cold read the pure validator
    receives as an argument. Read off the EXPANDED document (phase 17): a
    ladder has no doors or squares until expand_stages mints them; a
    ladder that will not expand gathers nothing and the validator says why.

    A topic TWO sources declare for the merchant has no honest field map
    while a door names topic alone (canon T19; `entry.source` is a ruling
    owed): the gather refuses with a WorkflowValidationError naming both
    sources rather than hand the validator a silent union."""
    if isinstance(raw, dict) and raw.get("stages") is not None:
        try:
            raw = expand_stages(raw)
        except Exception:  # LadderProblem / pydantic — validate_definition reports it
            return {}
    entry = raw.get("entry") if isinstance(raw, dict) else None
    doors = entry if isinstance(entry, list) else [entry]
    topics = {
        str(door["topic"])
        for door in doors
        if isinstance(door, dict) and door.get("topic")
    }
    # …and every topic a wait_event square listens on: its letter's facts
    # (phase 16) are what a send may name as facts_<square>_<key>.
    for node in raw.get("nodes") or [] if isinstance(raw, dict) else []:
        if isinstance(node, dict) and node.get("type") == "wait_event":
            topics.update(str(t) for t in node.get("topics") or [] if t)
    catalogs: Dict[str, Optional[Dict[str, CatalogField]]] = {}
    problems: List[str] = []
    for topic in sorted(topics):
        try:
            catalogs[topic] = await catalog_fields(merchant_id, topic)
        except AmbiguousTopic as e:
            problems.append(
                f"topic {topic!r} is declared by more than one source "
                f"({', '.join(e.sources)}) — a door names the topic alone, so "
                "the plan cannot say which letter it means"
            )
    if problems:
        raise WorkflowValidationError(problems)
    return catalogs
