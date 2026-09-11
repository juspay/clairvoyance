# A reply wakes only the run it answers

*The incident of 10 Sep 2026, the reason `_is_about` could not decide it, and the
correlation this repo now plants so it can.*

## What happened

One customer had several COD orders open at once, so several runs of the confirmation
board were live for her at the same time. She tapped **Confirm** on one message. Every
open run took that tap as its own answer: one of them was standing on a square whose
`CANCEL` arrow led to a cancellation, and a confirmed order was cancelled on Shopify.

## Why no one could have written the plan differently

A listening square narrows a letter to one run with `match {payload, run}`: a field of
the letter compared against a field of the run. `canon/06-outreach.md` gives the keyed
example — two open orders, two live WISMO flows, matched on the order key both sides
carry.

**For a WhatsApp reply there was no comparable pair.** The letter carries Meta's `wamid`
of the message being answered (`context.id`, read by each source's own `replied_to`
deriver). The run carried OUR `crm_message` id, stamped by the send square when it queued
the message. Those are different identifiers, issued at different times: our id exists
before the send, Meta's only exists after the provider accepts it. There was no value to
compare, so the plan carried no `match`, and `_is_about` did what it documents — with no
match word, every letter on the topic is hers.

The default is right. What was missing was something to match on.

## The fix, in one line

**The provider's id for a send lives on the manifest row. Resolve it onto the run the
first time a square needs it, and match on the echo.**

| Piece | Where | What it does |
|---|---|---|
| `provider_message_id_for` | `connectivity/queue.py`, on the contract | The provider's own id for one logical send, read by the producer's OWN name for it (`dedupe_key`), filtered to the post-accept ladder |
| `_with_send_correlate` | `outreach/entry.py` | On the first reply a square must narrow, reads that id and writes it onto the run as `provider_message_id_<send node>`, so the next letter costs nothing |
| the match | the plan document | `match: {payload: replied_to, run: provider_message_id_<send node>}` |

`replied_to` is each source's own deriver, so a second channel needs no change here.

### Why it is read at reply time, and not pushed at accept time

The dispatcher learns the provider's id and could push it onto the run there and then,
through an observer slot. That was the first shape of this fix, and it was removed before
merge. Three reasons:

- **A reply is a cold path.** One customer answering one message, against a point read on
  a unique index. The push saves that read and costs a cross-module slot, a Protocol, a
  dispatcher hook, a deadline dial and a registration line.
- **A pushed value is a cache to keep coherent**, and this one is not free to keep: the
  walker's advance rewrites a run's context wholesale, so the push can be silently lost.
  The pushed design had to carry a reconcile-from-the-manifest path anyway, for the same
  row this now reads.
- **The reconcile would then run rarely**, which is the worst property a recovery path can
  have. Reading on demand means the one path runs on every reply, so it is exercised
  constantly rather than discovered during an incident.

The memo on the run is still written, so the read happens once per send, not once per
letter. Losing that write costs one read next time and nothing else.

## The manifest is the truth

The read trusts only the post-accept ladder, spelled from `connectivity/status.py`: some
providers return an id beside a refusal, and a message nobody received is not something a
reply can be answering. `_answered_by` judges the same resolved run as the wake did, so a
wake is never also a repeat.

This is also why **a run opened before this shipped resolves**: its `crm_message` row already
carries the wamid, so its first reply finds it.

## What stays closed

- **No stamp and no manifest id** → the run keeps waiting. It never takes another run's
  letter; the timeout edge recovers her.
- **An unthreaded message** (a typed reply with no `context.id`) claims nobody.
- **A dedupe key that is not `<run>:<node>`, or names another run**, is skipped, never
  guessed.

## What publish refuses, and why

The guards are scoped by the edge graph, because only a square with a send **upstream** of
it can be woken by a reply to that send:

1. A single-topic `message.inbound` square with a send upstream and **no match** — refused,
   with the exact line to add and the upstream sends named.
2. A **mixed** `message.inbound` listen behind a send — refused whole: without a match one
   reply wakes every run, and with one the square goes deaf on the other topic. Split it.
3. `match.run` that does not name a **send** node — nothing is ever stamped under that name.
4. `match.run` naming a send the token **has not crossed** when it stands on this square —
   publishes clean and is deaf forever, the silent version of the incident.
5. A typo inside the prefix gets a did-you-mean.
6. `match.payload` must be declared on every topic the square listens to.

A receive-first square (no send upstream) stays legal, unchanged.

## Deploying it

The live COD plan has no `match`, and runs already open are pinned to that version, so they
keep the open default until they exit.

1. Republish the plan with the match line and `on_publish: migrate`. The square exists and
   the entry is unchanged, so the migrate validator allows the re-pin of open runs.
2. Each old run's stamp fills itself from its manifest row on the next reply.

A worked document is `plans/cod-confirm.json`.

**Known gap, filed separately:** loom's workflow editor knows `match` on its type but cannot
author it, so a merchant drawing send → wait-reply in the console meets the new refusal with
no way to satisfy it in the UI.
