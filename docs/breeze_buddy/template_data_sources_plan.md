# Template Data Sources Plan

Date: 2026-07-07

## Current Backup

The backend state before simplification was saved outside this repository as a fallback reference.

It contains a full repo copy plus `git-status.txt`, `tracked.diff`, `cached.diff`, `untracked-files.txt`, `head.txt`, and `branch.txt`.

Use it only as fallback reference. The PR should be based on the current simplified worktree.

## Source Of Truth

This document is the source of truth for the final PR.

Older planning docs under `docs/plans/2026-07-01-*data-sources*` describe abandoned keyed-record, workbook, lookup, tool, and protocol-specific designs. They are historical references only.

## Product Goal

Data sources should be easy to understand:

1. User opens an agent's Knowledge Base page.
2. User connects or reuses a source.
3. Backend discovers tabs.
4. User chooses which tabs to attach.
5. Backend stores only config in `template.data_sources`.
6. At runtime, backend fetches selected tabs.
7. Each selected tab becomes one text payload.
8. Custom Python can return the relevant tab text.
9. If needed, a selected tab can also become a template variable for flow text.

No protocol-specific UI is needed. No LLM lookup tool is needed. No keyed-record or workbook mode is needed for v1.

## Core Decision

One selected source tab becomes one rendered text payload.

```text
Google Sheet tab -> CSV string -> context/template variable
```

Runtime path:

```python
context["data"][source_name][target]["content"]
```

Example:

```python
faq_csv = context["data"]["health_companion"]["faq"]["content"]
```

## Why The Older Approach Was Too Heavy

The older design tried to support multiple concepts at once:

- `kind = table | keyed_records | workbook | text`
- `key_fields`
- nested exposure or presentation objects
- separate limits objects
- row-level JSON lookup
- workbook-level all-tab payloads
- protocol-specific UI
- possible LLM lookup/tool runtime

That made the UI hard to explain and the backend schema harder to review. The real v1 requirement is simpler: fetch selected tab text and let the flow/custom Python decide when to use it.

For v1, custom Python is mainly a controlled switch:

1. Inspect LLM arguments.
2. Pick the relevant configured tab.
3. Return that tab's text to the LLM.

It does not need a special row lookup schema in the data-source contract.

## Backend Contract

Template config:

```json
{
  "data_sources": [
    {
      "name": "health_companion",
      "data_source_id": "00000000-0000-0000-0000-000000000000",
      "is_active": true,
      "datasets": [
        {
          "target": "faq",
          "selector": {
            "sheet_name": "FAQ"
          },
          "format": "csv"
        },
        {
          "target": "overview",
          "selector": {
            "sheet_name": "Overview",
            "max_rows": 20
          },
          "format": "csv",
          "variable_name": "health_overview"
        }
      ]
    }
  ]
}
```

Dataset fields:

```text
target         Stable key inside context["data"][source_name]
selector       Source-specific selector; Google Sheets uses sheet_name and optional max_rows
format         csv | markdown; UI uses csv for v1
variable_name  Optional; when present, content is also available as {variable_name}
```

Runtime data:

```json
{
  "health_companion": {
    "faq": {
      "format": "csv",
      "content": "question,answer\nWhat documents?,Carry Aadhaar\n"
    },
    "overview": {
      "format": "csv",
      "variable_name": "health_overview",
      "content": "topic,value\ntone,warm\n"
    }
  }
}
```

## Backend Implementation

### Schema

File:

```text
app/ai/voice/agents/breeze_buddy/template/types.py
```

Keep:

- `DatasetUse`
- `TemplateDataSourceRef`
- `TemplateModel.data_sources`
- `CreateTemplateRequest.data_sources`
- `ReplaceTemplateRequest.data_sources`

`DatasetUse` contains only:

```python
target: str
selector: Dict[str, Any]
format: Literal["csv", "markdown"] = "csv"
variable_name: Optional[str] = None
```

Validate:

- `selector` is present.
- Google Sheets configs include `selector.sheet_name`.
- Dataset targets are unique inside one source ref.

Remove from the final contract:

- `kind`
- `key_fields`
- `mode`
- `exposure`
- `presentation`
- `limits`
- `workbook`
- `keyed_records`
- lookup/tool schemas

### Data Source CRUD And Discovery

Keep:

```text
app/api/routers/breeze_buddy/data_sources/
app/schemas/breeze_buddy/data_source.py
app/database/queries/breeze_buddy/data_source.py
app/database/accessor/breeze_buddy/data_source.py
app/database/decoder/breeze_buddy/data_source.py
app/database/migrations/035_create_data_source_table.sql
```

Responsibilities:

- Store reusable source records.
- Enforce reseller/merchant access.
- Store source config, not fetched sheet content.
- Discover available tabs and preview rows.

Discovery response stays simple:

```json
{
  "datasets": [
    {
      "name": "FAQ",
      "columns": ["question", "answer"],
      "preview_rows": []
    }
  ]
}
```

No `kind_hint` is needed.

Google Sheets credentials:

- Prefer `GOOGLE_CREDENTIALS_JSON` locally, as raw service-account JSON or a mounted JSON file path.
- If `GOOGLE_CREDENTIALS_JSON` is empty, use Application Default Credentials so Kubernetes pods can use Workload Identity.

### Template Attachment

Keep:

