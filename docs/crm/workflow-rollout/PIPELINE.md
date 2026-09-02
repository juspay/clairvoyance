# The pipeline — how the 36 phases get merged, in what order, by one agent

This is the operating document for the queue in `README.md`. It answers: what
merges first, what runs in parallel, how status is tracked, and the exact
prompts to give the agent. The person dispatching (Swaroop) merges every PR;
the agent opens them. Update the *External PRs* table as those PRs move;
nothing else here should need editing until a phase changes shape.

## External PRs (state as of 2026-09-02)

| PR | Author | What | State | Gates |
|---|---|---|---|---|
| #1041 `feat/crm-repeat-entries` | manas-narra | `on_repeat` + `debounce_minutes` | open, CI green; **we land it ourselves as phase 00** (cherry-pick + 5 fixes) and ask for #1041 to be closed as superseded | 00 → 07, 16 |
| #1040 `feat/send-whatsapp-webhooks` | rab1prasad | WhatsApp webhooks → spine | open | 18 |
| #1052 `feat/whatsapp-extractor` | rab1prasad | WhatsApp inbound extractor | open | 18 |
| #1021 `feat/crm-permission-b1` | rab1prasad | consent ledger, decision log, `may_contact` | open | 19 |
| #1053 `feat/crm-workflow-list-counts` | sharifajahanshaik | per-workflow run counts, updated_by | open | 09, 14 coordinate |
| #1047 `feat/crm-event-catalog` | manas-narra | event catalog, where-grammar | open | none hard; 06/15 must not contradict its `where` grammar — read it before 06 |

## Phase 00 first — we land #1041 ourselves

