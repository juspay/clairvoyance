# F/02 — Identity nits (N10, N11)

**Track F · step 2** · **Kind**: fix + migration · **PR title**: `fix(crm): assert_facts reports a missing customer; trigram index for customer search (enh F/02)` · **Depends on**: nothing (do it first if F/01's ruling is pending) · **Notes**: `../../workflow-rollout/context/nits.md`

| Nit | Fix | Test |
|---|---|---|
| N10 | `identity/facts.py::_assert_facts_in_txn` returns `bool` (False when the customer row is missing); `assert_facts` returns it; `record/workers.py` logs at warning on False instead of silence. | False path pinned |
| N11 | Migration (next free number): `CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE INDEX crm_customer_display_name_trgm ON crm_customer USING gin (merchant_id, display_name gin_trgm_ops)` — note gin cannot mix btree columns without `btree_gin`; decide: `CREATE EXTENSION IF NOT EXISTS btree_gin` + the composite, or a plain `gin (display_name gin_trgm_ops)` and rely on the merchant predicate — choose the plain trgm index (simpler, the merchant filter is selective enough). `list_customers_query` unchanged (ILIKE uses it). | numbering guard; PR states the plan output if a DB was available |
