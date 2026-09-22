# The call square that waits for its call

*22 Sep 2026.* A call square queues a lead and moves on at once, so the
timer after it counts from the insert, not from the call — and a run held
overnight queues its morning call, waits its gap, and queues the next
while the first is still in the dialler's queue. On a 6,000-run morning
that is a second lead per customer behind a pile that already takes an
hour to drain.

## What the engine does now

- **`"event_name": "call.completed"` on a call square.** The square queues
  its lead and, in the same write, stays on the square with the awaited
  lead in context (`lead_awaiting_<square>`) and the backstop as its
  alarm. It listens for that call's own report — the `call.completed` the
  telephony mirror writes when the call ends — matched on the lead id it
  queued, never on `match`, never another call of the customer's. When
  the report lands the square takes its one plain edge with the call's
  outcome on the step (NO_ANSWER, BUSY, INTERESTED …), and the next timer
  counts from the call's end.
- **The backstop.** `await_minutes` (default 1440). A report that never
  comes — dispatcher down, number unavailable — ends the wait: the square
  takes its plain edge with `timeout` on the step.
- **Off unless the plan says so.** A call square without `event_name` is
  today's square, exactly: queue and move on. A default cannot change
  what published plans do — a plan whose NEXT square listens for
  `call.completed` would otherwise see its report eaten by the call
  square.
- **One event, one edge.** The call's own report is the one event a call
  square can wait for (it is the only letter matched on a lead id), and
  a waiting call has exactly one plain edge and no labelled arrows.
  Publish refuses anything else.
- **A call report never becomes the run's latest letter**
  (`CALL_REPORT_SOURCES` = telephony): it answers its square and keeps its
  facts under it (`facts.<square>`, readable as `facts_<square>_<key>`),
  but the pointer that decides which letter's facts the next call speaks
  stays on the merchant's last word — so call-2 says what the merchant
  sent, not what call-1 reported.
- **Where it lives.** `nodes/call.py` (the word, the awaiting key, the
  report's outcome), `walker.py::_advance` (queue-then-wait in one write;
  the resolution on the next visit), `entry.py::_is_about` (the lead-id
  match) and `_reply_patch` (the pointer rule). No column, no migration.

## Rolling it out

1. Deploy api, event-worker and walker. Nothing changes for open runs or
   published plans until a plan is published with the word.
2. Set the dialler template's `max_retry` to 0 for workflow templates: the
   ladder is the retry, and a dialler retry is a fresh lead the square is
   not waiting for — it would ring behind the square's own next call.
3. Publish the plan with `event_name` on its call squares and
   `on_publish: migrate`. A run already standing on a call square keeps
   its alarm and moves on as before; the next call square it reaches
   waits.

## Rolling back

Old code does not know a waiting call square: a run standing on one is
visited again when its report or backstop wakes it, and the square is run
again — a second call. Before rolling the walker back, move those runs off
their call squares. Per plan, with the plain edge's target as `<next>`:

```sql
UPDATE crm_workflow_enrollment
SET current_node = '<next>', wake_at = now(),
    context = context - ('lead_awaiting_' || current_node) - ('reply_' || current_node)
WHERE workflow_id = '<plan>' AND status = 'waiting'
  AND context ? ('lead_awaiting_' || current_node);
```

The leads those squares queued are the dialler's as before: they ring
when their turn comes, and their reports wake nothing.
