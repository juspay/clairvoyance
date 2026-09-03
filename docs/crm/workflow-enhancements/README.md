# CRM workflow enhancements — seven independent tracks, one agent each

The rollout queue (`../workflow-rollout/`, phases 00–19) is done except for
two items gated on other people's PRs (18's message half, 19). This folder is
the next layer: everything the read-through found that is still open, cut
into **tracks that do not depend on each other**, so each track can be handed
to its own agent and run in parallel. Inside a track the steps are strictly
ordered and each step is one PR.

## The tracks

| Track | Delivers | Steps | Gates outside our control |
|---|---|---|---|
| `A-node-vocabulary/` | condition · letter facts · http · split · simulate · outreach nits | 6 | A/03 needs PR #987 (SSRF guard) |
| `B-handoff/` | `crm_handoff` table + routes + letter; `handoff` node | 2 | coordinate with PR #963 if merged |
| `C-record-spine/` | event re-drive · ingest body cap · retention ADR · retention sweep | 4 | C/03 needs Swaroop's ADR sign-off |
| `D-connectivity-pacing/` | per-merchant channel throughput budgets | 1 | — |
| `E-platform-suppression/` | suppression by channel · platform nits | 2 | — |
| `F-identity/` | staple carries handles · identity nits | 2 | F/01 needs Swaroop's ruling (ADR 0021) |
| `G-outreach-hardening/` | enrollment FK · run redaction · buddy template guard · ops nits | 4 | G/04 N2 needs Swaroop's ruling |

Twenty-one steps in all. Tracks are **logically independent**: no step in
one track requires a step in another to be merged. They do share a few
files (`outreach/nodes.py`, `outreach/api.py`, `connectivity/dispatch.py`);
each track README names them. The rule is simple: whoever merges second
rebases. Merge order across tracks does not matter.

Still in `../workflow-rollout/`, not here: rollout 18's message half (after
#1040/#1052) and rollout 19 (after #1021). They stay under their original
files and numbers.

## Rules every track follows

Same as the rollout (`../workflow-rollout/README.md` "Conventions"), plus:
- Branch: `claude/crm-enh-<letter>-<nn>-<slug>` from `origin/release`. One
  commit per PR. PR title exactly as the step file says, ending `(enh X/NN)`.
- A step may start when the previous step of the SAME track is merged. A
  step never waits on another track.
- Migrations: **065 is taken**; take the next free number at implementation
  time and run `check_migrations.py --base origin/release` — two tracks (B/01,
  F/02, G/01, C/04 all carry migrations) may race for a number; renumber and
  amend, that is all.
- Rulings (C/03, F/01, G/04-N2): the agent asks Swaroop in its report and
  does the other steps of its track while waiting. It never assumes.
- Never edit a step file or the rollout context notes in a code PR. A
  decision change is a separate `docs(crm)` PR by the dispatcher.
- Status is the PR list: `is:pr "(enh"`. Nothing here is edited to say done.

## The kick-off prompt (one per track — paste with the letter filled in)

> You are the implementing agent for track `<LETTER>` of the CRM workflow
> enhancements in juspay/clairvoyance, end to end. I (Swaroop) merge every
> PR; you open them. Your track folder is
> `docs/crm/workflow-enhancements/<LETTER>-*/` on `origin/release`.
>
> First, read in this order and do not skip: `CLAUDE.md`;
> `docs/crm/building-modules.md`; `docs/crm/migrations.md`;
> `docs/crm/workflow-enhancements/README.md`; your track's `README.md`; every
> step file in your track folder; then `docs/crm/workflow-rollout/README.md`
> and `docs/crm/workflow-rollout/PIPELINE.md` for how the rollout was run;
> then the sections of `docs/crm/workflow-rollout/context/reading-notes.md`
> and `context/nits.md` your step files cite. Read the current code of the
> files your track owns against those notes and tell me in one paragraph
> anything that has drifted since they were written.
>
> Then work your track's steps strictly in order. A step is startable when
> the previous step's PR is MERGED into `origin/release` (verify with the
> GitHub tools). Never wait on another track. For each step: branch
> `claude/crm-enh-<letter>-<nn>-<slug>` from `origin/release`; one commit,
> amended as you iterate; red tests written first and shown failing on
> `release`; the full check list from the rollout README run unpiped before
> pushing (`uv sync --extra dev` first; export `JWT_SECRET_KEY`,
> `JWT_ALGORITHM`, `SKIP_KMS_DECRYPT=true`); a PR to `release` titled exactly
> as the step file's *PR title*. The body states: the step, what changed,
> the red tests and that they fail on release, anything in *Decisions
> already made* you disagreed with (implement as written regardless), and
> adjacent issues you noticed but left alone. Subscribe to the PR and drive
> it green; address review comments; never widen a step; never edit a step
> file or the context notes in a code PR.
>
> If a step needs a ruling or an external PR (your track README says which),
> ask me in your report and continue with the next step of your track that
> does not need it; come back when I answer. When your track's next step is
> blocked on a merge of your own previous PR, stop and report: the open PR
> with its link, what it unblocks, and any question you have. Do not poll; I
> will tell you to continue.

## The continue prompt (per track, after each merge)

> Continue track `<LETTER>` of the CRM workflow enhancements. Check
> `origin/release` for which of your PRs merged (search `is:pr "(enh <LETTER>/"`).
> Rebase any open PR of yours onto the new `origin/release` (keep one commit,
> `--force-with-lease`), re-run the check list, make sure CI is green. Then
> work the next startable step exactly as in the kick-off prompt. Stop and
> report as before.
