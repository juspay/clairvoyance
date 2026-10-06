# v2 event dialler: review notes

What the review ids in the code mean. Code comments cite them as "Fable C1", "x7b", "D14", "ruling
C-concern 2", and so on. Two design reviews used the same id scheme, so each id below names its source:

- **plan review**: the independent review of the plan and rulings (4 Oct 2026; Critical C1–C3, Important I1–I8,
  Minor M1–M9);
- **final review**: the whole-branch review of the implementation (4 Oct 2026; Important I1–I4, Minor M1–M12).

Rules ("rule N", "§N") are in [design-card.md](design-card.md).

## Cited in the code

| Id (where cited) | Source | What it was | What the code does |
|---|---|---|---|
| Fable C1 (`switch.py`) | plan review | "Mode" lived in three places (flags, routes, per-process caches) that changed at different times, so each transition had a window where today's path and v2 dialled the same free lines | one source of truth, `bb:num:{N}.mode`, written only by the switch (rule 21) |
| Fable C3 (`switch.py`) | plan review | the hand-back wrote DB `channels = SCARD(bb:busy:N)`, which counts tickets too and can be permanently wrong | the hand-back writes the DB's own PROCESSING count (rule 22) |
| Fable I2 (`scripts.py` match and reap) | plan review | match didn't check `bb:route:{T}.number == N`, and `bb:numtpl:{N}` was never pruned | match drops a template no longer routed to N and flags the number it moved to; the reaper re-queues onto the current route |
| Fable I2 (`switch.py`, `reconcile.py`) | final review | switch-on "observe" and "seed" hit the DB and SCANned the keyspace per pending number every 5 s | locked leads are read once per switch step and shared by every number; no SCAN |
| Fable I3 (`sweep.py` header) | plan review | the 1 s tick also ran blocking work: keyspace SCANs every second, inline switch waits and reconcilers | the tick does no SCAN; jobs are spawned as background tasks with timeouts (design card §5) |
| Fable I3 (`sweep.py` channels mirror) | final review | a Redis flush or a code rollback turns v2 off without a hand-back, leaving today's DB gate on a counter nothing corrects | the DB `channels` of a `v2` number is rewritten every 30 s from the DB's PROCESSING count (rule 29) |
| Fable I4 (`switch.py`) | plan review | card rule 3 (disabled number → today's path → `NUMBER_UNAVAILABLE`) wasn't implemented: leads waited forever | a non-AVAILABLE number leaves the desired set and drains to today's path (rule 27) |
| Fable I4 (`reconcile.py`) | final review | the switch-on stability signature included leads that can't over-dial, so the seed could be starved | the signature is the number's locked leads only (rule 31) |
| Fable I7 (`reconcile.py`) | plan review | the reaper's 120 s overlapped a legitimate dispatch (greeting pre-warm alone can take ~61 s) | lease age limit 180 s (rule 25) |
| Fable I8 (`switch.py`) | plan review | the hand-back gave up after 30 s and UNLINKed, stranding tickets still waiting to be dialled | no give-up: the hand-back waits until no lease is left (every ticket has one) |
| Fable M1 (`scripts.py` match) | final review | the sweep's list of numbers with waiting leads never shrank for a number not in mode `v2` | match removes a legacy (or mode-less) number from the list (now `bb:due`, rule 55) |
| Fable M2 (`reconcile.py` backlog) | plan review | the backlog reconciler's Python pre-checks repeated what `enqueue` checks | one `enqueue` per row (since 6 Oct a page's go in one pipeline), with `only_if_absent`, so a queued lead costs one `ZSCORE` (rule 43) |
| Fable M2 (`scripts.py`, `sweep.py` mirrors) | final review | a mirror job SCANned the keyspace every 5 s for hand-set reseller pause keys | match reads today's pause key directly; the mirror was removed (rule 28) |
| Fable M3 (`release.py`) | final review | inbound on a `v2_pending` number could be refused while lines were free | in `v2_pending`, releases and inbound use today's path until the busy list is seeded (rule 31) |
| Fable M4 (`reconcile.py` prune) | plan review | leads of a deleted template would wait forever in v2 | the orphan prune moves rooms without a route back to today's schedule, where they dial "without a template" as today |
| Fable M5 (`sweep.py`, `reconcile.py`) | final review | one Redis round trip per active number in the mirror, ledger and stranded checks | the stranded check and the ledger's mode reads are one round trip; the channels mirror still reads each number's mode right before writing it, on purpose (a hand-back may have started) |
| Fable M6 (`reconcile.py` ledger) | plan review | a BACKLOG holder freed by the ledger waited for the backlog reconciler (up to 90 s) | the ledger re-queues it at once |
| Fable M7 (`routes.py`) | final review | dead code and six copies of the Redis-client helper | one shared `_client()` in `routes.py` |
| Fable M10 (`keys.py`) | final review | route and number hashes were never removed | routes expire 2 days after their last write; numbers never (rules 30, 33) |
| x7b (`queue.py`, `switch.py`) | harness scenario | Redis flushed while the v2 flags lived only in Redis: v2 read as off with no hand-back, today's path ran on a stale DB `channels` and over-dialled (91 events) | while `bb:epoch` is missing after a process saw it, today's workers defer; with the flags gone the leader recounts after 30 s (rule 32) |
| D14 (`scripts.py`) | decision | match touches a number's keys and its templates' rooms in one script, so single-node Redis is assumed | verified 5 Oct 2026: Memorystore STANDARD_HA, Redis 7.2, one primary |
| D16 (`scripts.py`) | decision | `RedisService.run_script` swallows errors and returns None | every v2 caller treats None as "not done"; the sweep and reconcilers heal |
| ruling C-concern 2 (`release.py`) | ruling | inbound in `v2_pending` | uses today's DB gate until the seed (rule 31) |
| ruling C-concern 4 (`switch.py`) | ruling | a config read error | no switch transition in that check (the flag is read strictly) |
| race C (`scripts.py` reap) | ticket-id races | the reaper read a lease without `dialling_ms`, then the dialler marked it | `reap_lease` re-checks the lease inside the script |
| PoC bug b051299f (`scripts.py` enqueue) | PoC | after a flush a process kept queuing into rooms whose route was gone | `enqueue` refuses (`-1`) and the caller re-resolves (rule 18) |
| PoC fix 4e5862a7 (`scripts.py` release_stale) | PoC | the ledger's "no lease" check and its removal were two steps | one script (rule 19) |
| PoC issue 6 (`reconcile.py` backlog) | PoC | restarting from the oldest rows every run hid lost leads behind a big pile | the page position is kept between runs |

