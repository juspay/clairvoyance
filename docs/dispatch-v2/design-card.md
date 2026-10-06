# v2 event dialler: design card (keys, scripts, rules)

The single reference for the v2 dialler's Redis keys, Lua scripts and rules. Code comments cite it as
"design card §N" and "rule N"; the numbers below are the ones the code uses. Review ids the code cites
("Fable M2", "x7b", "D14", ...) are explained in [review-notes.md](review-notes.md). Operator steps are in
[runbook.md](runbook.md).

**State:** verified against the code on branch `feat/v2-act-stage` (6 Oct 2026): the act stage of spec
`2026-10-05-v2-act-stage-redesign` (one ticket list, one acceptor per pod, one coroutine per ticket). v2 ships switched off
(`BB_DISPATCH_V2_ENABLED` unset); today's dialler is unchanged until v2 is first used (rule 23).

**Design in one paragraph:**
- Leads wait in a **waiting room per template**.
- Each phone number keeps a **busy list** of who holds its lines.
- Whenever something changes on a number (a lead added, a call ended, a line given back), the code that made
  the change runs **match** for that number, inside Redis, as one atomic script.
- match pairs free lines with due leads and turns each pair into a **ticket**.
- Every ticket goes on **one list**, `bb:tickets`, in issue order. Each dialler pod's **acceptor** pops it in
  batches and runs **one coroutine per ticket**: claim the ticket, run today's checks, dial. No pool: a slow
  merchant holds only its own lines.
- A **1-second sweep** handles what no event does at the right moment: a lead's due time arriving.

**Decisions:** rooms keyed by template; a number's facts (`max`, `mode`) stored once per number; a line is
released on the first "call ended" signal; no token lists and no DB counter gate for v2 numbers; Twilio (and
Exotel) stay on today's path; a full number serves its oldest due lead first (§7).

**Single-node Redis (decision D14):** match touches a number's keys and its templates' rooms in one script.
Verified 5 Oct 2026: prod `voice-redis` is Memorystore STANDARD_HA, Redis 7.2, one primary (not cluster mode).

---

## 1. Keys

| Key | Type | Holds | Written by | Read by |
|---|---|---|---|---|
| `bb:route:{T}` | hash | template → `{number, tier, start, end, enabled, reseller}`. `start`/`end` = calling hours in IST seconds of the day (`""` = always open); `number` `""` = no number resolved (today's path); `tier` = `high` / `medium` / `normal` from the merchant id lists `BB_V2_TIER_HIGH_MERCHANT_IDS` / `BB_V2_TIER_MEDIUM_MERCHANT_IDS`. **No `mode`** (rule 21) | `routes.ensure_route` on first use (today's number rule `_get_available_number`), template / config / number save hooks, the route refresh (`BB_V2_ROUTES_REFRESH_S`, 10 min) | enqueue, match, reaper |
| `bb:num:{N}` | hash | `max` (= `maximum_channels`, NULL counts as 1), `provider`, `status`; `mode`, `mode_since_ms`, `stable_count`, `stable_sig`, `reseed_at_ms`, `handback_pending` (switch state); `seq` (ticket-id counter, rule 15) | facts: `routes.refresh_number` (save hook + every 5 s); switch state: **only** `switch.py` (through `scripts.switch_cas`); `seq`: match | enqueue, match, inbound admit, release, switch |
| `bb:numtpl:{N}` | set | templates whose room is (or was) routed to N | enqueue, route writes; match removes a template no longer routed to N | match, hand-back, route refresh |
| `bb:q:{T}` | sorted set | **waiting room**: lead id → due time (ms) | enqueue, reaper re-queue | match, orphan prune, hand-back |
| `bb:busy:{N}` | set | **busy list**: who holds each line (`lead:<id>` outbound, `call:<call_id>` inbound) | match (reserve), release, inbound admit, switch seed | match, ledger, monitor |
| `bb:tickets` | list | **tickets** of every number, in issue order: `"N\|L\|tk\|T\|issued_ms"` (a line on N reserved for lead L; `tk` = ticket id, T = its template) | match (append), the reaper's re-push and a stopping acceptor (head) | acceptors (`BLPOP` + `LPOP` batch), monitor |
| `bb:inflight:{N}` | hash | **lease** per ticket: lead → JSON `{t, issued_ms, tk, owner?, claimed_ms?, repushed_ms?, dialling_ms?}` (`owner` = the coroutine that claimed it) | match, `claim`, `mark_dialling`, reaper re-push | reaper, ledger, return / clear, monitor |
| `bb:due` | sorted set | number → when match can next issue on it (ms; rule 55) | match (its own entry: rewritten every run, removed when full, with nothing waiting or legacy), enqueue and the reaper's re-queue (`LT`: never later), a route write with waiting leads, a raised `max`, the kill switch back on, the hand-back (removed) | sweep tick (entries ≤ now), monitor |
| `bb:v2:active` | set | numbers in a v2-accounted mode, plus numbers whose hand-back is not finished | switch | every job, latch (rule 23) |
| `bb:v2:sweep:leader` | string | the sweep leader's lock | `LeaderElection` | sweep, monitor |
| `bb:dispatch:enabled` | string | mirror of the kill switch `BB_DISPATCH_ENABLED` | sweep `enabled_mirror` job (5 s) | match |
| `bb:reseller:paused:{R}` | string | **today's** reseller pause key; match reads it directly (rule 28) | today's code / by hand | match, monitor |
| `bb:epoch` | string | "Redis still holds v2's state": `<ms>`, or `legacy-recovery:<ms>` after a recount (rule 32). Missing = Redis restarted, failed over or was flushed | sweep recovery | sweep, today's worker (`queue.v2_owns_number`) |

