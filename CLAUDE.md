# Clairvoyance

Conversational AI platform for real-time voice and chat interactions, built around **Breeze Buddy** — a template-driven agent for outbound/inbound telephony, web chat, and widget voice — and the **Buddy CPaaS CRM** growing under `app/crm/`. Built on FastAPI + Pipecat-AI + asyncpg.

## Commands

```bash
./scripts/setup.sh                  # Python 3.11 check, git hooks, uv install
uv sync --extra dev                 # Install with dev tools
uv run python run.py                # Start FastAPI server on 0.0.0.0:8000

uv run black .                      # Format (pre-commit hook runs these)
uv run isort . --profile black
uv run autoflake --in-place --remove-all-unused-imports --remove-unused-variables --exclude "app/__init__.py,.venv/*,venv/*" -r app/
uv run pyrefly check                # Type check
uv run pytest tests/                # Tests
uv run python scripts/check_migrations.py       # Migration numbering guard
uv run python scripts/check_crm_boundaries.py   # CRM boundary guard (12 rules)
```

## Building modules & making code changes — READ THIS FIRST

**Any new module, table, contract, or non-trivial change follows
[`docs/crm/building-modules.md`](docs/crm/building-modules.md)** (skeleton, laws,
scars, checklist) and [`docs/crm/migrations.md`](docs/crm/migrations.md). The full
design corpus (canon, ADRs 0001–0021, per-module guides) is at
https://swaroopvarma1.github.io/buddy-cpaas-docs/ — **the corpus wins on any
conflict**. Applies to legacy areas too: touched buddy code is left better, never
worse. `scripts/check_crm_boundaries.py` enforces the laws in CI — run it before
pushing; if it fails, fix the code, don't fight the rule (rules changed only via
the corpus).

### The module skeleton (every `app/crm/<module>/`)

```
contracts.py   # THE public surface — the only file other modules import
api.py         # thin routes → logic (or db/accessor for trivial reads)
schemas.py     # leaf Pydantic shapes; imports nothing internal
<concern>.py   # BUSINESS LOGIC by name (resolve.py, facts.py, ingest.py):
               #   gather → decide (PURE, returns a plan) → apply
workers.py     # drain loops, only if the module owns one
db/            # ALL mechanics: __init__ (the door), accessor.py,
               #   queries.py ($1 params only), decoder.py
```

Layer law: `api → logic → db/accessor → db/queries`. Cross-module = the other
module's `contracts.py`, nothing else. Business logic is findable by FILENAME.

### The atomic grammar (transactions)

- Logic enters a DB boundary ONLY via `await atomically(_thing_in_txn, ...)`.
- The body is named `*_in_txn(txn, ...)`, sits immediately below its public
  entry, and its docstring opens `ATOMIC: <what shares fate> — <the law>`.
