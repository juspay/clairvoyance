# PgBouncer / Transaction-Pooling Compatibility

Clairvoyance's asyncpg pool is configured to be safe behind a
**transaction-pooling** proxy — PgBouncer (`pool_mode = transaction`) or Cloud
SQL Managed Connection Pooling. This is task **P0.1**: at 100+ pods the fleet's
eager per-pod pools exceed Cloud SQL's `max_connections` (default 400 on the
current 8 GB instance) before a single call is placed; a pooler multiplexes all
of that onto a handful of server connections.

## What the code guarantees

Transaction pooling hands a server connection to a client **only for the
duration of one transaction**. Anything that assumes session state survives
between transactions breaks. The DB layer is clean today, and
`tests/database/test_pgbouncer_compat.py` pins the contract:

| Rule | Why |
|---|---|
| `statement_cache_size=0` on the pool (set `POSTGRES_BEHIND_POOLER=true`; the cache default follows it) | asyncpg's implicit prepared-statement cache prepares on one server connection and executes on another → `InvalidSQLStatementNameError` / `DuplicatePreparedStatementError`. Reproduced in the tests; do not raise the value while a transaction-pooling proxy without prepared-statement tracking is in the path. |
| No `LISTEN/NOTIFY`, session `SET`, advisory locks held across transactions, temp tables, `WITH HOLD` cursors, `currval` | Session state does not survive transaction boundaries. Use Redis pub/sub (already the pattern), `SET LOCAL` inside a transaction, and row locks (`FOR UPDATE [SKIP LOCKED]`) instead. |
| Keep transactions short; never `await` external I/O (LLM/TTS/HTTP) inside `conn.transaction()` | A transaction pins a server connection for its whole duration. |
| Any new `asyncpg.create_pool()`/`asyncpg.connect()` outside `init_db_pool` must also pass `statement_cache_size=0` | `scripts/migrate.py` had exactly this bug: its own pool, no cache override — it worked single-client (PgBouncer happened to reuse the same server connection) and failed instantly (`DuplicatePreparedStatementError`) once any concurrent client shared the bouncer. That "works in dev, breaks in prod" shape is why the rule exists. Pinned by `test_migration_runner_disables_statement_cache`. |

## Required PgBouncer version: >= 1.21 (>= 1.25.2 recommended)

Not a preference — two things we depend on do not exist below 1.21:

- **Cancel-request peering** (`peer_id` + `[peers]`). 1.21 also changed the
  cancellation-token encoding, so peers must all be >= 1.21.
- **`max_prepared_statements`**, the escape hatch below.

1.25.2 (May 2026) additionally carries fixes for four CVEs including an
unauthenticated remote crash. Note distro packages lag badly — Ubuntu 22.04
ships 1.16 — so pin the image tag explicitly. The local rig refuses to start
below 1.21 so it can't silently test something production won't be.

## Multi-replica deployments: cancels need peering

**A cancel request travels on a NEW TCP connection, not the one running the
query.** With several PgBouncer replicas behind a Service (the planned
topology is 3), the load balancer routes that new connection to an arbitrary
replica, which knows nothing about the query — the cancel is dropped and the
statement keeps running server-side until it finishes on its own.

This matters here specifically: voice teardown cancels in-flight work
constantly, which is what `test_query_cancellation_through_bouncer` covers.
That test runs against a **single** rig instance, so it proves the client and
protocol path — it cannot prove the multi-replica path. Production must give
each replica a unique `peer_id` and a shared `[peers]` section listing all of
them (see the pgbouncer.ini in the evaluation doc).

## Never open a second connection inside a transaction (the deadlock)

**This is the one hazard that pooling INTRODUCES rather than exposes**, and it
is the only failure mode here that hangs instead of erroring.