## Review of PR #1287 (manas-narra, 5 Oct 2026)

| # | Finding | Status |
|---|---|---|
| 1 | A shared number serves its templates strictly by earliest due time, so one merchant's pile delays the others on it | **kept as is, owner decision pending** (FIFO by due vs round-robin per template); documented in design card §7 |
| 2 | One fixed pool of dialling tasks across numbers: a slow provider (or slow pre-check / greeting) pins it | **fixed by the act stage** (6 Oct): no pool. One acceptor per pod runs one coroutine per ticket, so a slow merchant holds only its own lines (design card §4, rules 49-54). Measured 5 Oct on the old pool: with Flipkart's greeting hanging, other merchants' p95 lag went 2 s → ~15 s |
| 3 | A switch step of a deposed leader can undo a finished hand-back | **fixed** in `794dacdd`: `switch_cas` + the sweeper cancels its jobs when it stops leading (rule 46) |
| 4 | The design card and a runbook are not in the repo | **fixed**: this folder |
| 5 | Once a process has seen v2, a slow Redis read refuses inbound on legacy numbers too | **accepted known risk** (only while a mode read fails; bounded at 1 s, rule 42) |
| 6 | match blocks Redis for O(open rooms × cap); room moves in one script | **fixed** in `1eccba9c`: heads read once per run, rooms moved 1,000 per script (rules 47, 48) |
| 7 | CI runs none of the dispatch tests | **fixed** in `e9f3bcdf`: a CI job with a Redis service runs `tests/breeze_buddy/dispatch` |
| 8 | The parity proof skips silently when its base commit is unreachable | **fixed** in `e9f3bcdf`: it fails instead; `BASE_SHA` must name the release commit after the rebase |
| 9 | Two commits until #1280 merges | rebase after #1280 merges |

## Design review of the act stage (Fable, 5 Oct 2026) — closed by the act-stage branch

| Finding | Closed by |
|---|---|
| One fixed pool of dialling tasks shared by every number: a burst or one slow merchant makes every other wait; 20 per pod topped out at ~210 dials/s | one acceptor per pod, one coroutine per ticket, no pool (design card §4; rule 49) |
| The Plivo SDK re-POSTs a dial on any 5xx (up to 3 calls to one customer) | `NoResendClient`: every request sent once; a 5xx or an unreadable 2xx is held (rule 54) |
| redis-py retries a command 3 times with 1-10 s sleeps inside the dial path | the dialler's own Redis client, no library retries (rule 50) |
| `match` issues at most `BB_V2_MATCH_CAP` per run and nobody loops: 5,000 free lines fill at 100 a second | `match_all` after a capped enqueue and in the sweep tick (design card §2) |
| A slow step (greeting TTS) pins the dialling capacity | the greeting pre-warm runs beside the dial, waited for at most 2 s (rule 53) |
| A 429 sends the lead back with a 10 s defer and churns under a burst | the same request again after a jittered wait that doubles to 8 s, for at most 60 s (rule 51) |
| The static greeting check `GET`s the ~100 KB audio per dial | Phase 2 (`EXISTS`) |

