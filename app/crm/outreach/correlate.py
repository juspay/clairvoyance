"""The send's provider id reaching its run (the reply join) — the call
square's plant-and-echo pattern applied to messages, on EVERY channel.

A call plants ``enrollment_id`` in the outbound lead and the outcome echoes
it, so a listening square can say WHOSE letter it hears. A message send
cannot plant anything a reply echoes: the only correlate a provider returns
on a reply is its own id for the message answered (Meta's wamid in
``context.id``; an email's Message-ID in ``In-Reply-To``) — issued at
ACCEPT time, after the run's context was written. So the join is completed
from the other side: when the dispatcher learns the provider's id, this
observer stamps it into the run's context as
``provider_message_id_<send node>`` — the T16 column's own name, one
spelling for every channel — and the square matches on its echo:

    {"match": {"payload": "replied_to", "run": "provider_message_id_confirm"}}

Without the stamp a listening square has no writable ``match`` and falls to
_is_about's open default — one customer's CANCEL resolved her three other
orders' runs (observed 2026-09-10; docs/crm/dev_only_reply_run_matching_
solution.md). ``provider_message_id_`` is a bookkeeping prefix
(nodes/context.py), so the id can never leak into a template.

Registered into connectivity's send-observer slot by app/crm/worker_main.py
— the retire-guard inversion, since connectivity may not import this module.
Total on purpose: the observer hears history, and nothing here may fail,
retry or double the send it heard about.
"""

from typing import Optional

from app.core.logger import logger
from app.crm.outreach.db.accessors import enrollment as enrollment_accessor
from app.crm.outreach.nodes.context import (
    parse_send_dedupe_key,
    provider_message_key,
)


async def note_send_accepted(
    *,
    merchant_id: str,
    source_kind: str,
    source_id: Optional[str],
    dedupe_key: str,
    message_id: str,
    provider_message_id: str,
    **_: object,
) -> None:
    """Stamp an accepted send's provider id onto ITS run, keyed by the
    square that sent it.

    Only workflow sends are this module's business; every other producer's
    acceptance returns at once (the same per-observer filtering record's
    consumers do). The write is one guarded UPDATE: a run already exited
    keeps its context — the stamp exists to route a FUTURE reply, and an
    exited run has no square listening. A miss is logged, not raised: by
    the observer contract nothing here may fail the send.
    """
    if source_kind != "workflow" or not source_id:
        return
    parsed = parse_send_dedupe_key(dedupe_key)
    if parsed is None or parsed[0] != source_id:
        # Not the send node's "<run>:<node>" shape — a producer this
        # observer does not understand must not be guessed at.
        logger.warning(
            f"provider-id stamp skipped: dedupe_key {dedupe_key!r} is not "
            f"<run>:<node> for run {source_id} (message {message_id})"
        )
        return
    run_id, node_id = parsed
    stamped = await enrollment_accessor.stamp_context_key(
        merchant_id,
        run_id,
        provider_message_key(node_id),
        provider_message_id,
    )
    if not stamped:
        # Exited between the send and the acceptance (a goal event can end
        # a run mid-flight) — nothing is listening, so nothing is owed.
        logger.info(
            f"provider-id stamp found no open run {run_id} (message {message_id})"
        )
