# E/02 — Platform nits (N8, N9)

**Track E · step 2** · **Kind**: fix · **PR title**: `fix(crm): platform hygiene — total jsonb decoders, entry_is_live in use (enh E/02)` · **Depends on**: E/01 merged (same file) · **Notes**: `../../workflow-rollout/context/nits.md`

| Nit | Fix | Test |
|---|---|---|
| N8 | `platform/suppression.py::_load_dict/_load_list` → `shared/decode.jsonb_object/jsonb_list` (total; a malformed row can no longer raise inside the record atom). | malformed jsonb → empty, no raise |
| N9 | `entry_is_live` is called from `is_suppressed` logging when a probe returns True (which channel/entry blocked, no values), so it stops being test-only documentation. | log line names the channel |
