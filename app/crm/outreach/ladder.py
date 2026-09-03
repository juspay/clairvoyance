"""The stages ladder (rollout phase 17; notes §14.1, §16.2): an ordered
funnel written as ONE small object, expanded into the wait_event board by
the validator — PURE, and idempotent because every square's id derives
from its stage's topic. The author never draws the O(n²) arrows, the
walker never learns the word, and the console can re-edit the funnel
because the ladder is stored beside the board it produced.

For stage i (topic T_i, slug s_i) with later stages T_{i+1} .. T_n:

  at-s_i      wait_event  key $topic · topics = the later stages ·
                          minutes = idle (the "went quiet on this stage"
                          clock; the stage is labelled on the square)
  act-s_i     the on_idle action — a call or a send
  after-s_i   wait_event  the same later stages · minutes = after_action
                          (the listening window after the action)
  arrows      at-s_i     --T_j-->      at-s_j    for every later stage j
              at-s_i     --timeout-->  act-s_i   -->  after-s_i
              after-s_i  --T_j-->      at-s_j    for every later stage j
              and NO timeout arrow out of after-s_i: silence there is the
              end of the run — completed, the drop-off record.

The LAST stage has nothing later to listen for: at-s_n is a plain wait,
then act-s_n, then the end. The doors: one per stage, starting on its
at- square, debouncing by that stage's idle time (a retried stage letter
re-arms the clock) and carrying the ladder's restart_on_repeat (phase
16: the stage's own letter re-arms whatever square the run stands on).
Goals and exits are the author's — a ladder says nothing about them.

Slugs: the topic's own name (after its last '.'), lowercased, every run
of anything but letters and digits as one '-': loan.kyc_completed ->
kyc-completed. No underscores on purpose — phase 16 flattens a square's
facts as facts_<square>_<key>. Two stages that slug alike are refused.

A keyed ladder (a top-level `key`, phase 18) listens only for letters
about ITS key: every listening square carries match {payload: key, run:
key} — the consumer wakes every open run of the customer whose square
listens for a topic, and with two applications (two runs) a KYC letter
for one would otherwise move both.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from app.crm.outreach.nodes import TIMEOUT, TOPIC_KEY
from app.crm.outreach.schemas import StageAction, Stages

# What the ladder produces. A document carrying a ladder may not draw
# these by hand; its OWN expansion beside it (the stored form) reads as
# itself.
_EXPANDED_WORDS = ("nodes", "edges", "entry")
# The door words the ladder sets on every door it mints. A top-level
# spelling would fold into the doors (schemas._doors_take_the_shared_words)
# and silently lose to the ladder's, so it is refused instead.
_LADDER_OWNED_WORDS = ("debounce_minutes", "restart_on_repeat")
_NOT_A_SLUG = re.compile(r"[^a-z0-9]+")


class LadderProblem(ValueError):
    """A ladder the expander cannot honour — one human-readable sentence,
    reported by validate_definition as a problem."""


def slug_of(topic: str) -> str:
    """PURE: the part of a stage topic its squares are named by."""
    name = topic.rsplit(".", 1)[-1].lower()
    return _NOT_A_SLUG.sub("-", name).strip("-")


def expand_stages(raw: Dict[str, Any]) -> Dict[str, Any]:
    """PURE, idempotent: a document with `stages` gets the nodes, edges and
    entry the ladder means, beside the ladder itself; any other document
    comes back as it is. Raises LadderProblem when the document draws
    beside its ladder or spells a word the ladder owns, and pydantic's
    ValidationError when the ladder's own shape is wrong."""
    if raw.get("stages") is None:
        return raw
    stages = Stages.model_validate(raw["stages"])
    if "debounce_minutes" in raw:
        raise LadderProblem(
            "stages: debounce_minutes is the ladder's to set — every door it "
            "mints re-arms by its stage's idle_minutes; remove the top-level word"
        )
    if "restart_on_repeat" in raw:
        raise LadderProblem(
            "stages: restart_on_repeat is the ladder's to set — say it inside "
            "stages; remove the top-level word"
        )
    nodes, edges, entry = _board(stages, raw.get("key"))
    produced: Dict[str, Any] = {"nodes": nodes, "edges": edges, "entry": entry}
    for word in _EXPANDED_WORDS:
        if word in raw and raw[word] != produced[word]:
            raise LadderProblem(
                f"stages: a ladder does not carry {word} of its own — the "
                f"expander produces them, and the {word} given are not this "
                "ladder's expansion"
            )
    return {**raw, **produced}


def _board(
    stages: Stages, key: Optional[str]
) -> Tuple[List[Dict[str, Any]], List[List[str]], List[Dict[str, Any]]]:
    """PURE: the squares, the arrows and the doors of one ladder; `key` is
    the document's run key, which every listening square matches on."""
    slugs = [slug_of(topic) for topic in stages.order]
    by_slug: Dict[str, str] = {}
    for topic, slug in zip(stages.order, slugs):
        if not slug:
            raise LadderProblem(
                f"stages: {topic!r} has no letters or digits to name its squares by"
            )
        if slug in by_slug:
            raise LadderProblem(
                f"stages: {by_slug[slug]!r} and {topic!r} would share the square "
                f"at-{slug} — two stages need distinct names"
            )
        by_slug[slug] = topic

    nodes: List[Dict[str, Any]] = []
    edges: List[List[str]] = []
    entry: List[Dict[str, Any]] = []
    for index, (topic, slug) in enumerate(zip(stages.order, slugs)):
        own = stages.overrides.get(topic)
        idle = stages.idle_minutes
        after_action = stages.after_action_minutes
        action = stages.on_idle
        if own is not None:
            idle = own.idle_minutes if own.idle_minutes is not None else idle
            if own.after_action_minutes is not None:
                after_action = own.after_action_minutes
            action = own.on_idle if own.on_idle is not None else action
        at, act, after = f"at-{slug}", f"act-{slug}", f"after-{slug}"
        later = list(zip(stages.order[index + 1 :], slugs[index + 1 :]))

        entry.append(
            {
                "topic": topic,
                "start": at,
                "debounce_minutes": idle,
                "restart_on_repeat": stages.restart_on_repeat,
            }
        )
        if not later:
            nodes.append({"id": at, "type": "wait", "minutes": idle, "stage": topic})
            nodes.append(_action_square(act, action, topic))
            edges.append([at, act])
            continue
        listens = [t for t, _ in later]
        nodes.append(_listening_square(at, listens, idle, topic, key))
        nodes.append(_action_square(act, action, topic))
        nodes.append(_listening_square(after, listens, after_action, topic, key))
        for later_topic, later_slug in later:
            edges.append([at, f"at-{later_slug}", later_topic])
        edges.append([at, act, TIMEOUT])
        edges.append([act, after])
        for later_topic, later_slug in later:
            edges.append([after, f"at-{later_slug}", later_topic])
    return nodes, edges, entry


def _listening_square(
    node_id: str, topics: List[str], minutes: float, stage: str, key: Optional[str]
) -> Dict[str, Any]:
    square: Dict[str, Any] = {
        "id": node_id,
        "type": "wait_event",
        "key": TOPIC_KEY,
        "topics": list(topics),
        "minutes": minutes,
        "stage": stage,
    }
    if key:
        square["match"] = {"payload": key, "run": key}
    return square


def _action_square(node_id: str, action: StageAction, stage: str) -> Dict[str, Any]:
    return {"id": node_id, **action.model_dump(exclude_none=True), "stage": stage}
