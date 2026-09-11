# A reply wakes only the run it answers

*The incident of 10 Sep 2026, why no plan could have prevented it, and why the fix needs
nothing declared by anyone.*

## What happened

One customer had several COD orders open at once, so several runs of the confirmation board
were live for her at the same time. She tapped **Confirm** on one message. Every open run
took that tap as its own answer: one of them stood on a square whose `CANCEL` arrow led to a
cancellation, and a confirmed order was cancelled on Shopify.

## Why no plan could have been written differently

A listening square narrows a letter to one run with `match {payload, run}`: a field of the
letter compared against a field of the run. `canon/06-outreach.md` gives the keyed example —
two open orders, two live WISMO flows, matched on the order key both sides carry.

**For a WhatsApp reply there was no comparable pair.** The letter carries the provider's id
of the message being answered (`context.id`, read by each source's own `replied_to` deriver).
The run carried OUR `crm_message` id, stamped by the send square when it queued the message.
Those are different identifiers, issued at different times: ours before the send, the
provider's only after it accepted. There was nothing to compare, so the square carried no
`match`, and `_is_about` did what it documents — with no match word, every letter on the
topic is hers.

The default is right. What was missing was a value.

## The fix: a reply is ADDRESSED, not matched

**The manifest already knew.** Canon T16 records what caused every send — col 7 `source_kind`,
col 8 `source_id`, which for a workflow send *is the run* — and col 14 carries the provider's
own id for it under a partial UNIQUE that migration 056 describes, in its own words, as **"how
an inbound receipt finds this row"**.

A reply is a receipt of another kind. So:

```
reply.replied_to  ->  crm_message_provider_id_uq  ->  the row
                                                       source_id  = the run
                                                       dedupe_key = <run>:<node> = the square
```

One indexed read, on a letter that carries a thread. `outreach/reply_attribution.py` makes it;
`entry.py` resolves it once per letter, not once per run.

**It addresses the RUN, and deliberately not the square.** The manifest names the square that
SENT the message; the square that waits for the answer is a different one, the listener the
send's edge leads to. Which of the run's squares resolves is already decided, and decided
better: the resume statement moves a run only while its token is standing on that square, as a
WHERE rather than a Python branch.

**Nobody declares this.** Not the plan author, not the publish validator, not the console.
The fact was written by the code that sent the message.

### What this is not

`match` stays the general mechanism, and it is the right one wherever the letter genuinely
carries a fact ABOUT the run: a call outcome echoing `enrollment_id`, a keyed door's order id
on both sides. Attribution is narrower and stronger — it applies only to a reply to one of our
own sends, and there it needs no declaration at all.

## What stays exactly as it was

Attribution only ever NARROWS, so anything it cannot address behaves as it always did. That is
why **no published plan has to change** and no run has to be re-pinned:

- **She typed instead of tapping.** No thread on the letter, nothing to look up, every
  listening square hears it — as before.
- **She replied to a message this system never sent.** No row carries that id.
- **She replied to a broadcast, an agent's message, or a transactional one.** There is no run
  behind it, and guessing past the producer's own word is the cross-wake being prevented.
- **A workflow send whose producer name is not `<run>:<node>`.** The run is known, the square
  is not, and waking the wrong square is the failure — so the letter is left unaddressed.
- **A source that declares no `replied_to` field.** No read is made; nothing about that source
  changes. A source opts in by declaring the field, and gets attribution for free.
- **The run she answered has already ended** — its goal fired, it timed out, she was ejected.
  Narrowing to a run nobody is holding would silence the letter completely, so it is treated as
  unaddressed and her other runs hear it as before. The answer is late, not misdirected.

**One thing it does change, deliberately.** A square listening for *any* inbound message, in a
plan with no send before it, no longer hears a letter that answers a DIFFERENT open run's send.
It still hears everything else she says. That is the same narrowing this exists for, applied
where no `match` could have been written; if a plan genuinely wants every letter including other
runs' answers, that is a vocabulary question to settle when one appears.

## Deploying

Nothing to do. No plan republished, no migration, no re-pinning of open runs: a run opened
before this shipped is addressed by its own manifest row, which already carries the provider's
id.

A worked document is `plans/cod-confirm.json` — and note what it does **not** contain.
