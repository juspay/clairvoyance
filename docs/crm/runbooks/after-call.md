# The wait after a call — one queued call per run

*22 Sep 2026.* A call square queues its lead and moves on at once, so the
timer after it counts from the insert, not from the call. A run held
overnight queues its morning call, waits its gap, and queues the next
while the first is still in the dialler's queue. On a 6,000-run morning
that is a second lead per customer behind a pile that already takes an
hour to drain — seen on 23 Sep 2026 as a second wave of inserts exactly 30
minutes after the first.

## The shape

Nothing new in the engine: a listening wait after the call, hearing the
call's own report.

```json
{"id": "call-1", "type": "call", "template_id": "..."},
{"id": "after-call-1", "type": "wait",
 "topics": ["call.completed"], "key": "outcome", "minutes": 1440,
 "match": {"payload": "lead_id", "run": "lead_call-1"}}
```
with arrows `after-call-1 → gap-30m` labelled `else` and `timeout`.

- **The call square is today's.** It queues the lead, writes the lead id
  into the run's context as `lead_call-1`, and the run walks on to
  `after-call-1` in the same visit.
- **`after-call-1` waits for that call's report.** The telephony mirror
  writes a `call.completed` when the call ends, carrying `lead_id` and
  `outcome`. `match` compares the report's `lead_id` with the run's
  `lead_call-1` as text. A lead id is uuid5 of run, square and visit, so
  no other run, square or visit answers to it — two applications on one
  phone never hear each other's call, and a late report from an earlier
  call wakes nothing.
- **It branches on the outcome.** `key: outcome` makes the outcome the
  square's answer, so an author may draw `NO_ANSWER`, `BUSY`, `INTERESTED`
  arrows; `else` takes the rest, `timeout` fires when no report came in
  `minutes`. The example plan sends both to the gap.
- **The backstop must outlast the pile.** `minutes` is how long a queued
  lead may wait for a line before the run gives up on the report. Shorter
  than the dialler's drain time and runs time out of `after-call` while
  their lead is still queued — and queue the next call behind it, the
  very thing this shape prevents. 1440 in the example.
- **It listens for nothing else, so a letter during the call is not
  seen by this run.** No merchant topics and no window on `after-call`.
  A merchant letter that lands between queueing the call and its report
  finds no square to answer: a reply is written only to the square the
  run is standing on, and `after-call` hears only the report. The letter
  is recorded in the event table but the run keeps neither its facts nor
  its topic — call-2 speaks the offers from before the call. That window
  is the dialler's queue time, not the ring: up to ~80 minutes for the
  morning pile at 77 calls a minute. Goals are the exception: a goal
  letter ends the run on any square. Ruled 23 Sep 2026: accepted and
  documented; the merchant's next letter is heard on the gap, where the
  ladder starts over on it. One `match` per square is why the merchant
  topics cannot simply be added here: they match on the customer, the
  report on the lead — and re-routing from here would queue a second
  call while the first is still queued, the very thing this square
  prevents.

## What this change fixes

Every heard letter used to take the run's latest-letter pointer. A
report is a letter, so after `after-call-1` heard it the pointer named
that square, and the next call's payload — built from "the latest
letter's facts" — spoke the report: its outcome and lead id, with the
offers the merchant sent before the first call gone from the second (seen
live 16 Sep 2026).

`CALL_REPORT_SOURCES` (the record module's surface; today `telephony`)
names the sources whose letters are our own call reports. A report from
one of them answers its square and keeps its facts under it —
`facts_after-call-1_outcome` is readable by name — but never takes the
pointer. Where it lives: `entry.py::_reply_patch`, the one place a heard
letter's context patch is built.

## Rolling it out

1. Deploy api, event-worker, walker. The fix only decides what a heard
   call report writes, so it touches exactly the plans that already hear
   one: `cart-recovery-retry` and `cart-recovery-fallback` have an
   `after-call` square. Today their rescue call's lead carries the
   report's `outcome` at the top level; after this deploy it does not
   (it stays readable as `facts_after-call_outcome`). Open runs on those
   plans change with the deploy. Checked 23 Sep 2026: no live call
   template reads `{outcome}`, so nothing a customer hears changes.
   Every other plan and run is untouched.
2. Set the dialler template's `max_retry` to 0 for workflow templates: the
   ladder is the retry, and a dialler retry is a fresh lead no square is
   waiting for.
3. Publish the plan with an `after-call` square behind each call and
   `on_publish: migrate`. Runs already waiting keep their square and
   their alarm; the next call square they reach is followed by its wait.
   Publish after the morning sweep, not during it.

## Rolling back

Nothing to move. The words are today's; old code hears the report the
same way and only differs in taking the pointer again.
