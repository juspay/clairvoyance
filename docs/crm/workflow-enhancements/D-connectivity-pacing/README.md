# Track D — Send pacing

**What this track delivers**: per-merchant, per-channel throughput budgets in the dispatcher, with deferral instead of refusal.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh D/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 send pacing

## Files this track owns
`app/crm/connectivity/{dispatch,channels,reasons}.py`, a Redis token bucket, one static config.

## Shared files with other tracks
None. Rollout 19 (gate wiring) is deferred; this phase owns its own deferral path and 19 reuses it later.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh D/"`); step files are never edited to say "done".
