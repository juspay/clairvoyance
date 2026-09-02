# CRM workflow rollout — the ordered implementation queue (36 phases 00–35)

This folder is the ordered, PR-sized backlog for taking the CRM workflow layer
(`app/crm/outreach`, with touches in `record`, `connectivity`, `app/ai`) from
where it is on `release` today to the two target flows:

1. **Cart abandonment** (short board): checkout update → 30m → WhatsApp → 30m →
   rescue call → 1d → close; recovered at any point ends the run.
2. **Loan-onboarding drop-off** (long board): a customer stalls on any stage for
   30 minutes → call; stages may be skipped; runs live for weeks; ships as
   per-stage "clocks" first, becomes one pinned board once versioning exists.

Every phase is **one PR with one commit** (CI-enforced). Phases are ordered;
a later phase may assume every earlier phase is merged. Where a phase depends
on someone else's open PR it says so in *Dependencies* and what to do if that
PR has not merged yet.

## Read this before picking up any phase

0. `PIPELINE.md` — when #1041 merges, the waves, what runs in parallel, the
   handoff prompt, and how status is tracked. Read it if you are the person
   dispatching agents; agents read it too, once.

1. `CLAUDE.md` and `docs/crm/building-modules.md` — the module skeleton, the
   layer law, the atomic grammar, the twelve boundary rules. CI enforces them
   (`scripts/check_crm_boundaries.py`); do not fight the rule, fix the code.
2. `context/reading-notes.md` — the full read of every CRM module (sections
   0–8), the bug/issue tally (10–11), the two flows and PR #1041 (12), the loan
   funnel and the clocks-vs-boards decision (13–15), and the final flow shapes
   with the remaining functional gaps G1–G12 (16). Phase files cite these
   sections by number; **the notes are the source of truth for intent**.
3. `context/nits.md` — N1–N21, the small things; some phases pick them up.
4. The phase file itself, end to end, including *Out of scope* and *Decisions
   already made*. Do not re-open a decision the file marks as made; if you
   believe it is wrong, say so in the PR description and implement as written.

## Conventions every phase follows

- Branch: `claude/crm-phase-<NN>-<slug>` from `origin/release`. Target:
  `release`. Title/commit prefix as the phase file says (`feat(crm):`,
  `fix(crm):`, `docs(crm):`).
- One commit. Iterate with `git commit --amend` + `git push --force-with-lease`.
- Before pushing, unpiped so the exit code is real:
  `uv run black . && uv run isort . --profile black && uv run autoflake --in-place --remove-all-unused-imports --remove-unused-variables --exclude "app/__init__.py,.venv/*,venv/*" -r app/ && uv run pyrefly check && uv run pytest tests/crm && uv run python scripts/check_crm_boundaries.py && uv run python scripts/check_migrations.py --base origin/release`
  (`uv sync --extra dev` first; tests need `JWT_SECRET_KEY`, `JWT_ALGORITHM`,
  `SKIP_KMS_DECRYPT=true` in the environment.)
- **Every law change is a triple in one commit**: docs text + CI rule + a red
  test proving the rule fires. Every bug fix carries a regression test that
  fails on `release`.
- Migrations: take the **next free number at implementation time** (other open
  PRs — #1021 consent ledger, #1053 run counts — may take numbers first; run
  `check_migrations.py --base`). Never edit a merged migration; amend triggers
  with `CREATE OR REPLACE` in the new file (migration 060 is the precedent).
  New tables need a `TABLE_OWNERS` entry in `scripts/check_crm_boundaries.py`
  and an ownership-map entry in `docs/crm/migrations.md`.
- Vocabulary (channels, topics, node types, policy words) lives in code dicts,
  never in CHECKs. CHECKs on FORMAT and on closed status enums are required.
- Fail CLOSED anywhere permission-adjacent; buddy-side mirrors fail OPEN.
- Logs never carry a phone/email; use `app/crm/shared/redact.py`.
- Do not widen a phase. If you find an adjacent bug, note it in the PR
  description and leave it for its own phase; if none fits, say so and the
  dispatcher adds one.
- The PR description states: the phase number, what changed, the red test(s),
  and any *Decisions already made* you disagreed with.

## The queue

