# The overnight drain — hot and cold runs, one queued call per run

*21 Sep 2026; revised 22 Sep after review.* Every wait square with a
calling window moves a timer that ends after the hours to the next
opening, and every run held that night lands on the same second. On 21 Sep
2026 that was 6,256 runs of one plan, each queuing a call the moment the
walker reached it at 09:00, on a number that places about 23 calls a
minute. Until this shipped the pile was ejected by hand every morning.

## What the engine does now

- **Lane.** A run a window holds overnight is marked `cold`
  (`crm_workflow_enrollment.lane`, migration 077). A merchant letter that
  wakes or refreshes the run makes it `hot` again: the customer acted now.
  So does queuing its call: from the dialler's door on the run holds a
  line, and its report, its gap and its next call are ordinary work.
- **Hot first.** The walker claims every due hot run before any cold one,
  with no check at all.
- **Cold, oldest held first.** Then it claims cold runs by the time they
  reached the held square: the customer held at 21:05 is dialled before
  the one held at 06:00. Unclaimed cold runs keep their 09:00 alarm; only
  a claim moves it. Before claiming a plan's cold runs it reads how many
  lines the plan's numbers have free, and claims none while there are
  none — so a full dialler costs nothing.
- **A cold call is written only into a free line.** At the call square a
  cold run's lead is inserted by ONE statement whose condition is the
  count of calls already holding the number's lines — queued, retrying or
  on the line, on every template that dials through that number —
  against the number's `maximum_channels` (`outreach/capacity.py`,
  `nodes/call.py`). No row back means no line: the run stays on the
  square, due now, and tries again next pass. Because the count and the
  write are one statement there is nothing to guess about what another
  walker holds; with one walker replica it is exact, with N replicas the
  over-fill is at most N−1 calls at an instant. The number is the
  template's own pinned number, nothing else: a call template with no
  number pinned makes its plan unsizable, logged every cache period, and
  its cold runs are not claimed until it is fixed. A plan with no call
  square needs no line and is claimed cold freely. Hot calls are written
  unconditionally. Nothing here reads Redis or the dialler's semaphore.
- **One queued call per run.** A call square that says `"await": true`
  waits for its own call's report (`call.completed`, matched on the lead
  it queued) before taking its edge; `await_minutes` (default 1440) is the
  backstop. Off by default: a plan opts in. The next timer counts from the
  call's end, not from the insert. If the report never comes, the queued
  lead is aborted and the run moves on. INVALID_PHONE and BLACKLISTED end
  the run as `ejected` with the outcome on its last square, because this
  customer cannot be called. Every other failed outcome — a misconfigured
  number, no config, a pre-check that ran out, the reaper's UNKNOWN after
  a call that was placed — takes the plain edge like a no-answer.
- **A new event beats a queued call.** An awaiting call square may list
  the merchant's `topics` with labelled arrows (usually back to the plan's
  rule square). A letter on one aborts the queued call if it is still
  BACKLOG, a ringing call is left to end on its own, and the run follows
  the arrow with the new facts as `hot`. The abort looks again for a
  moment when the dialler had the lead locked, so a superseded call
  cannot survive a deferral.
- **A run that ends takes its queued calls with it.** goal, withdrawn,
  converted_elsewhere, timed_out and ejected all abort the run's BACKLOG /
  RETRY leads (the finished hooks fire, so the mirror reports ABORT). Never
  `completed`: a plan whose last square is a call must still make it.
- **Retries inherit the run, nothing else.** A dialler retry of a run's
  call carries workflow_id, enrollment_id and lane from its parent, and
  no other meta_data; a retry of any other lead carries none.
- **The progress line.** Every `CRM_DRAIN_ALERT_INTERVAL_SECONDS` (60)
  the walker logs and posts to Slack (`SLACK_WEBHOOK_URL`), per merchant:
  cold calls placed this drain, on the line now, still queued, cold runs
  still waiting, and the total left. The drain begins when the plan's
  window last opened, on the window's own clock. When the total reaches
  zero it says "drained" once and goes quiet. **Run one walker replica**:
  every replica would post the same line.