Rather than wait for a fork branch we cannot push to, phase 00 cherry-picks
#1041's single commit onto our branch, folds in the two guards (P9: a
redelivered founding event never re-patches; P10: debounce only extends the
window via `GREATEST`) and the three CodeRabbit nits, keeps manas's
attribution in the commit, and opens our PR with a note asking a maintainer
to close #1041 as superseded. Everything downstream that needs repeat
entries (07's cart plan, 16) depends on phase 00 being merged, not on #1041.

## One agent, resumable, merge-driven

Use **one agent, one long-lived session**, not several. Reasons: waves 3 and
4 are strictly sequential; waves 1 and 2 share `outreach/db/queries.py`,
`entry.py`, `plans.py`, so parallel agents mostly buy rebases; decisions must
stay consistent across twenty PRs; and you are the only merger, so the
merge queue is the real bottleneck either way. Parallelism inside the agent
comes from it opening every PR whose dependencies are already merged before
it has to wait.

The loop is:
1. You paste the **kick-off prompt** (below). The agent reads the corpus,
   then works every phase that is startable now (00, 01, 04, 10), one branch
   and one PR each, and stops with a report listing the open PRs and what
   they unblock.
2. You review and merge. Then paste the **continue prompt**. The agent
   re-checks which phases are now unblocked, works them, stops, reports.
3. Repeat until phase 35 is merged.

A phase is DONE only when its PR is merged AND `pytest tests/crm` is green on
`release` afterwards. The PR list is the ledger — every rollout PR carries
`(rollout NN)` in its title; `is:pr "(rollout"` in the repo search shows the
queue's state. Phase files are never edited to say "done".

## Waves (what becomes startable when)

| Wave | Phases | Startable when | Shared files (the agent merges its own PRs' order, you merge serially) |
|---|---|---|---|
| 0 | 00 · 01 · 04 · 10 | now | 00 and 01 both touch `entry.py` — the agent opens 00 first, branches 01 from `release`, and rebases 01 after 00 merges |
| 1 | 02 · 03 | 01 merged (02, 03); | `outreach/db/queries.py` (02, 03) |
| 2 | 06 → then 07 · 08 · 09 | 06 after 01 and 03; 07 after 00, 02, 06; 08 and 09 after 06 | `plans.py` (06, 08), `entry.py` (06, 09) |
| 3 | 11 → 12 → 13 → 14 | 11 after 10 merged (needs your sign-off on the ADR); each after the previous | sequential by design |
| 4 | 15 → 16 → 17 → 20 | 15 after 13; 16 after 00 and 15; 17 after 14, 15, 16; 20 after 15 and 16 (do it after 17 to keep the wave serial) | `schemas.py`, `entry.py`, `walker.py`, `nodes.py` — sequential |
| 5 | 18 · 19 | 18 after 15 and #1040/#1052 merged; 19 after #1021 merged | independent |
| 6 | 21 · 22 · 23 → 24 · 25 · 26 | 21 after 16; 22 after 20 and #987; 23 after 20; 24 after 18; 25 after 17, 20, 21; 26 after 19 | `nodes.py`, `schemas.py`, `plans.py` in 21–25 — serial merges; 26 is connectivity-only |
| 7 | 27 · 28 · 29 · 30 · 31 · 32 · 33 · 34 → 35 | each per its *Depends on*; 29 and 35 need a ruling/ADR sign-off first | mostly disjoint modules; 34 last of the small ones, 35 last of all |

Milestones: **M1/M2** cart board publishable + loan clocks live after wave 2
(phase 07); **M3** versioning after wave 3; **M4** loan funnel is one pinned
board after wave 4; **M5** feedback loops + compliance after wave 5; **M6**
product vocabulary (condition/http/split/handoff/simulate/pacing) after wave
6; **M7** every probable issue and nit from the read-through closed after
wave 7 — the queue is then empty.

Merge order within a wave when several PRs are open: lowest phase number
first; ask the agent to rebase the rest (the continue prompt does that).

## Review gates

- Waves 0–2: a CRM maintainer review; the red tests and the boundary
  checker are the substance. Migrations (04, 06) get a second look at the
  `CREATE OR REPLACE` trigger amendment and the constraint name.
- Wave 3: phase 10 (the ADR) needs Swaroop's explicit sign-off before 11
  starts — it reverses a canon sentence (057: "never an execution pin").
- Wave 4: phase 17's expander test ("every node lists all downstream
  topics") is the gate.
- Wave 5: 19 is compliance — review with #1021's author.
- Wave 6: 22 (http) is an outbound-fetch surface — review with #987's author;
  24 (handoff) coordinate with #963.
- Wave 7: 29 and 35 each start with a ruling from Swaroop (ADR 0021 intent;
  ADR 0023 partitioning). The agent is told to ask, not assume.

## The kick-off prompt (paste once, at the start)

> You are running the CRM workflow rollout in juspay/clairvoyance, end to
> end, as the single implementing agent. I (Swaroop) merge every PR; you open
> them. Work from `docs/crm/workflow-rollout/` on `origin/release`.
>
> First, read in this order and do not skip: `CLAUDE.md`;
> `docs/crm/building-modules.md`; `docs/crm/migrations.md`;
> `docs/crm/workflow-rollout/README.md`; `docs/crm/workflow-rollout/PIPELINE.md`;
> `docs/crm/workflow-rollout/context/README.md`; then
> `docs/crm/workflow-rollout/context/reading-notes.md` in full (it is the
> source of truth for intent) and `context/nits.md`. Then read every phase
> file `00`–`35` once, so you know the whole queue before
> touching the first phase. Then read the current code of `app/crm/outreach`,
> `app/crm/record`, `app/crm/shared` and `scripts/check_crm_boundaries.py`
> against the notes and tell me in one paragraph anything that has drifted
> since the notes were written (commits after `719d88f` on `release`).
>
> Then work the queue. A phase is startable when every PR in its *Depends
> on* line is MERGED into `origin/release` (verify with the GitHub tools; an
> open PR does not count). Work every startable phase, lowest number first,
> each on its own branch `claude/crm-phase-NN-<slug>` from `origin/release`,
> one commit per branch (amend as you iterate), red tests written first and
> shown failing on `release`, the full check list from the README run unpiped
> before pushing (`uv sync --extra dev` first; export `JWT_SECRET_KEY`,
> `JWT_ALGORITHM`, `SKIP_KMS_DECRYPT=true`). Open a PR to `release` titled
> exactly as the phase file's *PR title* with ` (rollout NN)` appended. The
> body states: phase number and file, what changed, the red tests and that
> they fail on release, anything in *Decisions already made* you disagreed
> with (implement as written regardless), and adjacent bugs you noticed but
> left alone. Subscribe to each PR and drive it green; address review
> comments; never widen a phase; never edit a phase file or the context
> notes in a rollout PR (a `docs(crm)` PR of its own if a decision must
> change — and tell me).
>
> Phase 00 is special: it carries PR #1041 by cherry-pick with five fixes
> folded in; keep manas-narra's attribution exactly as the phase file says.
> Phase 01 touches the same file — open 00 first, then 01, and expect to
> rebase 01 after 00 merges.
>
> When no phase is startable, stop and report: the open rollout PRs with
> links, what each unblocks, and which external PRs (#1040, #1052, #1021)
> are still gating 18/19. Do not wait or poll for merges; I will tell you to
> continue.

## The continue prompt (paste after each merge round)

> Continue the CRM workflow rollout. Re-read `docs/crm/workflow-rollout/PIPELINE.md`
> and check `origin/release` for which rollout PRs merged since your last
> report (search PRs for "(rollout"). For every open rollout PR of yours,
> rebase onto the new `origin/release` (`git rebase`, keep one commit,
> `--force-with-lease`), re-run the check list, and make sure CI is green.
> Then work every newly startable phase exactly as in the kick-off prompt,
> lowest number first. Before starting phase 11, confirm with me that the
> phase 10 ADR is signed off — ask, do not assume. Stop and report as before.

## If something slips

- A phase's design turns out wrong mid-implementation: the agent stops,
  reports, and the phase file is corrected by you in a separate docs PR
  before any code is pushed. Drift is what this folder exists to prevent.
- #1021 stalls: phase 19 waits; everything else proceeds. Sends stay
  suppression-gated only — do not put `marketing.*` purposes in front of
  real customers without it.
- #1040/#1052 stall: phase 18 ships the call-outcome half only (its file
  says how).
- #1053 merges before 09/14: the agent extends its counts instead of
  duplicating (both phase files say so).
- Someone else lands a migration number you planned: `check_migrations.py
  --base origin/release` fails in CI; renumber, amend, push.