**Free lines on N** = `bb:num:{N}.max − SCARD(bb:busy:{N})`. Computed every time, never stored.

**Lifetimes:** `bb:route:{T}` expires 2 days after its last write (`keys.ROUTE_TTL_S`); a missing route is
re-resolved on the next use. `bb:num:{N}` never expires: prod is `volatile-lru`, so a key with a TTL could be
evicted and a number's mode lost (rule 33). Busy lists, leases, tickets and rooms have no TTL.

---

## 2. Scripts (`dispatch/v2/scripts.py`; each runs atomically inside Redis)

Redis runs one script at a time, start to finish, so "count free lines" and "reserve a line" can't be split
and no two callers can take the same line. Every wrapper returns `None` on a Redis error; callers treat that as
"not done" and the sweep / reconcilers heal (decision D16). The scripts run on the dialler's own Redis client,
which never retries or sleeps (rule 50), by SHA1 (rule 57). `cap` = `BB_V2_MATCH_CAP` (default 100); a caller whose match issued
exactly `cap` runs it again until a run issues less (`match_all`: the enqueue path and the sweep tick).

**`enqueue(T, L, due_ms, cap, only_if_absent)`**: called by `queue.schedule_lead` (push API, CRM created hook,
retries, defers, dispatch-now, backlog reconciler). Returns tickets issued, or a refusal (`scripts.Enqueue`):
`-1` ROUTE_MISSING (rule 18), `-2` HOLDS_LINE: L holds a line on N (rule 17), `-3` NOT_V2: N not v2-accounted
(nothing written; the caller uses today's schedule). Every script's refusal codes are named in `scripts.py`
(`Enqueue`, `GiveBack`, `Reap`, `Mark`); the Lua comments use the same names.
```
N = bb:route:{T}.number                 -- missing → -1; "" → -3
mode(N) not v2_pending / v2 / draining  → -3
L in bb:busy:{N} or bb:inflight:{N}     → -2
only_if_absent and L already in bb:q:{T} → 0 (score untouched, no match)    -- rule 43
ZADD bb:q:{T} due_ms L ; SADD bb:numtpl:{N} T ; ZADD bb:due LT due_ms N
return match(N)
```

**`match(N, cap)`** (`v2_match`, also run inside enqueue, release, return_line, reap, release_stale):
```
stop if bb:dispatch:enabled = 0
mode(N) ≠ v2 → 0  (legacy / absent: also ZREM bb:due N; v2_pending / draining: an entry
        already due moves to now + BB_V2_DUE_RECHECK_S, XX GT)              -- rule 21
free = max − SCARD(bb:busy:{N}) ; free ≤ 0 → ZREM bb:due N, 0 (no template pass) -- rule 44
for T in bb:numtpl:{N}:
    route moved off N → SREM bb:numtpl:{N} T (and ZADD bb:due LT now its new number if T has waiting leads)
    open if route enabled, today's pause key for its reseller unset, and inside its hours
        (IST, inclusive both ends, wraps past midnight); an enabled room that is not open is
        re-checked: paused → now + BB_V2_DUE_RECHECK_S, hours closed → when they open
first ticket: read every open room's head (ZRANGE 0 0) and pick in the same pass;
later tickets: compare the remembered heads, re-read only the room just taken from      -- rule 47
pick the lowest (due − tier boost): high −10 min, medium −5 min; strict "<", so ties go to
    the first room in bb:numtpl order
repeat while free > 0 and issued < cap, at most cap + #open-rooms steps:
    ZREM the lead from its room
    lead already holds a line on N (busy or leased)? → only this copy is dropped
    else: SADD bb:busy:{N} lead:L ; tk = HINCRBY bb:num:{N} seq 1
          HSET bb:inflight:{N} L {t, issued_ms, tk}
          RPUSH bb:tickets "N|L|tk|T|issued_ms" ; free −= 1
bb:due N (rule 55) = now if the run hit the cap; else, with a line free, the earliest of every open
    room's head and every non-empty re-checked room's time; else (full, or nothing waits) ZREM
```

**`claim(N, L, tk, owner)`**: a dial coroutine, first thing. 1 = `owner` holds the ticket: the lease still
carries `tk` and no other owner has it; stamps `owner` and `claimed_ms`. The same owner's re-run answers 1, so
the app retries once after a lost reply (rule 36). A ticket delivered twice lets exactly one coroutine through
(rule 49).

**`return_line(N, L, tk, owner, not_placed, cap)`**: a coroutine ends without dialling. Only if L's lease still
has ticket id `tk` and is `owner`'s (`-1` `GiveBack.NOT_OURS` otherwise). A lease already marked dialling is
freed only when the provider said "not placed" (`-3` `GiveBack.DIALLING` otherwise: a call may exist, rule 26). Then `SREM busy`, `HDEL lease`, match.

**`mark_dialling(N, L, tk, owner)`**: the commit point before the provider request. 1 = dial, 0 = not our line
any more (lease gone, other ticket id, or not `owner`'s). A lease this owner already marked answers 1 (the
app's one retry after a lost reply, rule 36).

**`clear_lease(N, L, tk, owner)`**: after the call is stamped on the lead: the lease goes, `lead:L` stays busy
(the live call holds the line). `owner` empty = the lease reaper, which may clear any lease with `tk`.

**`repush_ticket(N, L, tk, older_than_ms)`**: the reaper's unclaimed tier. A lease nobody claimed (its pod died
after the pop, or the pop's reply was lost), issued or last re-pushed `older_than_ms` ago, and issued before
the entry at the head of `bb:tickets` (so it has left the list), goes back to the head of the list with its
line still held. Not while the kill switch is off (rule 52).

**`release(N, holder, cap)`**: a call ended (any signal; twice is harmless). `SREM bb:busy:{N} holder`, then
match. Returns `{removed, issued}`.

**`admit_inbound(N, call_id)`**: `SADD bb:busy:{N} call:<id>` if a line is free (already there = admitted).

**`reap_lease(N, L, tk, T, due_ms, cap, allow_dialling)`**: the reaper. Re-checks the lease inside the
script (race C); a dialling lease only with `allow_dialling`. Removes ticket, lease and busy holder; with `T`,
re-queues L in `bb:q:{T}` and lists T on the number it routes to now. Then match.

**`release_stale(N, L, cap)`**: the ledger. Removes `lead:L` only if L still has no lease (rule 19), then match.

**`move_room_to_schedule(T)`**: hands a room back to today's schedule (`bb:schedule:leads`) with its due
times, 1,000 leads per script call until the room is empty (rule 48).

**`switch_cas(N, mode, mode_since_ms, ops)`**: runs a switch step's writes (only `HSET`, `HDEL`, `SADD`,
`SREM`, `UNLINK`) all or none, only if `bb:num:{N}` still has the mode and `mode_since_ms` the step was
decided on (absent = `legacy` / `0`). Returns True applied, False stale (rule 46).

---

## 3. Who triggers match (nobody listens)

| What happens | Code that runs match right there | Pod |
|---|---|---|
| a lead is created, retried or deferred | `schedule_lead` → `enqueue` | API, dialler, CRM walker, voice-agent |
| a call ends (bot leaves, Plivo/Vobiz callback, stuck sweep) | `managers.calls._release_call_resources` → `release.release_lead_line` → `release` | voice-agent, API, dialler |
| a dial coroutine stops without dialling | `return_line` | dialler |
| a ticket's lease is reaped / a stale holder is freed | `reap_lease` / `release_stale` | dialler (leader) |
| **a due time arrives** (retry, delayed lead, hours open, unpause) | **the 1 s sweep**: match for every number whose `bb:due` time has come (rule 55) | dialler (leader) |

Each dialler pod's acceptor waits inside Redis on `BLPOP bb:tickets` (1 s, `BB_V2_TICKET_BLPOP_TIMEOUT_S`): no CPU
while waiting, one connection per pod.

---

## 4. The act stage (`dispatch/v2/acceptor.py` + `Worker._dispatch` with a `ClaimedTicket`)

One acceptor per dialler pod, one coroutine per ticket, no pool (spec 2026-10-05 §4.1-4.3). The only bound is
the per-pod memory guard `BB_V2_MAX_INFLIGHT_PER_POD` (2,000 dials in flight, decision D8): a full pod stops
popping and the other pods take the next tickets. An acceptor that has never seen v2 in use sleeps without
touching Redis (rule 23).
```
acceptor loop:
  BLPOP bb:tickets 1 s ; LPOP up to BB_V2_ACCEPT_BATCH − 1 more (≤ the pod's room)
  pod stopping, or the kill switch on (read once per batch) → LPUSH the batch back, oldest first  -- rule 24
  one dial_ticket coroutine per entry, each with its own Worker
dial_ticket(N, L, tk, T):
  claim(N, L, tk, owner) → 0: exit (void, or delivered twice)              -- owner: new per coroutine
  pod stopping → return_line, re-queue L now
  today's _dispatch checks for L, in today's order (read, BACKLOG?, reseller pause, lock, config,
      enabled, blacklist, hours, template, pre-checks, rate-limit, call limits, number == N?,
      phone, greeting pre-warm); config, template and number come from the pod's memo (rule 56)
      ── any exit without a dial → today's outcome / defer for L + the line given back first (rule 17)
      ── lock fails → give back; re-queue 30 s out only if still BACKLOG (rule 25)
      ── locked row's next_attempt_at > now + 5 s → give back, re-queue at its own time (rule 38)
      ── greeting pre-warm: started as a background task under the pod's TTS limit; the dial waits
         for it at most BB_V2_PREWARM_WAIT_S (rule 53)
  mark_dialling(N, L, tk, owner)                    (from here a call may exist)
      ── 0 → don't dial: take back the call-limit entry, give the line back (refused if the lease
         was in fact marked: the 10-min dial reap bounds it), unlock, re-queue the lead now
  dial row written first (#1280), then one request through the Plivo SDK, sent once (rule 54)
      ── 429 → the same request again after a jittered wait that doubles to 8 s, at most 60 s of 429s (rule 51)
      ── "not placed" → defer + return_line(not_placed)
      ── unknown (a 5xx, a 2xx without a call id, no complete reply) → keep the line; the lead is held
         PROCESSING with no call id (#1280's hold); its webhook claims it, or the stuck sweep re-queues it
  PROCESSING + call_id + telephony_number_id = N   (today's UPDATE)
  clear_lease(N, L, tk, owner)
stop (shutdown): stop popping; a batch popped meanwhile goes back to the head; dispatches still in their
  checks or greeting wait are cancelled (line back, lead re-queued); a throttled dial gives up at once
  (nothing placed); committed dials get the grace (25 s) and are never cancelled.
```
No extra DB write before dialling: a real dial costs the lock, the dial row and PROCESSING (+ FINISHED at the
end).

**A Plivo dial's outcome** (`PlivoProvider.make_call`, every caller, v2 or not; spec §10.3):

| What happened | Answer |
|---|---|
| 2xx with a `request_uuid` (or `call_uuid`) | placed |
| 2xx without one (body not JSON, only `api_id`) | unknown: held |
| any 5xx | unknown: held (`BB_PLIVO_5XX_OUTCOME`, decision D1, default `unknown`) |
| 429 | not placed; `throttled` when the v2 dialler asks (`report_throttle`) |
| other 4xx, an argument the SDK refuses before sending | not placed |
| connect timeout, refused, DNS (nothing sent) | not placed |
| read timeout, reset after sending, broken chunked / gzip body | unknown: held |
| any other error once a reply has arrived (reading it failed) | unknown: held |
| any other error before a reply | raised; the dispatch's failure path (not placed) |

---

## 5. Background jobs (`dispatch/v2/sweep.py`; the leader of `bb:v2:sweep:leader` runs them)

| Job | Every | Does |
|---|---|---|
| **Sweep tick** = the clock | 1 s | `bb:epoch` missing → recovery (rule 22 / 32). Else match the numbers whose `bb:due` time has come (at most `BB_V2_DUE_BATCH`, 5,000, earliest first) in one round trip (`match_many`), and every `BB_V2_DUE_FULL_PASS_TICKS` (30) ticks every number in `bb:v2:active` too (safety net: logs a number it found dialable that `bb:due` did not list); a number whose run hit the cap is filled at once (`match_all`). No SCAN |
| `switch` | 5 s | walks each configured / active number one step through the modes (§6b rule 22); writes via `switch_cas` |
| `enabled_mirror` | 5 s | `bb:dispatch:enabled` from `BB_DISPATCH_ENABLED`; switched back on, every active number is due now (match did nothing while it was off) |
| `number_facts` | 5 s | `max` / status / provider of every active number from the DB (rule 13); a raised `max` makes the number due now |
| `ledger` | 30 s | frees holders whose lead is FINISHED (unless a placed call is still attached, rule 39) or BACKLOG, unlocked and without a lease; re-queues a freed BACKLOG lead at once; frees `call:` holders whose inbound call ended (or has no row on two checks); alerts when a live PROCESSING call is missing from a `v2` number's busy list on two checks. **Never adds**. Every number's holders and leases in one round trip, then the DB in one query per `BB_V2_LEDGER_CHUNK` (1,000) ids, whatever the number count (order per number unchanged: holders, leases, statuses, rule 11). That batch only finds candidates: their leases and then their statuses are read again just before each is freed, because the batch can be seconds old (review P2-2) |
| `lease_reaper` | 30 s | every number's leases in one round trip; three tiers (rule 14): **unclaimed** 30 s after issue (or the last re-push) → delivered again (`repush_ticket`, rule 52); **claimed**, not dialling, 180 s after `claimed_ms` (a lease the previous release took has none: `issued_ms`) → line freed, a BACKLOG lead re-queued and, if still locked, unlocked; **dialling** for 10 min → only the lease goes if the lead is PROCESSING, else as the claimed tier |
| `channels_mirror` | 30 s | DB `channels` of `v2` numbers = the DB's own PROCESSING count (rule 29) |
| `backlog` | 60 s | only while `bb:v2:active` is not empty: up to 5 pages × 1,000 due BACKLOG rows (a keyset cursor kept between runs, wraps around), rows of v2-accounted templates → `enqueue(only_if_absent)`, one pipeline per page (`schedule_backlog_v2`; each reply means what it does for `schedule_lead`, a missing route goes through `schedule_lead` alone) (rule 43) |
| `routes` | `BB_V2_ROUTES_REFRESH_S` (10 min) | re-resolves the routes of templates with waiting leads on active numbers (a backstop: the save hooks re-resolve on every edit); two round trips to find them |
| `orphan_prune` | 5 min | rooms whose template has no route or a non-v2 number → today's schedule; other rooms drop leads that are no longer BACKLOG, read in ZSCAN chunks of `BB_V2_PRUNE_CHUNK` (1,000) with one DB query per chunk |
| `monitor` | 15 s | alerts: the oldest live unclaimed ticket in `bb:tickets` waiting > 10 s for an acceptor (void entries skipped, read in chunks of `BB_V2_MONITOR_SCAN_CHUNK` (100) up to `BB_V2_MONITOR_SCAN_MAX` (1,000); a void run longer than that reports the head's age; not while the kill switch is off; a re-pushed ticket's age counts from its issue, not the re-push, so one left waiting after a re-push alerts on the next check); a `v2` number whose `bb:due` time passed > 5 s ago, on two checks (every match rewrites its entry to now or later, so nothing has matched it); every pod: no sweep leader for 10 s |

Every job runs as a background task with a timeout, never two copies at once. A sweeper that stops being the
leader cancels its running jobs; the new leader re-runs them (rule 46).

---

## 6. Rules that are easy to get wrong

1. **Release uses the lead's stamped number** (`telephony_number_id`), never the template's current route.
2. **Number changes re-resolve unpinned routes.** Today's fallback number rule depends on every number of a
   provider, so a number save re-resolves the routes of that provider's templates.
3. **A disabled number** drains to today's path, where its leads end `NUMBER_UNAVAILABLE` as today (rule 27).
4. **The legacy path keeps its own workers.** Today's promoter and workers keep running for numbers not on v2
   (Twilio, Exotel, numbers not switched). They bounce a v2-accounted number's lead to its room.
5. **Inbound** (Plivo) on a `v2` / `draining` number is admitted by `admit_inbound`
   (`SADD bb:busy:{N} call:<id>` if a line is free) and released the same way. In `v2_pending` today's DB gate
   admits (ruling C-concern 2).
6. **The kill switch** is read inside match from its mirror key.
7. **The stuck sweep** (#1280) asks Plivo whether a call really ended before releasing its line.
8. **Reseller pause set by hand counts.** Superseded by rule 28: match reads today's pause key itself.
9. **After a Redis loss, nothing is half-known when busy lists are rebuilt:** a desired number restarts at
   `v2_pending` (≥ 15 s, over Plivo's 15 s request timeout) before it is seeded; with the flags gone too, the
   leader waits 30 s before recounting (rule 32).
10. **A lost callback keeps a line busy until the stuck sweep** asks the provider whether the call ended.
11. **The ledger reads leases first, then the leads' status.** Otherwise it can see "BACKLOG" just before the
    dial coroutine writes PROCESSING, then "no lease" just after, and free a live call's line.
12. Retired with the act stage (6 Oct): one ticket list for every number leaves no number to strand.
13. **Number facts refresh every 5 s**, so `maximum_channels` edits are picked up quickly.
14. **The lease reaper has three tiers.** A ticket nobody claimed 30 s after its issue (or its last re-push)
    is delivered again (rule 52). A claimed ticket that never started dialling 180 s after its claim frees its
    line; a BACKLOG lead is re-queued and, if its dead coroutine left it locked, unlocked (the new ticket can
    lock it; a coroutine alive after all fails `mark_dialling` on the ticket id). A lease stuck in
    `dialling_ms` for 10 min is cleared (the pod died mid-dial): if the lead is PROCESSING only the lease goes,
    the call keeps the line and the stuck sweep owns it; otherwise as the claimed tier. The limits are
    constants in `static.py` (`BB_V2_UNCLAIMED_REPUSH_S`, `BB_V2_CLAIMED_MAX_AGE_S`, `BB_V2_DIAL_STUCK_S`).
15. **Every ticket has an id** (`HINCRBY bb:num:{N} seq 1`, kept in the lease as `tk`). `return_line`,
    `mark_dialling`, `clear_lease` and `reap_lease` act only if the lease still carries that id.
16. **No jitter on v2 due times.**
17. **Give the line back before re-queuing a held lead.** `enqueue` skips a lead that still holds a line, so a
    defer done while the line was held would be skipped and the lead lost until the backlog job.
18. **`enqueue` refuses when the template's route is missing** (`-1`); the caller re-resolves the route and
    retries once.
19. **The ledger removes a stale holder atomically** ("still no lease?" + remove in one script) and alerts on
    a live call missing from the busy list only on two checks in a row.
20. Superseded by rule 25.

## 6b. Rulings (4 Oct) — these win over §6 where they differ

21. **One source of truth for mode: `bb:num:{N}.mode`**: `legacy` (or absent) → `v2_pending` → `v2` →
    `draining` → `legacy`. "v2-accounted" = `v2_pending`, `v2`, `draining`: leads go to rooms, today's
    workers bounce them back there. Only `v2` issues tickets.
22. **The dynamic config is the switch.** `BB_DISPATCH_V2_ENABLED` + `BB_DISPATCH_V2_NUMBERS` say which numbers
    should be on v2; the leader moves each number one step per 5 s check, never sleeping inside a check:
    - **on:** `v2_pending` for ≥ 15 s and until the number's locked leads are the same on two checks → seed the
      busy list from live calls and locked leads → `v2` → re-seed once 20 s later (adds only);
    - **off:** `draining` until no ticket or lease is left → hand-back: rooms to today's schedule → mode
      `legacy` → DB `channels` = the DB's own PROCESSING count → today's tokens = `max − count` → rooms again.
      `handback_pending` marks a hand-back not finished; the next check re-runs it.
    A global off drains every number in the same check. A config read error changes nothing (ruling C-concern 4).
23. **Today's code is untouched until v2 is first used:** a per-process latch (`latch.v2_seen`, re-checked at
    most every 5 s, true once the flag is on or `bb:v2:active` is not empty) keeps every v2 hook a no-op.
24. **Kill switch:** the acceptor reads it once per batch, right after the pop; a batch popped while it is on
    goes back to the head of `bb:tickets`, unclaimed and in order. A stale read is bounded by one batch.
25. **Lease reaper at 180 s. Lock failure with a held line → give back, re-queue 30 s out, only if still
    BACKLOG** (never earlier than the row's `next_attempt_at`).
26. **`return_line` after `mark_dialling` is refused** unless the provider said "not placed".
27. **Only AVAILABLE Plivo numbers are desired.** Any other number drains to today's path. Vobiz stays on
    today's path too (Fable F-1): one lead can briefly hold tickets on two numbers (its template moved while a
    ticket was out and the backlog job re-queued it), and only Plivo's dial row, written BACKLOG → PROCESSING
    before the request, lets exactly one of them dial; Vobiz writes its row after the reply.
28. **Reseller pause is read from today's key directly** (`bb:reseller:paused:<reseller>`) inside match and
    the monitor. No mirror, no SCAN.
29. **The DB `channels` of a `v2` number is the DB's own PROCESSING count** (written every 30 s), so today's
    gate is right even if v2 stops without a hand-back (Redis flush, code rollback).
30. **`bb:route:{T}` carries a 2-day TTL refreshed on every write.** (For `bb:num` see rule 33.)
31. **Switch-on stability is judged on locked leads only**, read once per switch step. In `v2_pending`,
    outbound releases and inbound admits use today's path.
32. **Redis loss with the flags gone.** A process that saw `bb:epoch` and then finds it missing treats it as a
    loss: today's workers defer on every number while it lasts (`queue.v2_owns_number` returns None). If the
    flags still name v2 numbers, they restart at `v2_pending`. If not, the leader waits 30 s
    (`BB_V2_LOSS_RECOVERY_WAIT_S`), rewrites every Plivo / Vobiz number's DB `channels` and today's tokens from
    the DB's PROCESSING count, then sets the epoch to `legacy-recovery:<ms>`.
33. **No TTL on `bb:num:{N}`** (prod is `volatile-lru`).
34. **Known, today's code:** after a hand-back, today's token reconciler can mint more tokens than free lines
    (today's refusal loop, no over-dial). The hand-back can leave DB `channels` too high when calls end in the
    same milliseconds (under-dial); corrected by hand (runbook §5).
35. **Rolling deploys:** switch v2 on only when every API, dialler and voice-agent pod runs this code. Mixed
    pods with v2 on over-dialled in tests (peak 20 on 10 lines).

## 6c. Rules added after testing and review (4–5 Oct)

36. **A script run twice gets the same answer.** The dialler's client never re-sends (rule 50); `claim` and
    `mark_dialling` run once more themselves after a lost or timed-out reply, with the same owner, and both
    scripts answer that owner's re-run with 1 and any other owner with 0.
37. **`v2_seen` is single-flight:** one check at a time (1 s timeout); callers during a check wait for its
    answer instead of reading a stale "no".
38. **A v2 ticket never dials a lead before its due time:** if the locked row's `next_attempt_at` is more than
    5 s ahead (a stale queue copy), the line goes back and the lead is re-queued at its own time. Today's
    worker is unchanged.
39. **A placed call attached to a lead the merchant finished mid-dial** (`CALL_ATTACHED_AFTER_FINISH`) keeps
    its line in the ledger while it is younger than `BB_STUCK_SWEEP_MAX_CALL_MINUTES` (240).
40. **CRM workflow leads are scheduled at creation only when their number is on v2** (created-lead hook);
    every other lead is left to today's backlog reconciler.
41. **Shutdown:** the sweep stops first; the v2 acceptor (25 s grace, §4) and today's workers drain in
    parallel.
42. **v2 reads on today's paths are bounded at 1 s** (mode, epoch): a hung Redis can't stall a release or an
    inbound admit.
43. **The backlog job adds only missing leads** (`only_if_absent`): a lead already in its room costs one
    `ZSCORE`; its score is never rewritten, so a stale page can't move a deferred lead's due time earlier.
    A page's enqueues go in one round trip (1,000 per page, each its own script), not one per lead.
44. **match on a full number returns before its template pass.**
45. **An unreadable mode at call end releases on both sides:** the v2 holder (bounded 1 s) and today's
    accounting. Each is a no-op on the other kind of number.
46. **A switch step writes only if the mode is still the one it was decided on** (`switch_cas`), and a sweeper
    that stops leading cancels its jobs. A deposed leader's slow step changes nothing.
47. **match reads each room's head once per run**, not once per ticket: a 100-ticket burst on a number shared
    by 300 templates blocked Redis 70 ms before, 5.8 ms after (Redis SLOWLOG, 5 Oct); same leads chosen.
    One run also takes at most cap + #open-rooms steps: a copy of a lead that already holds a line is only
    dropped, so a room full of such copies would otherwise be walked whole in one script. The rooms left
    with copies keep a due head, so the next tick goes on.
48. **Rooms move to today's schedule 1,000 leads per script** (a 100k room blocked Redis 209 ms in one
    script; now ≤ 2.3 ms per call).

## 6d. The act stage (6 Oct; spec 2026-10-05)

49. **A ticket is claimed before anything else.** `claim(N, L, tk, owner)` stamps a fresh per-coroutine owner;
    `mark_dialling` and `return_line` act only for that owner, `clear_lease` for it or the reaper. A ticket
    delivered twice (a reaper re-push racing the first delivery) is claimed by exactly one coroutine, so one
    dial.
50. **The dialler's own Redis client never retries or sleeps** (`dispatch/v2/redis_client.py`): redis-py's
    default re-sends a command 3 times with 1-10 s sleeps. A socket timeout (`BB_V2_REDIS_SOCKET_TIMEOUT_S`,
    10 s) answers "not done"; every v2 script is idempotent by ticket id and owner.
51. **A 429 sends the same dial again** (`dispatch/v2/throttle.py`): a 429 proves Plivo placed nothing, so the
    same request (same `dial_ref`, same dial row) goes again after a jittered wait whose upper bound doubles per
    resend, 2 → 4 → 8 s (`BB_V2_THROTTLE_MAX_STEP_S`), or Retry-After if longer, for at
    most `BB_V2_THROTTLE_MAX_WAIT_S` (60 s, checked at load to stay under the claimed tier), then today's
    not-placed path once. The line, lock and lease stay held; nothing is written per 429; a stopping pod gives
    up at once.
52. **An unclaimed ticket is re-pushed only once it has left `bb:tickets`**: issued before the entry at the
    head of the list (the list is in issue order), and back to the head. A ticket still waiting (every pod at
    its in-flight guard) is never delivered twice. Tickets of one match run share their issue time; among
    them the number's ticket id gives the order, and a tie with another number's head waits for the next run.
53. **The greeting pre-warm runs beside the dial** (decisions D2, D10): it starts where today's does (after
    every gate), under a per-pod limit of `BB_V2_TTS_CONCURRENCY` syntheses (fail-open), and the dial waits for
    it at most `BB_V2_PREWARM_WAIT_S`; it goes on through the dial and the ring.
54. **Every Plivo request is sent once** (`NoResendClient`): the stock SDK re-POSTs a dial, a transfer or an
    MPC participant up to twice more on any 5xx. The outcome mapping is the table in §4.
55. **The sweep matches only numbers that can issue now** (`bb:due`; decided with the user, 6 Oct; it replaced
    the set of numbers with any waiting lead, which the tick matched every second). Every match
    rewrites its own number's entry (rule 47's head reads give it for free), and every other write that can
    make a number dialable lists it: enqueue and the reaper's re-queue at the lead's due time (`LT`), a template
    now routed elsewhere and a route write with waiting leads at once, a raised `max` and the kill switch back
    on at once. Every script that frees a line runs match, so a full number needs no entry. A paused reseller
    (today's key, no write tells v2) is re-checked every `BB_V2_DUE_RECHECK_S` (5 s), and so is a number still
    switching (`v2_pending` / `draining`: match issues nothing until the flip, which runs match), so neither
    sits at the front of every tick's range; a missed write is caught by the full pass every
    `BB_V2_DUE_FULL_PASS_TICKS` (30) ticks, which logs it and raises a throttled P1 (a missed write is a code
    bug, and nothing else watches for it).
56. **A v2 dial reads its template, call config and number from a per-pod memo** (`dispatch/v2/memo.py`):
    kept `BB_V2_DIAL_MEMO_TTL_S` (10 s; 0 = off), so 3 DB reads per dial become 3 per template per 10 s per
    pod. An edit reaches the template's dials within that; the route (which leads get a line) still changes at
    once through the save hooks, and a ticket whose number changed is re-queued as before once the memo turns
    over. Concurrent misses share one load; a None or an error is never kept; today's path reads per dial.
    What a dial already holding a ticket can see stale, for at most the TTL: `enable_calling`, the template
    disabled or deleted, its calling hours, its provider account and its number. That is acceptable because
    tickets are issued only from the route: the save hook rewrites the route at once, so new tickets stop or
    move immediately, and only coroutines already past their claim use the old value. A number that no longer
    matches the ticket's is caught at the mismatch check, which drops the template's memo entries (review
    P2-1), so a move costs one bounce, not a loop.
57. **Scripts are sent by their SHA1** (`EVALSHA`): `match` and its callers carry ~4 KB of Lua, which went
    over the wire on every call. The body travels only when Redis answers NOSCRIPT (first use, a restart, a
    failover, `SCRIPT FLUSH`), as one `EVAL` that also caches it; NOSCRIPT means the script did not run, so
    that is not a second run. One helper sends every v2 script, one or a pipeline of many (the sweep's
    match, the backlog reconciler's enqueues): one round trip, the body re-sent only for the NOSCRIPT replies,
    and a failed script answers None for itself alone. Today's dialler keeps its own scripts and client.

---

## 7. What does NOT change

- Every check, its order and its DB writes; outcomes and `meta_data`; webhooks and CRM events; retry rules;
  `telephony_number_id` stamped at PROCESSING. Postgres stays the source of truth.

**Visible changes to accept:**
- `dispatched_at` = first time a line was free;
- terminal-without-call outcomes wait for a free line;
- pre-check HTTP runs less often;
- retries and delayed leads fire within ~1 s of their due time;
- **a full number serves its oldest due lead first**, across all templates on it (today's ready list is
  last-in-first-out). Same call count; the order changes. On a number shared by many templates, one
  merchant's pile is served before a later merchant's leads (review finding 1, owner decision pending).
