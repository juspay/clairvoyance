"""The laws of a listening square's ``match`` — publish-time, pure.

A reply narrows to ONE run by comparing a field of the letter against a
field of the run (``match {payload, run}``). Every law here exists because
its absence fails SILENTLY: the plan publishes clean and the square is
either deaf for as long as it waits, or it wakes runs the letter was never
about. One customer with three open orders had her single CANCEL resolve
all of them (10 Sep 2026, docs/crm/reply-run-matching.md) — that is the
loud version; the rest of these are the quiet ones.

Scoped by the EDGE GRAPH, not by the node list: only a square with a send
UPSTREAM of it can be woken by a reply to that send, so a receive-first
square keeps the open default and is never refused.

Kept out of plans.py for the reason catalog_laws.py is: that file is the
plan lifecycle (gather -> validate -> apply), and these are one pure
concern with one subject.
"""

from typing import Dict, List

from app.crm.outreach.nodes.context import PROVIDER_MESSAGE_PREFIX
from app.crm.outreach.schemas import WorkflowDefinition

REPLY_TOPIC = "message.inbound"


def reply_match_problems(definition: WorkflowDefinition) -> List[str]:
    """PURE: everything wrong with how this plan's squares narrow a reply."""
    problems: List[str] = []
    for node in definition.nodes:
        if node.match is not None and node.type != "wait_event":
            problems.append(
                f"node {node.id}: match belongs to a wait_event — only a "
                "listening square hears a letter"
            )
        if (
            node.type == "wait_event"
            and node.match is None
            and node.topics == ["message.inbound"]
            and (upstream := _upstream_sends(node.id, definition))
        ):
            # The 2026-09-10 incident, at publish instead of in production:
            # a square waiting for the reply to a send UPSTREAM of it,
            # without saying whose, falls to _is_about's open default — one
            # customer's answer resolves every open run of hers, and an
            # order she confirmed gets cancelled. Scoped three ways so it
            # cannot demand a match where one has no meaning: only the
            # reply topic; only when a send is upstream on the EDGE GRAPH
            # (a receive-first square beside an unrelated send branch has
            # no reply to mis-route, and the suggested match would be
            # unsatisfiable there); and only a single-topic square — a
            # mixed listen (reply OR call outcome) forced to match would
            # go deaf on its other topic, since match is judged per
            # letter. The suggestion names the UPSTREAM sends: a plan with
            # two sends must not be told to match the wrong one's id.
            names = " or ".join(
                f'"{PROVIDER_MESSAGE_PREFIX}{send}"' for send in upstream
            )
            problems.append(
                f"node {node.id}: listens on message.inbound with no match — "
                f"a reply would wake EVERY open run of the customer, not the "
                f'one it answers. Add match: {{"payload": "replied_to", '
                f'"run": {names}}} (the send square whose reply this square '
                f"waits for). A customer who TYPES a fresh message instead "
                f"of replying carries no thread id and will not match — the "
                f"timeout edge is that path."
            )
        if (
            node.type == "wait_event"
            and "message.inbound" in node.topics
            and len(node.topics) > 1
            and _upstream_sends(node.id, definition)
        ):
            # A mixed listen downstream of a send is unsafe in BOTH
            # directions, so it is refused whole rather than matched:
            # matchless, _is_about's open default takes one reply for
            # every open run (the incident, one door over); matched, the
            # same match is judged against every letter the square hears
            # and the other topic's letters — which carry no replied_to —
            # all claim nobody, deafening the square to the very outcome
            # it also waits for. Two squares each say one thing honestly;
            # the vocabulary has no shape that lets one square say both.
            others = [t for t in node.topics if t != "message.inbound"]
            problems.append(
                f"node {node.id}: message.inbound cannot share a square "
                f"with {', '.join(repr(t) for t in others)} when a send is "
                f"upstream — without a match one reply wakes every open "
                f"run; with one, the square goes deaf on the other topic. "
                f"Split it into two listening squares."
            )
        if (
            node.match is not None
            and node.match.run.startswith("provider_message")
            and not node.match.run.startswith(PROVIDER_MESSAGE_PREFIX)
        ):
            # A typo INSIDE the prefix sails past the send-node check below
            # (its startswith fails) and publishes a square that matches
            # nothing forever — the silent version of the incident.
            problems.append(
                f"node {node.id}: match.run {node.match.run!r} — did you "
                f"mean {PROVIDER_MESSAGE_PREFIX}<send node>? The stamp "
                f"writes exactly that prefix and nothing else"
            )
        if node.match is not None and node.match.run.startswith(
            PROVIDER_MESSAGE_PREFIX
        ):
            # The stamp (correlate.py) writes provider_message_id_<node>
            # only for a SEND node's accepted message. A misspelled square
            # name here would publish clean and then match nothing forever
            # — every reply "claims nobody" — which is the silent version
            # of the very failure match exists to prevent.
            sender = node.match.run[len(PROVIDER_MESSAGE_PREFIX) :]
            named = next((n for n in definition.nodes if n.id == sender), None)
            upstream = _upstream_sends(node.id, definition)
            if named is None or named.type != "send":
                problems.append(
                    f"node {node.id}: match.run {node.match.run!r} — "
                    f"{sender!r} is not a send node, so no provider id is ever "
                    "stamped under that name"
                )
            elif sender not in upstream:
                # A send the token has not crossed yet stamps nothing while
                # the run stands here, so every reply "claims nobody" and the
                # square is deaf until its timeout — the SILENT version of the
                # incident this match exists to prevent. The name is real and
                # the type is right, so only the edge graph can catch it.
                problems.append(
                    f"node {node.id}: match.run {node.match.run!r} names a send "
                    f"the run has not reached when it stands here, so nothing is "
                    f"stamped under that name and every reply claims nobody. "
                    + (
                        f"Upstream of this square: "
                        f"{', '.join(repr(PROVIDER_MESSAGE_PREFIX + u) for u in upstream)}"
                        if upstream
                        else "No send is upstream of this square at all"
                    )
                )
    return problems


def _upstream_sends(node_id: str, definition: WorkflowDefinition) -> List[str]:
    """PURE: the send squares a token could have crossed BEFORE standing on
    ``node_id``, walked backwards over the edge graph — what the no-match
    refusal scopes itself by. A reply can only answer a send that already
    happened, so a square with no send upstream has no reply to mis-route,
    whatever else the plan contains."""
    inbound: Dict[str, List[str]] = {}
    for edge in definition.edges:
        inbound.setdefault(edge[1], []).append(edge[0])
    by_id = {node.id: node for node in definition.nodes}
    seen: set = set()
    stack = [node_id]
    sends: List[str] = []
    while stack:
        for source in inbound.get(stack.pop(), []):
            if source in seen:
                continue
            seen.add(source)
            node = by_id.get(source)
            if node is not None and node.type == "send":
                sends.append(source)
            stack.append(source)
    return sends
