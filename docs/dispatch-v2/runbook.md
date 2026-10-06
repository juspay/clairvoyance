# v2 event dialler: runbook

Operator steps for switching numbers onto the v2 dialler and back. Keys, scripts and rules are in
[design-card.md](design-card.md); review ids in [review-notes.md](review-notes.md). Times in IST.
Every prod write below (config, SQL) needs the owner's explicit go. Redis checks read keys and counts only.

Durations come from the load-test harness (real dialler code, local Postgres and Redis, simulated Plivo);
prod adds its own latency, so treat them as lower bounds.

---

## 1. Deploy rules

1. **Deploy with v2 off.** `BB_DISPATCH_V2_ENABLED` unset or `false`, `BB_DISPATCH_V2_NUMBERS` empty. Until v2
   is first used, every v2 hook is a no-op: the acceptor and the sweep only check a latch every 5 s
   (rule 23).
2. **Never switch a number on during a rolling deploy, or while any pod runs code without v2.** Every API,
   dialler and voice-agent pod must run this build first: calls end on all three, and an older pod releases a
   v2 line through today's path. Measured: v2 on with 3 old + 3 new dialler pods over-dialled (peak 20 calls on
   10 lines); v2 off with mixed pods behaved like today (rule 35).
3. **Never roll back code while a number is on v2.** Switch v2 off first (§3), then deploy.
4. Check that v2 is idle after the deploy: `EXISTS bb:epoch` → 0 and `SCARD bb:v2:active` → 0. Each dialler pod
   logs `Started the v2 acceptor` once.

**Before the first number (one-time checks):**

| Check | Pass |
|---|---|
| Redis topology (`gcloud redis instances describe`, `INFO server`) | standalone primary, not cluster mode. **Verified 5 Oct:** Memorystore STANDARD_HA, Redis 7.2, one primary |
| Eviction policy (`CONFIG GET maxmemory-policy`) | `noeviction` or `volatile-*`. **Not** `allkeys-*`: busy lists, leases and `bb:num` carry no TTL and must never be evicted. Prod: `volatile-lru` |
| Where the v2 flags live | decided by the owner. Flags in DevCycle only: a Redis flush hides them; v2 then holds today's dialling ~31 s and recounts (rule 32). Flags also in each Deployment's env: v2 rebuilds itself after a flush |
| Plivo account calls-per-second limit | covers the peak dial rate (139 dials/s for 5,000 lines at 36 s per call), with headroom for a fill of many lines at once |

---

## 2. Switch a number on

1. Set `BB_DISPATCH_V2_ENABLED = true` and add the number's `telephony_numbers.id` to
   `BB_DISPATCH_V2_NUMBERS` (comma-separated; keep the earlier ids). Only AVAILABLE Plivo numbers
   switch (rule 27); a listed number on another provider stays on today's path and is logged once
   (`listed in BB_DISPATCH_V2_NUMBERS but is a … number`).
2. Do nothing else: the sweep leader does the whole switch (§6b rule 22). Don't write `bb:num:*` by hand.
3. What to expect:

   | Step | When | What happens |
   |---|---|---|
   | `legacy` → `v2_pending` | within ~5 s | the number's leads go to v2 rooms; **no new call starts on it**; calls ending still release through today's path |
   | `v2_pending` → `v2` | after ≥ 15 s, once the number's locked leads are the same on two checks | busy list seeded from live calls; v2 fills free lines; re-seeded once 20 s later |

   Measured: 15–20 s in `v2_pending` (up to 32 s on a busy number shared by 300 templates). Switch busy
   numbers on at a quiet minute, not at the start of a calling window.
4. Confirm: `HGET bb:num:<N> mode` → `v2`; log lines `v2 switch: <N> -> v2_pending`, then `-> v2`.
5. Stuck in `v2_pending` for minutes: its locked leads keep changing (a very busy number), or the config can't be
   read (`v2 switch … failed for <N>`). Waiting is safe; removing the number from the list hands it back at once.

---

## 3. Switch a number off, roll back

**One number:** remove its id from `BB_DISPATCH_V2_NUMBERS`. **Everything:** set `BB_DISPATCH_V2_ENABLED =
false` (every number drains in the same check).

1. The leader moves the number to `draining` within ~5 s (no new tickets), then hands it back once no ticket or
   lease is left: rooms → today's schedule, mode `legacy`, DB `channels` = the DB's PROCESSING count, today's
   tokens = `max − count`. Measured 5.0–5.5 s after `draining`.