An atom that holds a transaction on connection A and then awaits work that
takes connection B needs **two server slots at the same time**. Behind a
transaction pooler, A pins its server connection until COMMIT, so with N
concurrent atoms and a `default_pool_size` below `2 x N`, every slot is held by
a transaction waiting for a slot. Nothing errors; it waits.

Measured on the rig (`default_pool_size = 2`, no wait timeout):

| Concurrent atoms | Result |
|---|---|
| 1 | completes in 0.1s |
| 2, 5, 20 | **hang forever** — no error, no timeout |

CI rule 13 (`scripts/check_crm_boundaries.py`) enforces this: from every
`*_in_txn` body it follows local calls transitively and fails any that reaches
an acquirer (`crm_connection`/`connection`/`atomically`) or another module's
`contracts`. The reach must be transitive because the real sites put the call
one or two hops down — `record/workers.py` reaches `resolve()` through
`_process_one`, so a lexical scan of the atom body finds nothing.

The sites that already do this are allowlisted in `NESTED_CONNECTION_LEGACY`
in that script; **that set is the single list** (this doc deliberately does not
repeat it) and it is CLOSED — a new entry is a new deadlock risk.

**The rule has one blind spot, by construction.** Nesting that goes through a
runtime registry slot — `connectivity/templates/lifecycle.py` reaching outreach
via `retire_guard`, which `worker_main` fills at startup — is late-bound and
invisible to any static analysis. Sizing and `query_wait_timeout` below are what
keep *those* safe; the rule cannot.

`app/crm/worker_main.py` already guards the *app* pool ceiling — but it cannot
see PgBouncer's `default_pool_size`, which is the binding constraint after
cutover.

**Three things keep this safe, and all three are required:**

1. **Size both pools for `2 x` the concurrent atoms.** The app pool
   (`POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW`) *and* the bouncer's
   `default_pool_size`. Verified: at `default_pool_size = 50` the same
   workload completes in 0.1s.
2. **`query_wait_timeout` on the bouncer is mandatory, not tuning.** With
   `query_wait_timeout = 5` the same deadlock became a visible error in 5.3s
   instead of hanging. The rig sets it so local runs match production.
3. **`POSTGRES_ACQUIRE_TIMEOUT_SECONDS`** (default 30) bounds the app-side
   half of the same wait. Note it does NOT cover the bouncer-side wait — the
   client connection is established and the *query* is what waits — which is
   why (2) is not optional.

Best of all, don't nest: pass the atom's own `txn` down instead of taking a
second connection. Pinned by `test_nested_connection_inside_transaction_fails_fast`.

## Local rig

