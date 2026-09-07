"""split — deterministic percentage branches for experiments (enh A/04).

A square that sends a share of runs down each arm: "half get the discount
message, half get the plain one". No waiting, no read, no randomness at
fire time — the arm is a pure function of the run and the square, so a
lease retry after a crash lands the SAME run in the SAME arm and an
experiment cannot double-count anyone.

The unit is the RUN, not the customer: a re-enrolled customer may be
re-randomised, which is correct for a per-run experiment and is the one
thing about this square worth saying out loud to an author.

Unlike ``condition``, there is no ``else``: the arms' percents sum to 100
(the validator says so at publish), so every bucket lands on an arm and
"nothing matched" cannot happen.
"""

import hashlib
from typing import Any, Dict, List

from app.crm.outreach.nodes.context import reply_key, split_key
from app.crm.outreach.schemas import (
    EnrollmentRun,
    SplitArm,
    WorkflowDefinition,
    WorkflowNode,
)

#: How finely the arms may be cut. 100 buckets = whole percents, which is
#: what the schema accepts; a finer grain would let an author write a share
#: the validator's "sum to 100" law cannot express.
BUCKETS = 100

#: How much of the digest the bucket is taken from. 32 bits is far more
#: entropy than 100 buckets need; taking a fixed prefix keeps the arithmetic
#: identical on every machine and every version of Python.
_DIGEST_CHARS = 8


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    """Two arms at least, whole percents summing to 100, labels distinct,
    each labelled with an edge and none of them dead.

    A 0% arm is refused rather than allowed-but-never-chosen: the walker
    could never pick it, so its edge is structure that cannot run — the
    same thing ``condition`` refuses when a rule has no edge."""
    problems: List[str] = []
    arms = node.arms
    if len(arms) < 2:
        return [
            f"split node {node.id} needs at least two arms — one arm is not a split"
        ]

    seen: set = set()
    for arm in arms:
        if arm.on in seen:
            problems.append(f"split node {node.id}: label {arm.on!r} twice")
        seen.add(arm.on)
        if arm.percent <= 0:
            problems.append(
                f"split node {node.id}: arm {arm.on!r} is {arm.percent}% — an arm "
                "no run can reach; remove it or give it a share"
            )

    total = sum(arm.percent for arm in arms)
    if total != BUCKETS:
        problems.append(
            f"split node {node.id}: arms add up to {total}%, not 100% — every run "
            "takes exactly one arm, so the shares must be whole"
        )

    labels = {on for _, on in definition.outgoing().get(node.id, [])}
    for arm in arms:
        if arm.on not in labels:
            problems.append(
                f"split node {node.id}: arm {arm.on!r} has no edge labelled with it"
            )
    return problems


def bucket_of(run_id: str, node_id: str) -> int:
    """PURE: which of the 100 buckets this run falls in at this square.

    Keyed on the SQUARE as well as the run, so two experiments on one board
    are independent — the same run is not pushed into the "A" side of both
    by an accident of its id.
    """
    digest = hashlib.sha256(f"{run_id}:{node_id}".encode()).hexdigest()
    return int(digest[:_DIGEST_CHARS], 16) % BUCKETS


def arm_for(arms: List[SplitArm], bucket: int) -> str:
    """PURE: the arm a bucket lands on — the first whose cumulative share
    passes it, arms in DOCUMENT order.

    Document order is load-bearing and deliberate: it is what makes the
    assignment stable when an author edits the shares. The last arm also
    catches a bucket the shares failed to cover, which the validator
    forbids at publish but a version row pinned before this law could
    still hold — a run walks the document it entered on, and it must
    walk, not raise.
    """
    ceiling = 0
    for arm in arms:
        ceiling += arm.percent
        if bucket < ceiling:
            return arm.on
    return arms[-1].on


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Choose the arm, and say it twice.

    ``reply_<node>`` is how every branching square answers, so pick_next
    and the reply clearing on advance treat a split exactly like a
    condition. ``split_<node>`` is the same word kept as a FACT: the reply
    is cleared when the token leaves the square, and a report that groups
    runs by arm has to read it days later.
    """
    chosen = arm_for(node.arms, bucket_of(str(run.id), node.id))
    return {reply_key(node.id): chosen, split_key(node.id): chosen}