2. Wait until it is done: `HGET bb:num:<N> mode` → `legacy` **and** `HEXISTS bb:num:<N> handback_pending` → 0,
   and `SISMEMBER bb:v2:active <N>` → 0. A hand-back that failed half-way keeps `handback_pending` and re-runs
   every 5 s.
3. Run the counter check (§5) for the number.
4. **Only then** deploy older code, if that is the plan (`SCARD bb:v2:active` → 0 first).

**If v2 couldn't be switched off before a rollback:** the old build's DB `channels` on those numbers is the last
v2 mirror write (up to 30 s old) and can be below the real count, which over-dials. Run §5 for every Plivo /
Vobiz number right after the rollback.

---

## 4. Redis checks (keys and counts only)

| Command | Healthy |
|---|---|
| `HGET bb:num:<N> mode` / `mode_since_ms` | the mode you expect; `v2_pending` never longer than a minute |
| `HGET bb:num:<N> max` | the number's `maximum_channels` |
| `SCARD bb:busy:<N>` | ≤ `max`, always; ≈ live calls on the number + tickets in flight |
| `LLEN bb:tickets` | near 0: tickets of every number not yet popped by an acceptor. Growing = dialler pods down, or every pod at `BB_V2_MAX_INFLIGHT_PER_POD` |
| `HLEN bb:inflight:<N>` | ≈ the number's dials/s × ~0.6 s (tickets being dialled); 0 when idle; never above `max` |
| `ZCARD bb:q:<T>` | waiting leads; due leads only while every line is busy |
| `ZSCORE bb:due <N>` | when match next looks at N (ms): absent while N is full or nothing waits; a time > 5 s in the past on a `v2` number = nothing is matching it (the P1 alert) |
| `SMEMBERS bb:v2:active` | the numbers you switched on (plus any hand-back still finishing) |
| `GET bb:v2:sweep:leader` | set (one leader); an empty value for > 10 s raises `[P0] Breeze Buddy v2: no sweep leader` |
| `EXISTS bb:epoch` | 1 once v2 is in use; 0 after a Redis restart until the leader's recovery sets it |

---

## 5. Counter check and the `channels` hand fix

After a hand-back (or a rollback without one), DB `telephony_numbers.channels` should equal the DB's own count
of calls holding a line on the number. That is the count the hand-back and today's token reconciler use
(`count_processing_by_telephony_number_query`). Read it **twice, a few seconds apart** (read-only):

```sql
SELECT n.id, n.number, n.maximum_channels, n.channels,
       (SELECT count(*) FROM lead_call_tracker l
         WHERE l.status = 'PROCESSING' AND l.telephony_number_id = n.id
           AND ((l.call_direction = 'OUTBOUND' AND l.execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST'))
                OR (l.call_direction = 'INBOUND' AND n.provider IN ('PLIVO', 'VOBIZ')))) AS processing
FROM telephony_numbers n
WHERE n.id = '<NUMBER_ID>';
```

- **Same on both reads:** nothing to do.
- **Off by ±1 on one read only:** not drift. A dial in flight holds its channel before its row turns
  PROCESSING; a call that ends gives its channel back a moment before its row turns FINISHED.
- **`channels` above `processing` on both reads:** the known hand-back drift (rule 34). The number dials fewer
  calls than its lines; fix it with the UPDATE below.
- **`channels` below `processing` on both reads:** over-dial risk. Fix it at once.

**Fix** (a write; one row, and only if `channels` still holds the value `<C>` you read twice, so a call that
started or ended meanwhile makes it change nothing):

```sql
UPDATE telephony_numbers n
SET channels = (SELECT count(*) FROM lead_call_tracker l
                 WHERE l.status = 'PROCESSING' AND l.telephony_number_id = n.id
                   AND ((l.call_direction = 'OUTBOUND' AND l.execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST'))
                        OR (l.call_direction = 'INBOUND' AND n.provider IN ('PLIVO', 'VOBIZ')))),
    updated_at = NOW()
WHERE n.id = '<NUMBER_ID>' AND n.channels = <C>
RETURNING n.id, n.channels;
```

`UPDATE 0` means the value moved: read again. No Redis write is needed: today's token reconciler rebuilds
`bb:channel:<NUMBER_ID>` from the same count on its next run. If `count_processing_by_telephony_number_query`
changes (for example to count more kinds of held calls), use its current WHERE clause here.