```text
app/database/migrations/036_add_data_sources_to_template.sql
app/database/queries/breeze_buddy/template.py
app/database/accessor/breeze_buddy/template.py
app/database/decoder/breeze_buddy/template.py
app/api/routers/breeze_buddy/templates/handlers.py
app/ai/voice/agents/breeze_buddy/utils/secrets.py
```

Responsibilities:

- Add `template.data_sources`.
- Return `data_sources` on full template reads.
- Preserve `data_sources` during GET-to-PUT round trips.
- Validate referenced data sources on create/update.

Do not fetch external sheet content during template CRUD.

### Fetch And Normalize

Keep:

```text
app/services/data_sources/models.py
app/services/data_sources/registry.py
app/services/data_sources/adapters/google_sheets.py
app/services/data_sources/normalizers.py
app/services/data_sources/cache.py
app/services/data_sources/runtime.py
```

Google Sheets adapter:

- `discover()` lists tabs and preview rows.
- Discovery uses one spreadsheet metadata call plus one `values:batchGet`.
- Runtime fetch uses `fetch_datasets()` to batch selected tabs per source.
- `selector.sheet_name` is required.
- Without `selector.max_rows`, runtime fetches the selected tab's used range.
- `selector.max_rows` is optional and caps loaded rows when set.

Normalizer output:

```python
{
    "format": dataset.format,
    "content": rendered_text,
    "variable_name": dataset.variable_name if set,
}
```

Do not output:

- `rows`
- `columns`
- `by_key`
- `tabs`
- `kind`
- `key_fields`
- `mode`

### Runtime Injection

Runtime path:

```text
FlowConfigLoader.load_data_sources()
  -> get_or_fetch_bundle()
  -> template.flow["_runtime_data"]
  -> Agent.runtime_data
  -> TemplateContext.runtime_data
  -> custom Python context["data"]
```

Template variable injection:

- If `variable_name` is present, add rendered content to template variables under that name.
- Flow text can use `{health_overview}`.
- The dataset is still available in `context["data"]`.

### Tests

Backend tests should cover:

- Dataset defaults to `csv`.
- Missing `selector.sheet_name` fails.
- Removed `mode` field is rejected.
- Duplicate dataset targets fail.
- Normalizer returns only text `content`.
- Runtime bundle isolates one bad tab from other tabs.
- Runtime bundle uses batch fetch when available.
- `load_data_sources()` injects `context["data"]`.
- `variable_name` content becomes available as a template variable.
- IVR templates skip data-source loading.

## Frontend Implementation

### Types

File:

```text
src/lib/types/dataSources.ts
```

`DatasetUse` matches backend:

```ts
{
  target: string;
  selector: Record<string, unknown>;
  format: 'csv' | 'markdown';
  variable_name?: string | null;
}
```

Remove frontend concepts:

- `DatasetKind`
- `DatasetMode`
- `DatasetExposure`
- `DatasetLimits`
- `kind_hint`
- lookup/tool vocabulary

### Knowledge Base Screen

File:

```text
src/routes/(app)/agents/[id]/knowledge-base/+page.svelte
```

Screen shows:

- connected source count
- attached tab count
- custom-code tab count
- prompt variable count
- existing reusable sources
- connected source cards with runtime name, tab name, custom-code key, and optional variable

Avoid:

- protocol-specific panels
- advanced mode panels
- key field UI
- workbook/all-tabs UI
- format dropdowns
- lookup/tool language

### Add Source Flow

Step 1: Source

- Connect new Google Sheet by URL, or attach an existing source.
- Ask for runtime name.
- Show a clear loading state while discovery is running.

Step 2: Tabs

- Show each discovered tab.
- Show columns and preview row count.
- Let user choose:
  - Use this tab
  - Create template variable
  - Optional max rows

Defaults:

- Most tabs default to selected.
- Obvious archive/notes/feedback tabs default to ignored.
- Selected tabs use `format = "csv"`.
- If max rows is blank, backend loads the selected tab's used range.
- If `Create template variable` is checked, `variable_name` is set.

### Responsive Behavior

Desktop:

- No horizontal overflow.
- Long source URLs wrap or truncate.
- Connected source cards use `min-w-0`.
- Dialog content stays within viewport.

Phone:

- Source cards stack.
- Tab review fields stack.
- Buttons fit without overflow.
- The same Knowledge Base route should behave like existing agent configuration pages.

## End To End Validation

Backend:

```bash
uv run --extra dev python -m pytest tests/test_template_data_sources_runtime.py
uv run --extra dev pyrefly check
```

Frontend:

```bash
pnpm check
```

Local E2E:

1. Start backend.
2. Start frontend on `localhost:5173`.
3. Open an agent Knowledge Base route.
4. Add or attach a Google Sheet source.
5. Confirm discovery returns tabs.
6. Attach selected tabs.
7. Save the template.
8. Reload template from API and verify `data_sources`.
9. Runtime-load the template and verify `_runtime_data`.
10. Verify `context["data"][source][target]["content"]` is available to custom Python.
11. Verify `{variable_name}` is rendered when configured.

## Merge Criteria

The PR is ready when:

- Only the simplified tab-text contract remains.
- New or PR-owned docs point to the tab-text contract.
- No user-facing UI mentions protocol, lookup, kind, key fields, or advanced modes.
- Backend tests pass.
- Frontend type checks pass.
- The Knowledge Base screen has no desktop or mobile horizontal overflow.
- The PR explains that v1 stores config only and fetches selected tabs at runtime.