## Reviews during the act-stage build (6 Oct 2026)

Commit subjects name these ids ("review P2-2", "review F-4"); the code cites rules, not them. P1 = review of
Phase 1, P2 = review of Phase 2, F = Fable's review of the whole branch, F-Q = its code-quality items.

| Id | Finding | Fix (rule) |
|---|---|---|
| P1-1 | the reaper read the `bb:tickets` head per lease, so a lost ticket behind a moving head never came back | head read once per run, passed to the script (52) |
| P1-2 | the acceptor's HTTP session capped connections at 100, so dials queued behind each other | no connection limit |
| P1-3 | the kill switch was read before the dial's commit point only | `mark_dialling` checks it (24) |
| P1-4 | a dispatch whose ticket was re-issued still unlocked and re-queued the lead | `Mark.SUPERSEDED`: the lead is left to the new holder |
| P1-6 | an error that is neither the SDK's nor requests' after Plivo replied counted as not placed | held as unknown (54) |
| P1-7 | a dial the SDK refused before sending could read the previous request's 429 | the reply is read only for a request that left |
| P1-8 | a claimed lease taken by the previous release has no `claimed_ms` and was never reaped | it ages from `issued_ms` (14) |
| P1-9 | the waiting-ticket monitor read 20 head entries, so 20+ void entries hid a stuck ticket | it reads past a void run in chunks |
| P1-10 | an acceptor BLPOP longer than the socket timeout loses the popped ticket | load check `0 < BLPOP timeout < socket timeout` |
| P2-1 | the dial memo kept a re-pinned template's old number, so every ticket mismatched | the mismatch drops the template's memo entries (56) |
| P2-2 | the ledger freed holders from a batch read seconds old | candidates' leases, then states, are read again before each free (11) |
| P2-3 | a missed `bb:due` write was only a log line | a throttled P1 (55) |
| P2-4 | a due number past the tick's batch cap was reported as a missed write | its score is read before matching |
| P2-5 | a switching number's overdue `bb:due` entry stayed at the front of every tick | moved to now + `BB_V2_DUE_RECHECK_S` |
| P2-6 | one failing DB chunk stopped the ledger run | each chunk is read on its own |
| P2-7 | a template rollback and Assist onboarding's update did not re-resolve the route | both call the v2 save hook |
| P2-8, P2-9 | docs | the card lists what the memo can serve stale (56); `hooks.py` docstring |
| F-1 | Vobiz writes its dial row after the reply, so the row cannot pick one of a lead's two tickets | v2 dials Plivo only (27) |
| F-2 | a dispatch whose lease was reaped still unlocked, deferred, finished or re-queued the lead | `return_line`'s `GiveBack.NOT_OURS` makes it leave the lead alone |
| F-3 | the call-end release ran unbounded twice on today's paths | both attempts bounded at 1 s (42) |
| F-4 | a lost ticket issued in the same match run as the list's head was never re-pushed | same-number tie broken by ticket id (52) |
| F-5 | a reply without a call id reverted Plivo's pre-written dial row | the row stays as the hold |
| F-6 | the acceptor's HTTP session stayed open after a dial outlived the shutdown grace | the last such dial closes it |
| F-7 | a room full of copies of leads already holding a line was walked whole in one script | at most cap + open rooms steps per run (47) |
| F-8 | a fixed 1-2 s 429 wait sent ~11 requests per placed dial in a storm | the wait doubles to 8 s (51) |
| F-9 | the backlog reconciler sent one script per row | one pipeline per page (43) |
| F-10 | is a lock refused on a lead no longer BACKLOG re-queued every 30 s? | no: only a lead still BACKLOG is (25); test added |
| F-Q1–Q3, F-Q5 | code quality | one memo-read helper; named refusal codes (`Enqueue`, `GiveBack`, `Reap`); review ids out of code comments; one script runner |

Two older sources were cited in code on this branch and are now here: the lag test of 5 Oct (L1: with the fixed
pool, one merchant's hung TTS parked 330 of a pod's 360 tasks, which is why the acceptor has no pool) and the Plivo
dial audit (§1: what the SDK sends and when it re-sends; §3: the dial outcomes, e.g. a 2xx without a call id is
held like a lost reply).