---

## 6. Go-live settings (measured 5 Oct)

| Setting | Value | Why |
|---|---|---|
| `BLOCKING_THREAD_POOL_SIZE` (dialler Deployment only) | **400** | `make_call` runs the blocking Plivo SDK on this pool (default 48); one thread per Plivo request in flight. 400 / 0.55 s ≈ 720 dials/s per pod, far above a pod's CPU (spec §10.5). API and voice-agent pods keep 48 |
| `terminationGracePeriodSeconds` (dialler) | **≥ 45–60 s** | the acceptor drains 25 s, next to a Plivo request of up to 15 s |
| `BB_V2_MAX_INFLIGHT_PER_POD` | 2000 (default) | a memory guard (~50-100 KB per dial in flight), not a merchant cap: a full pod stops popping and the other pods take the next tickets |
| `BB_V2_ACCEPT_BATCH` / `BB_V2_TICKET_BLPOP_TIMEOUT_S` | 100 / 1 s (defaults) | tickets taken per round trip (>= 1, as is the in-flight guard) / how long an idle acceptor blocks (must be > 0 and < the socket timeout: checked at load) |
| `BB_V2_ACCEPT_ERROR_BACKOFF_S` / `BB_V2_ACCEPT_DISABLED_SLEEP_S` / `BB_V2_ACCEPT_FULL_SLEEP_S` | 1 / 2 / 0.05 s (defaults) | the acceptor's pauses after an error in a round, after pushing a batch back while the kill switch is off, and while the pod is at its in-flight guard. Each ends early on shutdown; each must be > 0 (0 is a busy loop) and <= 60 s (checked at load). A pod that has never seen v2 looks again every 5 s (the latch's refresh) |
| `BB_V2_REDIS_SOCKET_TIMEOUT_S` / `BB_V2_REDIS_MAX_CONNECTIONS` | 10 s / 200 (defaults) | the dialler's own Redis client (no library retries): a reply not back in time is "not done" |
| `BB_V2_PREWARM_WAIT_S` / `BB_V2_TTS_CONCURRENCY` | 2 s / 32 (defaults; decisions D2, D10) | the longest a v2 dial waits for its greeting / greeting syntheses at once per pod (fail-open) |
| `BB_V2_DUE_BATCH` / `BB_V2_DUE_FULL_PASS_TICKS` / `BB_V2_DUE_RECHECK_S` | 5000 / 30 / 5 s (defaults; checked at load) | numbers a tick matches at most (earliest first, the rest next tick) / every 30th tick also matches every active number (the safety net for a missed `bb:due` write) / how soon a number match cannot act on yet is looked at again: one whose waiting leads all belong to a paused reseller (the most an unpause waits), and one still switching (`v2_pending` / `draining`; the flip itself runs match); ≥ 1 s |
| `BB_V2_ROUTES_REFRESH_S` | 600 s (default; ≥ 10, checked at load) | how often the routes of templates with waiting leads are re-resolved. Only a backstop: the template / config / number save hooks re-resolve a route the moment it changes. Why not 60 s: at thousands of templates that is tens of DB queries a second for nothing. Set 60 to get the old pace. Saves covered by a hook: template create / edit / delete / rollback, Assist onboarding's template update, call config, calling on/off, number edits. Not covered: a change to `BB_V2_TIER_HIGH_MERCHANT_IDS` / `BB_V2_TIER_MEDIUM_MERCHANT_IDS` (dynamic config, no save hook) reaches the routes only at the next refresh, so up to this interval; lower it for the day if a tier change must apply at once |
| `BB_V2_LEDGER_CHUNK` | 1000 (default; checked at load) | ids per DB query in the 30 s ledger check (lead states, live calls, inbound calls): the check costs one query per 1,000 holders, not ~4 per number |
| `BB_V2_PRUNE_CHUNK` | 1000 (default; checked at load) | leads per ZSCAN chunk (and per DB query) when the 5-min orphan prune reads a room: a 100k-lead room is never one multi-MB reply |
| `BB_V2_MONITOR_SCAN_CHUNK` / `BB_V2_MONITOR_SCAN_MAX` | 100 / 1000 (defaults; checked at load) | how the 15 s monitor reads `bb:tickets` for the oldest live ticket (void entries skipped); a void run longer than the max reports the head's age |
| `BB_V2_DIAL_MEMO_TTL_S` | 10 s (default; 0-60, checked at load; 0 = off) | how long a dialler pod keeps a template, its call config and its number for v2 dials: a template / config / number edit reaches its dials within this (3 fewer DB reads per dial) |
| `BB_V2_THROTTLE_WAIT_MIN_S`, `BB_V2_THROTTLE_MAX_STEP_S`, `BB_V2_THROTTLE_MAX_WAIT_S` | 1 / 8 / 60 s (defaults) | after a Plivo 429 the same dial goes again after a jittered wait whose upper bound doubles per resend (1-2 s, then up to 4, then up to 8 s), or Plivo's Retry-After if longer, for at most 60 s in all; then today's not-placed path. Checked at load: 0 < MIN ≤ MAX_STEP < MAX_WAIT < the 180 s claimed tier. The doubling matters under a sustained 429 storm (node test b13: a fixed 1-2 s retry sent ~11 requests per placed dial and filled the 400-thread pool) |
| `BB_PLIVO_5XX_OUTCOME` | `unknown` (default; decision D1, open) | a Plivo 5xx on a dial is held like a lost reply (`body_decides` / `not_placed` are the alternatives D1 weighs) |
| Dialler pods | 6 to start; 4–5 if CPU stays near 0.5 core | 13 ms CPU per dial measured with a simulated Plivo; real HTTPS adds some |
| Redis | watch Memorystore main-thread CPU | v2 used 35–43% of one core at 187–270 dials/s in the harness; prod peaks at 16% today |

