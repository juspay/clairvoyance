# G/04 — Ops nits and two rulings (N17, N2, N3)

**Track G · step 4** · **Kind**: fix · **PR title**: `fix(crm): worker pool message per role; call-template merchant rule; one goal predicate (enh G/04)` · **Depends on**: G/03 merged · **Notes**: `../../workflow-rollout/context/nits.md`

| Nit | Fix | Test |
|---|---|---|
| N17 | `worker_main.start_worker_role` message states the per-role connection need (walker: three transiently — claim, lead accessor, queue_message). | message text pinned |
| N2 | **Ask Swaroop** whether call nodes may use merchant-NULL (global) templates. Yes → state it in `execute_call`'s docstring; no → park with "template is not this merchant's". | the chosen behaviour pinned |
| N3 | Verify entry-side and walker-side goal predicates use the same `entered_event_at` COALESCE (rollout 06); add one test that pins both SQL strings to the same expression. | shared expression |
