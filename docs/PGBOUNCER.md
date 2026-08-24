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
| `statement_cache_size=0` on the pool (`POSTGRES_STATEMENT_CACHE_SIZE`, default 0) | asyncpg's implicit prepared-statement cache prepares on one server connection and executes on another → `InvalidSQLStatementNameError` / `DuplicatePreparedStatementError`. Reproduced in the tests; do not raise the value while a transaction-pooling proxy without prepared-statement tracking is in the path. |
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

Three tests run in every normal `pytest` invocation with no rig required:
both pool-config tests and `test_every_connection_site_disables_statement_cache`,
which scans `app/` and `scripts/` and fails on any future
`asyncpg.create_pool()`/`connect()` that omits `statement_cache_size`.

## Evidence (2026-08-24, local rig)

- Without the fix: 34 of 120 concurrent workers fail with
  `DuplicatePreparedStatementError`; other patterns fail with
  `InvalidSQLStatementNameError`; `migrate.py` fails on its first run once a
  second client shares the bouncer.
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

## Production rollout

The full evaluation — topology (centralized 3-replica Deployment, not
sidecars), sizing against Cloud SQL `max_connections=400`, pgbouncer.ini,
auth, canary plan, and the Cloud SQL MCP (Enterprise Plus) alternative —
lives in the workspace doc: `cpaas-buddy/docs/pgbouncer-evaluation.md`.

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

The contract is enforced, not just documented — `pytest` fails if you break
these, no rig required:

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

Verify anything DB-shaped against the rig, not just the unit tests:
`scripts/pgbouncer_local_rig.sh start && PGBOUNCER_RIG=1 uv run pytest tests/database/`.
