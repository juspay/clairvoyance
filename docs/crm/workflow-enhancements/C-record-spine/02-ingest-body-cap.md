# C/02 — Ingest body cap that actually caps (P8, N13)

**Track C · step 2** · **Kind**: fix · **PR title**: `fix(crm): the ingest door caps the body it reads, not just the header it is told (enh C/02)` · **Depends on**: nothing · **Notes**: §3 record OBSERVATION, §11 P8, `nits.md` N13

## Design
- Replace `within_size_limit` (Content-Length only, and it runs after FastAPI has parsed the body into `EventIn`) with an ASGI middleware scoped to `/crm/ingest/*` in `app/crm/record/api.py` (mounted from `app/crm/api.py`): reject `content-length > MAX_LETTER_BYTES` with 413 before reading, and wrap `receive` to count streamed bytes and abort with 413 once the cap is exceeded (chunked bodies). Keep `MAX_LETTER_BYTES` where it is.
- Ordering: the middleware runs before auth (cheapest refusal first) — it leaks nothing (413 says only "too large").

## Red tests
- Oversized declared length → 413 before the handler; chunked body over the cap → 413; a 1 KB body → 200 path unchanged.
