# Building modules — the rules every code change follows

This is the repo-local digest of the Buddy CPaaS module rules. The full
corpus (canon tables, ADRs 0001–0021, per-module build guides, diagrams)
lives at **https://swaroopvarma1.github.io/buddy-cpaas-docs/** until it
migrates into this repo. When this file and the corpus disagree, the
corpus wins. Migration conventions: [migrations.md](./migrations.md).

## The module skeleton (sealed — every `app/crm/<module>/` looks like this)

```text
app/crm/<module>/
  __init__.py    # empty — exports NOTHING
  contracts.py   # THE public surface — re-exports from the logic files;
                 #   the only file other modules may import
  api.py         # thin routes -> logic (or db/accessor for trivial reads)
  schemas.py     # leaf Pydantic shapes — the module's public vocabulary;
                 #   imports nothing internal (api/contracts/tests use it)
  <concern>.py   # BUSINESS LOGIC by name (resolve.py, facts.py, ingest.py,
                 #   suppression.py): gather -> decide (PURE, returns a plan)
                 #   -> apply, inside a boundary this file owns
  workers.py     # drain loops (only if the module owns one)
  db/            # ALL mechanics behind one hop — root stays the story
    __init__.py  # the db door: re-exports transaction, savepoint, DbTxn,
                 #   domain errors
    accessor.py  # execute one query builder per function; no decisions
    queries.py   # SQL builders — (sql, params), $1 placeholders
    decoder.py   # row -> schemas model, DB-side translation only
```

**At scale the three files become three folders, one file per table** — the
form connectivity carries today, and the one a module takes on its next `db/`
touch once one file would hold four tables or cross the ~500-line line:

```text
  db/
    __init__.py                  the door, unchanged
    queries/    installation.py · binding.py · template.py · message.py
    accessors/  installation.py · binding.py · template.py · message.py
    decoders/   installation.py · binding.py · template.py · message.py
```

Shared column lists move with their table. Import the table you mean by its
full path (`from ...db.accessors import binding as binding_accessor`) — the
sub-packages export nothing, so an accessor's imports say which table it
touches without opening a second file. CI rule 2 admits both shapes; a file
under `db/accessors/` or `db/decoders/` still may not carry SQL.

**The layer law:** `api -> logic -> db/accessor -> db/queries`.
**The boundary law:** logic owns transaction scope (atomicity is business
semantics) and imports db-world things ONLY from its module's `db/` door:
`transaction()`, `savepoint()`, the opaque `DbTxn` handle, domain-named
errors. Logic may open a boundary and pass the handle; it may never speak
the driver's API on it — not a query (that is the accessor's job), and not
`txn.transaction()` for nesting. Nesting has its own door: a batch atom
isolates one unit with `async with savepoint(txn):`, so a single bad row
rolls back alone while the rest of the batch commits. CI rule 5 rejects
every driver method on a `txn`/`conn` in a logic file, nesting included.
`import asyncpg` is legal only in `shared/db.py` and `db/` packages —
grep-enforced. Single statements and same-builder
batch loops self-scope INSIDE accessors (`crm_connection`, db-internal —
no explicit transaction; Postgres runs one statement atomically); a logic
file touches a handle in exactly ONE place: the `txn` param of an
`_in_txn` body (threading through the atom's private sub-steps is part
of the body; it never escapes the atom). Multi-
statement fate-sharing uses **the atomic grammar** (CI rules 7-9): logic
enters a boundary ONLY via `await atomically(_thing_in_txn, ...)`; the
body is named `*_in_txn(txn, ...)`, sits immediately below the public
function that invokes it (adjacency), and its docstring opens with
`ATOMIC: <what shares fate> — <the law it serves>`. So an atom is
recognizable by verb, suffix, docstring and position — and
`grep -rn "ATOMIC:" app/crm` prints the whole system's atom inventory
with justifications. atomically() is ParamSpec-typed: pyrefly checks
every forwarded argument through the hop.
**The logic style:** GATHER (accessor reads) -> DECIDE (pure function
returning a plan — DB-free testable, loggable) -> APPLY (accessor writes).
No service classes, no repository interfaces; pure core + thin shell.

## The laws (each traced to an ADR or a scar this repo already has)

1. **Three layers, always.** queries → accessor → decoder. Raw asyncpg, no
   ORM, every value parameterized — a value reaching SQL via f-string is a
   blocker (one DB role means total blast radius).
