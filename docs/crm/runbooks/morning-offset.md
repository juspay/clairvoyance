# The morning offset — the night's pile first, then the day

*22 Sep 2026.* Every wait square with a calling window moves a timer that
ends after the hours to the next opening, and every run held that night
lands on the same second. On 21 Sep 2026 that was 6,256 runs of one plan,
each queuing a call the moment the walker reached it at 09:00, on a number
that places about 23 calls a minute, while customers acting that morning
queued behind them. Until this shipped the pile was ejected by hand.

## What the engine does now

- **The pile still wakes at the opening.** Nothing changes for the runs a
  window held overnight: at 09:00 the walker pushes every one of their
  calls into lead_call_tracker, and the dialler works through them at its
  own pace, first come first served.
- **The reserved minutes.** A window may say `"offset_minutes": 90`. Every
  timer that window governs and that is SET inside the first 90 minutes
  after the opening runs 90 minutes longer: a customer entering at 09:20
  whose quiet-30m would end at 09:50 wakes at 11:20; a held run whose
  morning call ended at 09:12 and whose gap-30m would end at 09:42 wakes at
  11:12. After 10:30 nothing is added. So the pile has the lines to itself
  for the offset, and the day's live customers land behind it instead of
  beside it. The window's own hold still applies after the offset: a
  timer that then ends past the hours waits for the next opening. A timer
  set before the opening, at night, is held to the opening as always and
  gets no offset — that is the pile.
- **Where it lives.** `window.py::alarm`, the one place a wait's alarm is
  computed on arrival and at enrol. `in_reserved_period` is the test. No
  column, no migration, no counting, nothing read from the dialler.
- **One queued call per run.** A call square that says `"await": true`
  waits for its own call's report (`call.completed`, matched on the lead
  it queued) before taking its edge; `await_minutes` (default 1440) is the
  backstop, after which the queued lead is aborted and the run moves on.
  Off by default: a plan opts in, so no published plan changes meaning on
  deploy. The next timer counts from the call's end, not from the insert.
- **A new event beats a queued call.** An awaiting call square may list
  the merchant's `topics` with labelled arrows (usually back to the plan's
  rule square). A letter on one aborts the queued call if it is still
  BACKLOG — a ringing call is left to end on its own — and the run
  follows the arrow with the new facts; inside the reserved minutes its
  next timer gets the offset like any other.
- **A customer that cannot be called ends the run.** INVALID_PHONE and
  BLACKLISTED end the run as `ejected` with the outcome on its last
  square. Every other failed outcome — a misconfigured number, no config,
  a pre-check that ran out, the reaper's UNKNOWN after a call that was
  placed — takes the plain edge like a no-answer, so the ladder goes on.
- **A run that ends takes its queued calls with it.** goal, withdrawn,
  converted_elsewhere, timed_out and ejected all abort the run's BACKLOG /
  RETRY leads (the finished hooks fire, so the mirror reports ABORT). Never
  `completed`. A dialler retry of a run's call inherits workflow_id and
  enrollment_id from its parent and nothing else.
- **A call report never becomes the run's latest letter**
  (`CALL_REPORT_SOURCES`), so call-2 speaks the merchant's facts, not the
  report's.

## Sizing the offset

The pile must fit inside the offset at the number's pace, or live
customers still meet it. At 0.77 calls per line-minute a 100-line number
places about 77 calls a minute; 6,256 held runs are 81 minutes of it. Set
the offset to the pile's drain time with a margin — 90 for that pile on
that number — and watch the queue: if it is not empty when the offset
ends, the offset is too short or the number too small.

## Rolling it out

1. Deploy api, event-worker and walker. Nothing changes for open runs or
   published plans until a plan is published with the new words.
2. Set the dialler template's `max_retry` to 0 for workflow templates: the
   ladder is the retry, and a dialler retry is a fresh lead the square is
   not waiting for.
3. Publish the plan with `offset_minutes` on its windows, `"await": true`
   on its call squares with their `topics` and arrows, and
   `on_publish: migrate`. Check the template's calling hours contain the
   plan's window.
4. Stop ejecting the pile by hand the next morning. Watch leads inserted
   per minute (one burst at the opening, then the day's live customers
   from the offset's end) and the dialler's queue depth reaching zero
   before the offset ends.

## Rolling back

Old code does not know an awaiting call square: a run standing on one is
visited again when its report or backstop wakes it, and the square is run
again — a second call. Before rolling the walker back, move those runs off
their call squares and abort what they queued. Per plan, with the plain
edge's target as `<next>`:

```sql
UPDATE lead_call_tracker SET status = 'FINISHED', outcome = 'ABORT',
       meta_data = meta_data || '{"outcome": {"abort_reason": "rollback"}}'
WHERE enrollment_id IN (
    SELECT id::text FROM crm_workflow_enrollment
    WHERE workflow_id = '<plan>' AND status = 'waiting'
      AND context ? ('lead_awaiting_' || current_node))
  AND status IN ('BACKLOG', 'RETRY') AND is_locked = FALSE;

UPDATE crm_workflow_enrollment
SET current_node = '<next>', wake_at = now(),
    context = context - ('lead_awaiting_' || current_node) - ('reply_' || current_node)
WHERE workflow_id = '<plan>' AND status = 'waiting'
  AND context ? ('lead_awaiting_' || current_node);
```