Run the real thing locally (transaction mode with
`max_prepared_statements = 0` — the strictest configuration, matching Cloud
SQL MCP's default behavior):

```bash
brew install postgresql@14 pgbouncer   # once
scripts/pgbouncer_local_rig.sh start   # Postgres :55432 + PgBouncer :56432
PGBOUNCER_RIG=1 uv run pytest tests/database/test_pgbouncer_compat.py -v
# direct-to-Postgres (rollback path). Use PGBOUNCER_RIG_PORT, not
# BOUNCER_PORT — the latter also moves the bouncer itself.
PGBOUNCER_RIG_PORT=55432 PGBOUNCER_RIG=1 uv run pytest tests/database/test_pgbouncer_compat.py -v
scripts/pgbouncer_local_rig.sh stop
```

Seven tests run in every normal `pytest` invocation with no rig required
(the other seven are rig-gated and skip). They are pure file/AST scans plus
the two pool-config tests; none opens a socket, so CI needs no database.
`test_every_connection_site_disables_statement_cache` scans `app/`, `scripts/`
**and `tests/`**, failing any `asyncpg.create_pool()`/`connect()` that omits
`statement_cache_size`. A file the scanner cannot parse fails the guard rather
than being skipped — a skipped file would be reported clean.

## Evidence (2026-08-24, local rig)

- Without the fix: concurrent workers fail with
  `InvalidSQLStatementNameError` (within one process asyncpg's statement-name
  counter is a module global, so names never collide); the
  `DuplicatePreparedStatementError` variant appears ACROSS processes — the
  `migrate.py` scar, and in production the per-call `bot_runner` subprocesses,
  each of which restarts that counter at zero. Grep for both.
- With the fix: whole suite passes; 30 consecutive runs, 0 flakes.
- Soak: 400 concurrent workers × 10 mixed operations (parameterized selects,
  multi-statement write transactions, `SET LOCAL` + pg_trgm queries) =
  4,000/4,000 ops, 0 errors — while **Postgres never saw more than 2 server
  connections** (PgBouncer `default_pool_size = 2`). That is the multiplexing
  the production rollout depends on.
- The same suite passes pointed directly at Postgres, so shipping
  `statement_cache_size=0` ahead of the pooler is safe (P0.1's required
  ordering: the app change lands before or with the pooler, never after).

## What `statement_cache_size=0` costs, and the escape hatch

Disabling the cache means every query is parsed and planned on each
execution. There is **no extra round trip** (asyncpg still sends
Parse/Bind/Execute in one flight), so client-observed latency is
approximately unchanged; the cost is **CPU on the database**, and `breeze-db`
is a 2 vCPU / 8 GB instance. Negligible at today's volume, but worth watching
after the fleet scales out.

Two driver-level details the "no extra round trip" claim does not cover:
asyncpg runs `set_config('jit', 'off', false)` around type introspection
(session-scope, on whatever backend it lands on), and sends a reset query on
every connection release. Neither breaks correctness here; both add
round-trips the arithmetic above ignores.

If Cloud SQL CPU becomes the constraint, the two settings must be raised
**as a pair, bouncer first**:

1. PgBouncer `max_prepared_statements = 200` (>= 1.21), or Cloud SQL MCP's
   equivalent, so the pooler tracks and re-prepares statements per server
   connection.
2. Only then `POSTGRES_STATEMENT_CACHE_SIZE` above 0.

Raising the app setting alone reintroduces the exact production breakage this
document exists to prevent.

## Migrations

`scripts/migrate.py` is transaction-pooling-safe: each migration runs inside
one `conn.transaction()` (pinning one server connection for its duration),
multi-statement SQL files go over the simple protocol, and the runner now
disables the statement cache like the app pool. Pre-existing caveat,
unchanged by pooling: a migration containing `CREATE INDEX CONCURRENTLY`
cannot run inside a transaction anywhere — direct or pooled.

Note: the nautilus service tracks its own `_migrations` table **on the same
database** (see this runner's docstring) — so when PgBouncer fronts this
database, nautilus needs its equivalent fix (`prepare: false` in postgres.js)
before it is pointed at the bouncer.

## The bouncer side: `max_prepared_statements` is pinned, not inherited

PgBouncer >= 1.21 can re-prepare a client's named statements on whichever
server connection it hands out, which is what lets a pooled client keep its
driver-side cache. **1.25 defaults this to `200`** -- it was opt-in only in
1.21, so most writing on this subject is out of date.

Our ConfigMap therefore **pins `max_prepared_statements = 200` explicitly**
rather than inheriting it. An inherited default is invisible in review and
becomes a silent breaking change when an image or version changes it.

Measured on the rig, 60 clients x 8 queries:

| bouncer `max_prepared_statements` | app `statement_cache_size` | result |
|---|---|---|
| 0 | 100 | **54/60 fail** — `InvalidSQLStatementNameError` |
| 0 | 0 | 0/60 fail |
| 200 | 100 | 0/60 fail |
| 200 | 0 | 0/60 fail |

**The coupling, in one line:** a pooled deployment may run
`POSTGRES_STATEMENT_CACHE_SIZE > 0` only while the bouncer runs
`max_prepared_statements > 0`. Lowering the bouncer setting requires setting
every pooled deployment to `0` **first** -- otherwise the whole fleet starts
failing at once, under concurrency, with no code change to blame. The app
cannot check this (it only sees a socket), so `init_db_pool` logs the coupling
as a warning and the ConfigMap comment states it at the other end.

**The local rig runs `max_prepared_statements = 0` deliberately** -- stricter
than production, so code that passes it is correct under any pooler config,
including Cloud SQL Managed Connection Pooling where the tracking does not
exist at all.

## Per-deployment configuration (partial cutover)

The pooler does not have to sit in front of everything, and the setting is per
deployment because it is a plain env var. Point it at whichever deployment
actually holds the connections.

| deployment | destination | set | resulting cache |
|---|---|---|---|
| behind PgBouncer | the bouncer Service | **`POSTGRES_BEHIND_POOLER=true`** | `0` |
| direct to Postgres | Postgres | nothing (default) | `100` — correct AND faster |

**One flag, set where the topology actually changes.** `POSTGRES_BEHIND_POOLER`
goes in the same manifest edit that repoints `POSTGRES_HOST`/`POSTGRES_PORT` at
the bouncer -- the deployment knows which side it is on, so it declares it
rather than the app guessing from port numbers or hostnames. Deployments that
stay direct change nothing and keep asyncpg's cache, which is both correct and
faster for them.

`POSTGRES_STATEMENT_CACHE_SIZE` still exists and always wins when set. That is
the escape hatch for a pooler running `max_prepared_statements > 0` -- the one
configuration where a pooled pool may keep its cache.

`init_db_pool` logs an **error** only on the declared contradiction
(`POSTGRES_BEHIND_POOLER=true` with a non-zero cache). A direct deployment
keeping its cache is correct and stays quiet -- an error on every boot there
would train people to ignore the one line that matters.

## Production rollout

The deployed `pgbouncer.ini` **must** set `query_wait_timeout` (see the
nested-connection section above — without it an undersized pool hangs instead
of erroring) and `default_pool_size` above `2 x` the concurrent atoms per
replica. Those two are load-bearing for this repo's code and are restated here
so a reader of clairvoyance alone has them.

The rest of the evaluation — topology (centralized 3-replica StatefulSet with
cancel-request peering, not sidecars), sizing against Cloud SQL
`max_connections=400`, auth, canary plan, and the Cloud SQL Managed Connection
Pooling (Enterprise Plus) alternative — lives outside this repository, in the
infra workspace (`cpaas-buddy/docs/pgbouncer-evaluation.md` and
`cpaas-buddy/deploy/pgbouncer/`). It is **not** in the buddy-cpaas-docs corpus
and not clonable from here.

Cutover is an env flip: point `POSTGRES_HOST`/`POSTGRES_PORT` at the PgBouncer
Service. After cutover, shrink the per-pod pool (`POSTGRES_POOL_SIZE` floor
near 0, small `POSTGRES_MAX_OVERFLOW`) — the bouncer owns warm capacity from
then on.

**Rollback has a pod-count ceiling — know it before you rely on it.**
Reconnecting directly to Postgres restores the connection arithmetic the
pooler was adopted to escape. Against `max_connections = 400`:

| Pool settings | Direct-connect ceiling |
|---|---|
| Today (`min 5` / `max 15`) | ~26 pods under load; ~80 pods at idle floor |
| After the post-cutover shrink (`min ~0` / `max 5`) | ~80 pods under load |

So the env flip is a genuine safety net only while the fleet is small — which
it is today, and which is why the canary and cutover should happen before
scale-out, not after. Past that ceiling the real rollback is *shrink the pool
env vars*, not *reconnect direct*; a direct reconnect at 100+ pods fails to
boot the fleet.

## Working on this codebase? Read this before your next DB PR

The contract is enforced, not just documented — CI (`pytest tests/database`)
fails if you break these, no rig required:

1. **Never create an asyncpg pool or connection without
   `statement_cache_size=0`.** Reuse `init_db_pool()` / `get_db_connection()`
   instead of opening your own; if you genuinely need a separate pool, pass
   the setting. Guarded by `test_every_connection_site_disables_statement_cache`.
2. **No session state.** No `LISTEN`/`NOTIFY` (use Redis pub/sub), no
   session-scope `SET` (use `SET LOCAL` inside a transaction), no advisory
   locks held across statements, no temp tables, no `WITH HOLD` cursors, no
   `currval`/`lastval`.
3. **A lock is only a lock inside a transaction.** `SELECT ... FOR UPDATE`
   sent through `run_parameterized_query()` acquires and releases the lock in
   the same instant — *no error, just no locking*. Either fold the read and
   write into one statement (see `apply_wallet_delta_query`, a CTE) or use
   `atomically()` / an explicit `conn.transaction()`. `FOR UPDATE SKIP LOCKED`
   inside a single `UPDATE ... RETURNING` is fine as-is.
4. **Never `await` external I/O inside a transaction** — no HTTP, LLM, TTS,
   Redis, or `asyncio.sleep`. A transaction pins a server connection for its
   whole duration; the fleet's throughput is those connections.
5. **Migrations**: no `CREATE INDEX CONCURRENTLY` (the runner wraps each file
   in a transaction — migration 050 documents this), and no top-level
   `BEGIN;`/`COMMIT;` inside the SQL file (it closes the runner's own
   transaction early, leaving the tracking-table insert outside it). Several
   pre-2026-08 migrations still contain that pattern; they are already
   applied, but it would bite a fresh-environment bootstrap.
6. **Changing pool sizing or `POSTGRES_STATEMENT_CACHE_SIZE`?** Read the
   escape-hatch section above first — the app setting moves only after the
   pooler setting.
7. **Never open a second pooled connection while a transaction is open** —
   thread the atom's `txn` through instead. This is the one hazard that
   deadlocks rather than errors; see the section above for the sizing law.
8. **No `conn.cursor()`, `conn.prepare()`, or `executemany()`.** asyncpg
   creates a *named* server-side prepared statement for these regardless of
   `statement_cache_size=0` (`asyncpg/cursor.py` passes `named=True`), so they
   break behind a pooler that does not track prepared statements. The repo has
   none today; keep it that way.

What CI actually checks for you (all static, no DB needed):

| Guard | Catches |
|---|---|
| `test_every_connection_site_disables_statement_cache` | any `asyncpg.create_pool()`/`connect()` without `statement_cache_size=0` — resolves aliases and `from` imports, rejects opaque `**kwargs`, and checks the VALUE |
| `test_no_session_state_sql_in_application_code` | session `SET`, `LISTEN`/`NOTIFY`/`pg_notify`, session advisory locks, temp tables, cursors/`WITH HOLD`, `currval`/`lastval`, `LOCK TABLE`, `DISCARD`/`DEALLOCATE`, `SET ROLE` in SQL strings |
| `test_no_named_prepared_statement_apis` | `conn.cursor()` / `.prepare()` / `.executemany()` |
| `test_migrations_run_inside_the_runner_transaction` | top-level `BEGIN;`/`COMMIT;` and `CONCURRENTLY` in a new migration (12 already-applied legacy files are grandfathered) |

They are deliberately conservative: `UPDATE ... SET`, `SET LOCAL` inside a
transaction, and `pg_advisory_xact_lock` all pass, and f-strings are
reassembled before scanning so a fragment beginning `SET col = $1` is not
mistaken for a session `SET`.

Rules 3 and 7 (lock scope, nested connections) still need human review — a
static check cannot see whether a caller opened a transaction.

Verify anything DB-shaped against the rig, not just the unit tests:
`scripts/pgbouncer_local_rig.sh start && PGBOUNCER_RIG=1 uv run pytest tests/database/`.