2. **The boundary is the ownership map** (ADR 0001, amended 2026-08-23).
   One module owns each table; SQL touching a table exists ONLY in its
   owner's directory. Cross-module access goes through `contracts.py`
   functions — never a foreign SELECT/INSERT. No contract for what you
   need? That's a design conversation, not a workaround.
3. **Tenancy.** `merchant_id text NOT NULL` on every `crm_*` root table,
   first column of every unique index. `platform_*` tables never have a
   merchant column. No table stores a reseller — always a derived join.
4. **Idempotent by construction.** Every contract is safely callable
   twice — partial uniques and deterministic probes, not caller
   coordination. If two racing workers break your function, the design is
   wrong, not the callers.
5. **Facts vs commands.** Things that happened enter through
   `crm_event_raw` and a consumer (store raw first, 200 fast, understand
   later); callers who need a result NOW call the contract directly (the
   sync door). Consumers are order-tolerant and never signal each other.
6. **Fail closed** anywhere permission-adjacent: missing, NULL, erroring,
   unknown → NO, with the honest reason. No override parameter, no bypass
   flag, ever. Buddy-side mirrors are the opposite — fail-OPEN: recording
   a fact must never break the call that produced it. Don't confuse the
   two postures.
7. **Identity is resolve()'s monopoly.** The only creator of crm_customer
   rows; deterministic probe order, no fuzzy matching; handle changes
   follow the ADR 0021 evidence ladder (declared/observed overwrite,
   imported never displaces, inferred refused); a cross-customer handle
   collision is staple evidence (merge), never an error. Identity resolves
   at WRITE time — phone-matching at read is banned (ADR 0017).
8. **Normalize at every writer.** Phone E.164, email lowercased — enforced
   by helpers (`app/crm/shared/normalize.py`) AND table CHECKs. On
   platform_identity a format mismatch means the gate misses a suppression
   and someone who said "stop" gets contacted.
9. **Invariants live in the tables.** With one DB role, CHECKs and
   triggers are the only discipline-free enforcement: append-only logs,
   derived booleans, handle history. If a rule must survive any caller,
   it's a trigger, not a convention.
10. **No stored derived state a predicate can answer** (expired,
    in-window, overdue). Predicate at read, every time.
11. **Vocabulary in code, never CHECKs.** Channels, connectors, sources
    grow with the product — a new one is a deploy, not a migration (the
    027 scar). CHECKs on FORMAT (E.164, closed status enums) are required.
12. **Observability.** `set_log_context` at every entrypoint;
    `track_error` on degraded paths; log what was dropped when bounding
    coverage.

## Known scars (don't repeat them)

- God files (3,100-line types.py) — split before ~500 lines.
- Router→router imports — routers call accessors/contracts only.
- Re-export hub `__init__.py` — import by full path.
- Parking a provider's code at the module root to dodge the adapter rule.
  A connector has several FACES — send, onboard, templates — and CI rule 11
  gives each ONE composition root outside `providers/`: adapters answer to
  `send.py`, the non-send faces to `connectors.py`, vendor transport
  (`providers/meta/graph.py`) to neither. The scar: when the rule was
  folder-shaped, onboarding's Graph calls were moved to a root
  `meta_graph.py` to get around it, and the confined adapter then imported
  that unconfined file.
- `app/crm` importing `app/ai` — never; buddy imports crm contracts, and
  the DB accessor layer imports NEITHER (use the hook registry pattern in
  `app/database/accessor/breeze_buddy/lead_call_tracker.py`).
- LIKE-over-JSONB joins — handles are indexed columns.
- New env vars outside the config resolver.

## Checklist for any code change

- [ ] New SQL only in the owning module's `queries.py`, parameterized
- [ ] Cross-module needs met via `contracts.py` imports only
- [ ] New table? Canon-conformant migration (next number, one owner,
      merchant_id law, CHECKs/triggers for its invariants) + ownership
      map entry in migrations.md
- [ ] Contract functions have tests (incl. the race/idempotency path);
      bugfixes carry a regression test
- [ ] Fail posture explicit and correct (closed if permission-adjacent,
      open if a buddy-side mirror)
- [ ] `python scripts/check_crm_boundaries.py` clean (table ownership,
      SQL/driver confinement, import direction, handle discipline)
- [ ] `black` + `isort` + `autoflake` + `pyrefly` + `pytest tests/crm`
      clean before pushing
