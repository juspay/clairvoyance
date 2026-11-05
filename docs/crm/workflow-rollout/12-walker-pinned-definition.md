# Phase 12 — Walker reads the pinned definition

**Kind**: feat · **PR title**: `feat(crm): the walker executes the version a run entered under` · **Depends on**: 11 · **Notes**: §14.7 costs, §15.3

## Design
- `walker.py::walk_run`: replace `accessor.get_workflow(...)` + `workflow.definition` with: `workflow = accessor.get_workflow(...)` (still needed for `status` archived/paused) and `definition_doc = await _definition_for(run)` → `accessor.get_definition(run.merchant_id, str(run.workflow_id), run.workflow_version)`; `None` → `NodeParked(f"definition v{run.workflow_version} missing")` (honest park; phase 14's retention must never delete a referenced version).
- Cache: a small in-process LRU keyed `(workflow_id, version)` in `walker.py` (versions are immutable, so the cache never invalidates; bound it, e.g. 512 entries). Pure dict + OrderedDict; no new dependency.
- `enrol.py`: `workflow.version` already stamped into `workflow_version` at insert — verify it is the version whose `definition` was validated (it is: `enrol` reads `workflow.definition` of the row it was handed; after phase 11 the live row's `version` == latest version row).
- `plans.set_status("archived")` behaviour unchanged (walker ejects on next claim).

## Red tests
- `tests/crm/test_workflow_walker.py`: monkeypatch `get_definition` to return v3 for a run with `workflow_version=3` while `get_workflow` returns a row whose `definition` is v4 with a different node set → the walker acts on v3 (asserts the node executed is from v3); missing version → `park_run` called with "missing".
- Cache: second call with the same key does not hit the accessor.

## Acceptance
- Suite green; boundary clean; the runbooks note "runs finish on the version they entered under".

## Out of scope
- Goals/listening per version (13).