**Alerts to have before the first number:**

| Alert | Meaning |
|---|---|
| `[P1] Breeze Buddy v2: tickets waiting for an acceptor` (a ticket waiting > 10 s) | dialler pods down, or every pod at `BB_V2_MAX_INFLIGHT_PER_POD`: scale dialler pods (there is no task count to raise) |
| `[P1] Breeze Buddy v2: free line next to a due lead` | nothing has matched the number since its `bb:due` time (no leader, or its match keeps failing: `v2 script failed`) |
| `[P1] Breeze Buddy v2: live calls missing from the busy list` | a live call holds no line in v2's count: over-dial risk |
| `[P0] Breeze Buddy v2: no sweep leader` | nothing switches, matches on time, reaps or heals |
| `[P1] Breeze Buddy v2: a dialable number was missing from bb:due` | the sweep's full pass found numbers dialable that `bb:due` did not list (log: `did not list as due`): a code path forgot its `bb:due` write. The full pass recovers them every `BB_V2_DUE_FULL_PASS_TICKS` s; report it as a bug |
| Cloud SQL latency | a slow DB slows every dial; the ticket-wait alert does **not** fire for it |
| Memorystore main-thread CPU > 60% | voice agents share this Redis |
| Plivo 429s (`Plivo dial for lead … throttled (429)`) | our dial rate reached the account's request limit; live-call transfers on the same account may fail meanwhile (decision D11). The SDK's 5xx re-send is gone (`NoResendClient`), so its `Fallback for URL` stdout line no longer appears |

### Before switching on at scale

The one-time checks above are enough for the first numbers. Before most of the traffic moves to v2:

1. **Read the Plivo request limit first.** v2 fills free lines as fast as the pods can send, and there is no
   pacer yet (deferred). The steady dial rate is lines ÷ call length (5,000 lines at 36 s ≈ 139 dials/s), and a
   fill of many lines at once comes on top. Get the account's API request limit from Plivo and compare. A 429
   places nothing and is sent again (rule 51), but each one costs a request and a thread, and live-call
   transfers on the same account share the limit (decision D11).
2. **Redis main-thread headroom.** In the harness v2 used 35–43% of one core at 187–270 dials/s; prod peaks at
   16% today, and the voice agents share this Redis. Move numbers in steps and watch Memorystore main-thread
   CPU; stop adding numbers at ~60%.
3. **Cloud SQL and the 180 s claimed tier.** A dial costs ~30 DB transactions, and the DB's CPU is the first
   wall at peak. A ticket claimed but not dialling 180 s after its claim is reaped: the line is freed, the lead
   unlocked and issued again. The slow coroutine then leaves the lead to its new holder (it never unlocks, defers
   or re-queues it), so nothing dials twice, but its work is lost. A steady `v2 lease reaper reaped <n> leases`
   together with high Cloud SQL latency means the DB is the bottleneck: stop adding numbers; don't raise the
   tier.
