# Phase 08 — Publish-time template check (G12)

**Kind**: feat · **PR title**: `feat(crm): publish refuses a send node whose template the registry does not know` · **Depends on**: 06 · **Notes**: §16.3 G12, §6 (connectivity templates, `approved_template`)

## Why
A `send` node names a `template` by NAME; today the first sign it is wrong is a `blocked / template_not_approved` row at dispatch time, hours after publish. The T23 registry (`crm_channel_template`, `app/crm/connectivity/templates.py`) can answer at publish.

## Design
- New connectivity contract `template_status(merchant_id, channel, name) -> Optional[str]`: the status of the registry row(s) with that name for this merchant+channel — `None` if no row, `"approved"` if exactly one approved row, else the most recent row's status (for the message). Implement in `connectivity/templates.py` (the one file owning registry reads), export from `contracts.py`. Accessor: a new query `templates_by_name_query(merchant, channel, name)` ordered by `status_updated_at DESC`.
- `plans.py::_publish_in_txn` (outreach): after `validate_definition` passes, for each `send` node whose channel `registers_templates_for(channel)` (connectivity `channels.py` — expose `registers_templates_for` via contracts too) call `template_status`; `None` → problem `"send node {id}: template '{name}' is not registered on {channel} for this merchant"`; not approved → problem `"... is '{status}', not approved"`. Refuse publish with `WorkflowValidationError`. Keep `validate_definition` PURE — the lookup is a gather step in the atom, not in the validator.
- `create_workflow`/`update_draft` do NOT check (drafts may precede approval).
- Boundary: outreach → `app.crm.connectivity.contracts` only (already imported for `queue_message`).

## Red tests
- `tests/crm/test_workflow_plans.py`: monkeypatch `template_status` → None / "pending" / "approved"; `_publish_in_txn` (with monkeypatched accessor) raises with the right message for the first two and publishes for the third; a plan with no send nodes never calls it.
- Connectivity: `templates_by_name_query` is merchant-first and parameterised.

## Acceptance
- Suite green; boundary clean (contract import only).

## Decisions already made
- Refuse, don't warn: an unapproved template on a LIVE plan is a guaranteed blocked send.
- Language ambiguity (approved in two languages) is refused here exactly as `approved_template` refuses it at send time — same rule, earlier.

## Out of scope
- Variable-shape validation against `components` (phase 34, N15/N16 warnings territory; not scheduled as a refusal).