| # | Phase | Kind | Depends on |
|---|---|---|---|
| 00 | Land repeat entries ourselves (carry #1041 + P9, P10, N18–N20) | feat + fix | — |
| 01 | Outreach correctness fixes (B1, B3, B4) | fix | — |
| 02 | Keyed-plan admission (B2) | fix | 01 |
| 03 | Compare-and-set on enrollment writes (P1) | fix | 01 |
| 04 | Event attempts + quarantine (P2) | fix + migration | — |
| 06 | Goal tiers, goal key, occurred_at guard (G7, cart→order attribution) | feat + migration | 01, 03 |
| 07 | Plan templates + runbooks (cart board; loan funnel as clocks) | docs + tests | 00, 01, 02, 06 |
| 08 | Publish-time template check (G12) | feat | 06 |
| 09 | Runs reporting (G9) | feat | 06; coordinate PR #1053 |
| 10 | ADR: version pinning | docs | — (blocks 11) |
| 11 | Version storage + `on_publish` word | feat + migration | 10 |
| 12 | Walker reads the pinned definition; migrate mode | feat | 11 |
| 13 | Entry consumer evaluates per-run versions | feat | 12 |
| 14 | Version operations (drain/migrate-forward, retention, template guard) | feat | 13 |
| 15 | Topic branching + multi-topic entry + reply clearing | feat | 01, 13 |
| 16 | Facts on resume, stage facts, parked runs movable, restart-on-repeat anywhere | feat | 00, 15 |
| 17 | `stages` ladder sugar + loan funnel migration to a board | feat + docs | 14, 15, 16 |
| 18 | Outcome feedback into runs (G2, G3) | feat | PRs #1040/#1052 merged; 15 |
| 19 | Permission-gate wiring (G1) | feat | PR #1021 merged |
| 20 | `condition` node — branch on run and customer facts (G5 part 1, N1) | feat | 15, 16 |
| 21 | Letter facts — list-shaped payload data reaches templates at fire time (G4) | feat | 16 |
| 22 | `http` action node (G5 part 2) | feat | 15, 16, 20; PR #987 merged |
| 23 | `split` node — deterministic percentage branches (G5 part 3) | feat | 20 |
| 24 | `handoff` node + `crm_handoff` — a human closes it, the run continues (G6) | feat + migration | 15, 18; coordinate PR #963 |
| 25 | Simulate — dry-run a plan against a sample event (G10) | feat | 17, 20, 21 |
| 26 | Send pacing per merchant and channel (G11) | feat | 19 |
| 27 | Re-drive quarantined spine events | feat | 04 |
| 28 | Buddy call-template retirement guard against pinned versions | fix (buddy) | 14 |
| 29 | Staple carries the loser's handles (P3) | fix | ruling vs ADR 0021 |
| 30 | Tenancy FK on enrollment.customer_id (P4) | migration | — |
| 31 | Suppression probes by channel (P6) | feat | 19 |
| 32 | Redact run context on read (P7) | fix | 16 |
| 33 | Ingest body cap that actually caps (P8) | fix | — |
| 34 | Hygiene sweep (N2–N3, N8–N17) | fix | 06, 20 |
| 35 | Scale hardening — spine retention and the partitioning ADR | docs + migration | 27; ADR sign-off |

Phases 00–09 need no canon change. Phase 10 is the canon decision; 11–17
build on it. 18 and 19 are gated on other people's PRs and can slide. 20–26
are the product vocabulary (wave 6); 27–35 are hardening and hygiene (wave
7), closing every gap and probable issue the read-through found. There is
no backlog file: everything found has a phase or is listed under *Not doing*.

## Not doing (decisions, not deferrals)

- **In-call WhatsApp (`send_whatsapp` builtin)** — dropped 2026-09-02. The
  cart flow's WhatsApp is the workflow's `send` node only.
- **Merchant console / visual builder** — a separate product surface, not
  this repo's queue. Phase 25 (simulate) and the `stages` sugar (17) are what
  it will call.
- **Producer-owed scalar summaries** (manas's option a on #1041) — rejected in
  favour of phase 21's fire-time letter read.
- **Item-overlap goal logic** ("did the new order contain the abandoned
  items") — not expressible and not wanted; goal tiers (06) cover the cases. Phase
05 does not exist (its content became phase 00 when we decided to land
#1041 ourselves); numbering was kept so cross-references stay stable.

Deliberately NOT in the queue (decision, 2026-09-02): a `send_whatsapp`
builtin for WhatsApp from inside a buddy call. The cart flow's WhatsApp is
the workflow's `send` node only. Do not add it back without a new decision.
