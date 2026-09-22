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
- **The reserved minutes.** A window may say `"held_runs_first_minutes": 90`. Every
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
- **What it does not do.** It does not stop a run from queuing a second
  call while its first is still in the dialler's queue, and it does not
  move a call's gap to count from the call's end — the timer still counts
  from the insert. Those are a call square that waits for its own
  report's job, a separate change; the offset only decides *when* the
  day's timers end.

## Sizing the offset

The pile must fit inside the offset at the number's pace, or live
customers still meet it. At 0.77 calls per line-minute a 100-line number
places about 77 calls a minute; 6,256 held runs are 81 minutes of it. Set
the offset to the pile's drain time with a margin — 90 for that pile on
that number — and watch the queue: if it is not empty when the offset
ends, the offset is too short or the number too small.

## Rolling it out

1. Deploy api, event-worker and walker. Nothing changes for open runs or
   published plans until a plan is published with the new word: a window
   without `held_runs_first_minutes` reads as 0, today's behaviour exactly.
2. Publish the plan with `held_runs_first_minutes` on its windows and
   `on_publish: migrate`, so the runs already waiting take the new timers
   as they next set one. Check the template's calling hours contain the
   plan's window.
3. Stop ejecting the pile by hand the next morning. Watch leads inserted
   per minute: one burst at the opening, then the day's live customers
   from the offset's end.

## Rolling back

Nothing to move. Old code reads a window with `held_runs_first_minutes` as a
window — the word is ignored, timers end when they used to. Alarms already
set with the offset stay where they are; the next timer a run sets is
computed by the code then running.
