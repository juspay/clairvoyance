---
paths:
  - "app/database/**/*.py"
  - "app/database/**/*.sql"
---

# Database Rules

## Migrations
- Migration files are sequential: `001_initial_tables.sql`, `002_...sql`, etc.
- NEVER modify existing migration files. Always create a new one with the next number
- Check the latest migration number in `app/database/migrations/` before creating a new one
- Migrations run raw SQL -- no ORM abstractions

## Query Pattern
- Query functions return `Tuple[str, List[Any]]` -- the SQL string and parameter values
- Use PostgreSQL parameterized placeholders: `$1, $2, $3` (NOT `%s` or `?`)
- Never use string formatting/interpolation in SQL queries -- always parameterize

## Accessor Pattern
- Accessor functions call query builders, execute via `run_parameterized_query()`, then decode
- Always wrap DB calls in try/except, log with `logger.error(...)`, and re-raise
- Return Pydantic models (via decoder), not raw asyncpg Records

## Connection Pool
- Global pool initialized in `app/database/__init__.py` via `asyncpg.create_pool()`
- Pool config: `POSTGRES_POOL_SIZE` + `POSTGRES_MAX_OVERFLOW`
- Credentials may be KMS-encrypted. Use `SKIP_KMS_DECRYPT=true` locally
