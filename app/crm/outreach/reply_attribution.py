"""Which send a letter answers — and therefore which RUN.

A reply is not matched, it is ADDRESSED. The customer answered one message;
that message was sent by one run; and the manifest already wrote that down
when it sent it (canon T16 col 7/8), keyed by the provider's own id for it
(col 14, the partial UNIQUE migration 056 describes as "how an inbound
receipt finds this row"). A reply carries exactly that id.

So nobody declares this correlation. Not the plan author, not the publish
validator, not the console. One indexed read on a letter that carries a
thread, and the letter names its run.

The run, and NOT the square — see ``Addressed``. The manifest names the
square that SENT the message, and the square that waits for the answer is a
different one.

What this is NOT for: `match` stays the general mechanism, and it is the
right one where the letter genuinely carries a fact ABOUT the run — a call
outcome echoing `enrollment_id`, a keyed door's order id on both sides.
Attribution is narrower and stronger: it applies only to a reply to one of
our own sends, and there it needs no declaration at all.

What it narrows, and what it cannot: a customer with three open orders taps
CONFIRM on one of them, and the other two runs must not hear it — her single
tap once resolved all three and cancelled a confirmed order (10 Sep 2026,
docs/crm/reply-run-matching.md). Attribution only ever NARROWS, so a letter
it cannot address behaves exactly as it did before this existed. That is why
no published plan has to change and no open run has to be re-pinned.

A source opts in by declaring a ``replied_to`` field — the id of the message
this one answers, in whatever the provider calls it (Meta's context.id). A
source that declares none simply never addresses a letter this way, and
every square behaves exactly as it did before.
"""

from typing import NamedTuple, Optional

from app.core.logger import logger
from app.crm.connectivity.contracts import send_behind
from app.crm.record.contracts import (
    RawEvent,
    canonical_path,
    derive_for,
    field_value,
)

#: The declared field carrying "the id of the message this one answers".
#: A spec word, like `reply` — the one name every source spells the same, so
#: nothing here knows a provider.
REPLIED_TO_FIELD = "replied_to"

#: The producer word a workflow send carries on the manifest (T16 col 7) —
#: the same word nodes/send.py queues with. Spelled twice because the queue
#: is a contract call and the word is an argument to it, not an import; a
#: test pins the two equal, because if they ever diverged attribution would
#: simply stop addressing anything, silently, with every test still green.
_WORKFLOW = "workflow"


class Addressed(NamedTuple):
    """The run whose send this letter answers.

    The RUN, and deliberately not the square. The manifest names the square
    that SENT the message (its dedupe_key is ``<run>:<send node>``), and the
    square that waits for the answer is a different one — the listener the
    send's edge leads to. Narrowing by the sender would silence the very
    square that is listening.

    Which listening square resolves is already decided, and decided better:
    the resume statement moves a run only while its token is standing on
    that square, as a WHERE rather than a Python branch. Two squares of one
    run may be offered the same letter; at most one can be standing on it.
    """

    run_id: str


async def addressed_run(event: RawEvent) -> Optional[Addressed]:
    """PURE-ish (one indexed read): whose send this letter is a reply to.

    None for a letter that answers nothing of ours — she typed a fresh
    message, or the thread names a send this system never made. The caller
    then behaves exactly as it did before attribution existed, which is why
    this can be added under live plans without republishing one of them.

    None too when the send was a broadcast, an agent's or a transactional
    one: those have no run to wake, and guessing past the producer's own
    word would be the cross-wake this exists to prevent.
    """
    thread = field_value(
        event.payload,
        canonical_path(REPLIED_TO_FIELD),
        derive_for(event.source, event.topic),
    )
    if not thread:
        return None

    sent = await send_behind(event.merchant_id, str(thread))
    if sent is None or sent.source_kind != _WORKFLOW or not sent.source_id:
        return None

    # The run comes from source_id, which canon T16 col 8 defines as the
    # workflow_enrolment id. The producer's own name for the send is
    # "<run>:<node>" (nodes/send.py), so it must agree — a row where the two
    # disagree is a data fault, and acting on either half of a contradiction
    # is how a letter reaches a run it is not about.
    named_run, _, node_id = sent.dedupe_key.partition(":")
    if named_run != sent.source_id or not node_id:
        logger.warning(
            f"reply attribution: dedupe_key {sent.dedupe_key!r} disagrees with "
            f"source_id {sent.source_id} (event {event.id}) — not addressed"
        )
        return None
    return Addressed(run_id=sent.source_id)
