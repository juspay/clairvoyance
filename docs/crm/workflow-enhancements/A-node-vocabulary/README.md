# Track A — Node vocabulary

**What this track delivers**: the node types merchants ask for first: `condition`, letter facts, `http`, `split`, a dry-run simulator, then the outreach nits that live in the same files.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh A/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 condition node
2. 02 letter facts
3. 03 http action node (needs PR #987 merged — skip and return if not)
4. 04 split node
5. 05 simulate
6. 06 outreach nits

## Files this track owns
`app/crm/outreach/{nodes,schemas,plans,walker,entry}.py`, new `predicates.py`, `letters.py`, `simulate.py`; identity gains one contract (`customer_facts`); record gains one (`event_payload`); connectivity gains one (`message_id_for_dedupe`).

## Shared files with other tracks
Track B adds one registry entry to `nodes.py` and Track G touches `api.py`; whoever merges second rebases. No logical dependency either way.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh A/"`); step files are never edited to say "done".