## Rolling it out

1. Build the three indexes CONCURRENTLY on prod, then run migrations 077
   and 078 (their `IF NOT EXISTS` makes the second create a no-op).
2. Check every workflow call template has its telephony number pinned and
   that number carries its real `maximum_channels`. A NULL reads as one
   line; a 0 means that plan's cold runs are never claimed.
3. **Deploy at night, outside every plan's window, and let the old walker
   pods drain before any window opens.** An old walker claims cold runs
   with no lane and no room, and a new plan's awaiting call square is a
   square an old walker would run again. Deploy api, event-worker and
   walker together; walker at one replica.
4. **Backfill the pile that already exists.** Runs holding a window's
   opening alarm when this deploys carry the default `hot`. This assumes
   the night deploy above and plans that open at 09:00 Asia/Kolkata,
   which every Flipkart plan does; a plan with another opening gets its
   own copy of this statement with its own time:

   ```sql
   UPDATE crm_workflow_enrollment
   SET lane = 'cold'
   WHERE status = 'waiting' AND wake_at > now()
     AND (wake_at AT TIME ZONE 'Asia/Kolkata')::time = '09:00';
   ```

5. Set the dialler template's `max_retry` to 0 for workflow templates: the
   ladder is the retry, and a dialler retry is a fresh lead the square is
   not waiting for.
6. Abort the dead BACKLOG rows of abandoned merchants and raise
   `BB_RECONCILE_BACKLOG_LIMIT` (DevCycle) so one reconciler tick copies a
   whole morning's cold calls into Redis.
7. Publish the plan with `"await": true` on its call squares, their
   `topics` and arrows, and `on_publish: migrate`. Check the template's
   calling hours contain the plan's window, or cold leads bounce five
   minutes at a time inside the dialler and hold the lines.
8. Stop ejecting the pile by hand the next morning. Watch the progress
   line: cold calls placed per interval (steady, at the lines' pace), the
   count on the lines never far above `maximum_channels`, and the time it
   says drained. If that time drifts later day after day, buy channels —
   the room allocates them, it cannot make them.

## Rolling back

Old code does not know an awaiting call square: a run standing on one is
visited again when its report or backstop wakes it, and the square is run
again — a second call. Before rolling the walker back, move those runs off
their call squares and abort what they queued. Per plan, with the plain
edge's target as `<next>`:

```sql
-- the calls those runs still have queued
UPDATE lead_call_tracker SET status = 'FINISHED', outcome = 'ABORT',
       meta_data = meta_data || '{"outcome": {"abort_reason": "rollback"}}'
WHERE enrollment_id IN (
    SELECT id::text FROM crm_workflow_enrollment
    WHERE workflow_id = '<plan>' AND status = 'waiting'
      AND context ? ('lead_awaiting_' || current_node))
  AND status IN ('BACKLOG', 'RETRY') AND is_locked = FALSE;

-- the runs themselves, on to the next square, due now
UPDATE crm_workflow_enrollment
SET current_node = '<next>', wake_at = now(), lane = 'hot',
    context = context - ('lead_awaiting_' || current_node) - ('reply_' || current_node)
WHERE workflow_id = '<plan>' AND status = 'waiting'
  AND context ? ('lead_awaiting_' || current_node);
```

Then roll back. A ringing call ends on its own and its report is filed
against the lead only.

## What to expect

With 100 lines and the dialler's measured 0.77 calls per line-minute the
number places about 77 calls a minute. Hot demand of ~33 a minute after
09:15 leaves ~44 a minute for the pile, so the 21 Sep pile (6,256) clears
by about 11:20 while every hot lead is dialled within one call's length
of its insert. With one 30-line number the hot demand alone exceeds the
number and no scheduling clears a pile.