4. **TTS slots at peak.** A pod synthesises at most `BB_V2_TTS_CONCURRENCY` (32) greetings at once, and a dial
   waits at most `BB_V2_PREWARM_WAIT_S` (2 s) for its greeting, then dials without it; the call then
   synthesises when answered, so the first word is slower. Many `Greeting prewarm skipped … TTS slots busy`
   lines at a window's start mean the slots are the limit; raise the setting only within the TTS provider's
   own rate limit.
5. **No deploys while v2 is on (rule 35).** At scale v2 is on all day, so every deploy of API, dialler or
   voice-agent pods is: switch v2 off (§3), deploy, switch it on again. Plan deploys outside calling windows.

---

## 7. Log lines

| Log line | Meaning | Action |
|---|---|---|
| `Started the v2 acceptor` | a dialler pod started its acceptor | none |
| `v2 acceptor: <n> dials in flight (BB_V2_MAX_INFLIGHT_PER_POD)` | this pod is at its memory guard; other pods take the next tickets | scale pods if it lasts |
| `Plivo dial for lead <L> throttled (429) on account <A>` | Plivo refused a request (nothing placed); the dial goes again after 1-2 s | many: our request rate is at the account's limit (decision D11) |
| `v2 dial for lead <L>: Plivo still refusing after <n> requests …: not placed` | 60 s of 429s (or a stopping pod): the lead is deferred as today | as above |
| `Plivo call outcome unknown for lead <L> (…)` | a 5xx, a 2xx without a call id, no complete reply, or a reply that failed to parse: the lead is held, never redialled | a burst: Plivo's API is unwell; the stuck sweep settles them |
| `v2 switch: <N> -> v2_pending` / `-> v2` / `-> draining` / `-> legacy (channels=…, tokens=…)` | a switch step done | none |
| `v2 switch: <action> for <N> skipped, its mode changed since it was read (…)` | a stale switch step was refused (rule 46); normal right after a leader change | investigate if it repeats (leader flapping) |
| `v2 switch: handover for <N> skipped, mode changed` | as above, for a hand-back | as above |
| `v2 switch <action> failed for <N>: …` | a step failed; the next check retries | investigate if it repeats |
| `v2 switch: locked leads unreadable: …` | the DB read for the switch-on check failed; the count starts again | DB health |
| `v2 sweep: <job> job cancelled, no longer the leader` | this pod lost the leader lock; the new leader re-runs the job | none, unless frequent |
| `v2 <job> job failed: …` / `v2 sweep tick failed: …` | a job errored or timed out; the next run retries | investigate if it repeats |
| `v2 ledger check freed <n> stale lines` | holders of finished or abandoned leads freed | a few is normal |
| `v2 ledger: live calls missing from bb:busy:<N>: …` | with the P1 alert | over-dial risk: check the number now |
| `v2 lease reaper reaped <n> leases, re-delivered <m> tickets` | coroutines that died (or ran > 180 s after their claim); tickets popped and never claimed (a pod died, a pop reply was lost) | occasional is fine; steady = pods dying or a stuck step |
| `v2 backlog reconciler queued <n> leads` | due leads that were missing from their rooms | a steady stream means leads are being lost somewhere |
| `v2 orphan prune moved or dropped <n> leads` | rooms without a route, or finished leads in rooms | none |
| `v2 monitor: a ticket on <N> waiting <ms> ms` | with the P1 alert | dialler pods / their in-flight guard |
| `v2 monitor: <N> not matched since its bb:due time` | with the P1 alert | check `GET bb:v2:sweep:leader` and `v2 script failed` lines |
| `v2 sweep: the full pass issued tickets on [<N>, …], which bb:due did not list as due …` | the safety net found a dialable number the index missed (or a write was in flight); its leads waited up to 30 s | rare is fine; repeated = a missed `bb:due` write: report it |
| `v2 monitor: no sweep leader` | with the P0 alert | get the dialler pods healthy |
| `v2 mode unreadable for <N>: released <holder> on v2 and today's accounting` | a Redis read failed at call end; both sides released (rule 45) | Redis health |
| `v2 script failed: …` | a Lua script got a Redis error; the caller treats it as not done | Redis health |
| `v2: epoch set after a Redis flush (or first use)` | v2's state was lost and rebuilt (or v2 started) | check §4 once |
| `v2: Redis lost the v2 flags; today's counters rebuilt, epoch set` | Redis lost v2's keys and the flags; today's counters recounted after 30 s | run §5 once per number |
