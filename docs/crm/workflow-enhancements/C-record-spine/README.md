# Track C — Record spine

**What this track delivers**: operating the event spine: re-drive quarantined letters, a body cap that caps, the retention ADR, the retention sweep.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh C/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 event re-drive
2. 02 ingest body cap
3. 03 retention ADR (needs Swaroop's sign-off before 04)
4. 04 retention sweep

## Files this track owns
`app/crm/record/{api,ingest,workers}.py`, `record/db/*`, one static config, `docs/crm/adr/0023-*.md`.

## Shared files with other tracks
C/04 reads one outreach contract (`max_live_run_age_days`); nothing else crosses modules.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh C/"`); step files are never edited to say "done".