- **A logic file touches a handle in exactly one place: the `txn` param of an
  `_in_txn` body** (threading through the atom's private sub-steps included).
  Accessors self-scope single statements and batch loops (`crm_connection`,
  db-internal). `import asyncpg` only in `shared/db.py` and `db/`.
- `grep -rn "ATOMIC:" app/crm` = the system's atom inventory with reasons.

### Non-negotiable CRM laws (CI-enforced; full list in the docs)

- `merchant_id NOT NULL` + first column of every unique index on `crm_*` tables;
  `platform_*` tables never have a merchant column; no table stores a reseller.
- Fail CLOSED anywhere permission-adjacent (missing/NULL/error/unknown → NO; no
  bypass flags, ever). Buddy-side spine mirrors are the opposite: fail OPEN.
- `resolve()` is the only creator of customers: deterministic probes, no fuzzy
  matching, evidence-ladder handle overwrites (ADR 0021), collisions staple.
- Normalize at every writer (E.164 phone, lowercased email) — a format mismatch
  on a suppressed value CONTACTS someone who said stop.
- Vocabulary (channels/connectors/sources) lives in code, never CHECKs; CHECKs
  on FORMAT are required. No stored state a predicate can answer.
- The data layer (`app/database`) imports neither `app.ai` nor `app.crm` — taps
  use the hook registry in `accessor/breeze_buddy/lead_call_tracker.py`.

## Architecture

```
app/
├── main.py                         # FastAPI entry with lifespan
├── crm/                            # Buddy CPaaS (docs/crm/building-modules.md)
│   ├── api.py, auth.py             # /crm surface plumbing (root holds nothing else)
│   ├── identity/                   # crm_customer · resolve(), assert_facts()
│   ├── platform/                   # platform_identity · suppression contracts
│   ├── record/                     # crm_event_raw · record_event() (the spine)
│   └── shared/                     # db.py (atomically/crm_connection), normalize.py
├── ai/voice/
│   ├── agents/breeze_buddy/        # Telephony + chat + widget agent
│   │   └── crm_mirror.py           # Buddy→spine taps (ADR 0017), hook-registered
│   ├── llm/  stt/  tts/            # Provider wrappers
├── api/routers/breeze_buddy/       # leads, templates, analytics, websocket, auth
├── core/                           # config (static/dynamic), logger, security,
│                                   #   background_tasks, transport
├── database/
│   ├── migrations/                 # Sequential SQL (docs/crm/migrations.md)
│   ├── queries/ accessor/ decoder/ # Legacy three-layer (SQL → logic → Pydantic)
├── schemas/  services/  utils/
```

## Code Conventions

- **Python 3.11+**, `uv` (not pip/poetry); black (88), isort, autoflake, pyrefly
- Type hints on signatures; Pydantic for API schemas; async everything
- **No ORM** — raw asyncpg, `$1` placeholders; any value via f-string into SQL is
  a blocker (one DB role: total blast radius)
- snake_case functions, PascalCase classes, SCREAMING_SNAKE_CASE constants

## Git Workflow

- Commit prefixes: `feat:`, `fix:`, `fix(scope):`, `refactor:`, `docs:`
- **PRs contain exactly 1 commit** (CI-enforced) — iterate with
  `git commit --amend` + `git push --force-with-lease`
- Main branch: `release`; PRs target `release`
- CI: black/isort/autoflake/pyrefly, migration numbering + immutability,
  crm boundary guard, commit count

## Working practices (learned the hard way — follow them)

- **Never pipe test output when the exit code gates a commit** — `pytest | tail`
  exits 0 on failure. Run checks unpiped (or check PIPESTATUS) before any amend.
- **Full-file writes beat string-patching** on formatter-touched files — isort
  collapses/reorders imports and silently defeats exact-string replaces. If you
  script an edit, assert per-file that the pattern matched.
- **Verify wiring by running, not by reading**: import-smoke the routers,
  registries and contracts after structural changes (hooks registered? routes
  mounted?) — pyrefly passing does not prove runtime wiring.
- **Every law change is a triple in one commit**: docs text + CI rule + a red
  test proving the rule fires. Never ship a convention without its enforcement.
- Migrations: never edit a merged one; next number; one table owner per file.
- New tables: canon-conformant DDL + `TABLE_OWNERS` entry in
  `scripts/check_crm_boundaries.py`, or CI fails the PR by itself.
- **DB code must survive transaction pooling** (PgBouncer/Cloud SQL MCP):
  never open an asyncpg pool/connection without `statement_cache_size=0`
  (CI-guarded), no session state (LISTEN/NOTIFY, session `SET`, advisory
  locks, temp tables), and `SELECT ... FOR UPDATE` only inside a transaction —
  through `run_parameterized_query()` it silently does not lock. Full
  contract + local rig: `docs/PGBOUNCER.md`.

## Breeze Buddy essentials

- **Templates** are JSON in PostgreSQL (`{initial_node, nodes[...]}`), variables
  `{placeholder}`-resolved from the lead payload; node transitions are LLM
  function calls with async hooks. `template/types.py` is the source of truth.
- **Lead flow**: `/push/lead/v2` → BACKLOG → cron dispatch → pre-checks →
  telephony call (Twilio/Plivo/Exotel) → template pipeline → outcome on the lead.
  CRM taps ride the accessor hook registry — never import app/ai from the data
  layer.
- **Config hierarchy**: static env (loaded once) → dynamic (Redis/DevCycle) →
  template-level → playground override. Never read `os.environ` in module code.
- **Chat mode**: stateless per turn (fresh agent per POST /message, history
  replayed from DB, per-session RedisLock). Spec: `docs/CHAT_MODE.md`.
- **Errors**: `track_error(...)` to collect; fail-open degradation on the voice
  path. **Observability**: OTEL→Langfuse; `set_log_context()` at entrypoints.

## Important

- Secrets are KMS-encrypted in the DB; `SKIP_KMS_DECRYPT=true` locally without AWS
- `app/core/security/jwt.py` requires `JWT_SECRET_KEY` + `JWT_ALGORITHM` env vars
  at import — any script importing the API surface transitively needs them
- Redis distributed locking prevents duplicate background tasks across pods
- CORS via `CORS_ALLOWED_ORIGINS` env var in main.py
