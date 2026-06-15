# Data Sources Architecture Comparison

## Context

Enable merchants to inject Google Sheets content into voice agent prompts at call time. Two architectures evaluated. Pick one.

---

## Architecture A: Separate Table (Current Implementation)

### Database

Two tables involved:

```sql
-- Migration 033
data_source (
    id UUID PRIMARY KEY,
    reseller_id TEXT NOT NULL,
    merchant_id TEXT,           -- NULL = shared
    name TEXT NOT NULL,           -- "Pricing Catalog"
    source_type TEXT DEFAULT 'google_sheet',
    spreadsheet_url TEXT NOT NULL,
    spreadsheet_id TEXT NOT NULL, -- extracted from URL
    sheet_name TEXT,
    columns JSONB,
    format TEXT DEFAULT 'markdown_table',
    is_active BOOLEAN DEFAULT TRUE,
    created_at, updated_at
)

-- Migration 034
template (
    ...existing columns...,
    data_sources JSONB            -- array of DataSourceRef
)
```

```json
// template.data_sources
[
  {
    "data_source_id": "ds_uuid_789",
    "name": "catalog",           // {catalog} in prompts
    "inject_as": "var"
  }
]
```

### API Surface

```
POST   /data-sources                        -- create data source
GET    /data-sources                        -- list (paginated)
GET    /data-sources/{id}                   -- get one
PUT    /data-sources/{id}                   -- update
DELETE /data-sources/{id}                   -- delete
GET    /data-sources/sheets/tabs            -- discovery (admin)
GET    /data-sources/sheets/columns         -- discovery (admin)
GET    /data-sources/sheets/preview         -- discovery (admin)

PUT    /templates/{id} with data_sources[]  -- attach refs
```

### Runtime Flow

```
Dispatch (background):
  prefetch_data_sources(lead_id, template)
    → per ref: get_data_source_by_id(ref.data_source_id)
    → fetch_formatted(spreadsheet_id, ...)
    → redis.setex("datasource:content:{ds_id}", content, TTL=60)

Call time (critical path):
  load_template_config(lead)
    → load_template() → _fetch_data_source_content(ref)
      → redis.get("datasource:content:{ds_id}") → hit → instant
      → miss → fetch_formatted(800ms timeout) → fallback
    → _ds_messages returned separately (not stored in template.flow)
    → build_flow_config(template, ds_messages=_ds_messages)
      → flow_config["_data_source_messages"] = ds_messages
    → prepare_initial_node(flow_config)
      → task_messages = ds_messages + task_messages
```

### Files Added/Modified (~1,600 lines)

| Layer | Files | Lines |
|-------|-------|-------|
| Migrations | 033, 034 | +90 |
| Queries | data_source.py | +140 |
| Decoder | data_source.py | +35 |
| Accessor | data_source.py | +160 |
| Schemas | data_source.py | +104 |
| Service | google/sheets.py | +260 |
| REST | data_sources/__init__.py, handlers.py | +425 |
| Runtime | loader.py, flow.py, prefetch.py, worker.py | +350 |

---

## Architecture B: Inline Config (Proposed)

### Database

One table. No separate entity.

```sql
-- Single migration
template (
    ...existing columns...,
    data_sources JSONB            -- array of FULL DataSourceRef
)
```

```json
// template.data_sources — inline, self-contained
[
  {
    "name": "catalog",
    "inject_as": "var",
    "spreadsheet_url": "https://docs.google.com/spreadsheets/d/ABC123/edit",
    "sheet_name": "Products",
    "columns": ["SKU", "Name", "Price"],
    "format": "markdown_table",
    "is_active": true
  }
]
```

### API Surface

```
GET    /data-sources/sheets/tabs            -- utility (admin)
GET    /data-sources/sheets/columns         -- utility (admin)
GET    /data-sources/sheets/preview         -- utility (admin)

PUT    /templates/{id} with data_sources[]  -- full inline config in template
```

No CRUD for data sources. No separate listing. Template editor manages everything inline.

### Runtime Flow

```
Call time only (no prefetch):
  load_template_config(lead)
    → load_template()
      → for ref in template.data_sources:
          if not ref.is_active: continue
          content = _fetch_from_cache_or_live(ref)
          if ref.inject_as == "message": _ds_messages.append({...})
          else: template_vars[ref.name] = content
      → return template, template_vars, _ds_messages
    → build_flow_config(template, ds_messages=_ds_messages)
    → prepare_initial_node(flow_config)
```

No dispatch-phase prefetch. No `data_source` table lookups. Redis cache key derived from spreadsheet_id + sheet_name + columns hash.

### Files Added/Modified (~500 lines)

| Layer | Files | Lines |
|-------|-------|-------|
| Migrations | 033 (add data_sources column to template) | +15 |
| Schemas | types.py (DataSourceRef gains fields) | +30 |
| Service | google/sheets.py | +260 |
| REST | data_sources/ (discovery only) | +100 |
| Runtime | loader.py | +80 |

---

## Comparison Matrix

| Dimension | A (Separate Table) | B (Inline Config) |
|---|---|---|
| **DB Tables** | 2 (data_source + template) | 1 (template only) |
| **Migrations** | 2 (033 + 034) | 1 (033 only) |
| **REST Endpoints** | 7 (+ 3 discovery) | 3 discovery only |
| **Files Added** | 21 | 4 |
| **Lines Added** | ~1,600 | ~500 |
| **Loom Pages** | Data Sources CRUD + Template attach + Variable picker | Template editor inline only |
| **Complexity** | High | Low |

| Use Case | A | B |
|---|---|---|
| One sheet → 1 template | Both fine | Both fine |
| One sheet → 3 templates | ✅ Edit in one place | ⚠️ Edit in 3 places |
| Deactivate sheet for all templates | ✅ Set `is_active=false` on row | ⚠️ Edit each template |
| Merchants sharing sheets | ✅ Central view | ❌ No central view |
| Audit "who uses this sheet?" | ✅ Query `template.data_sources` | ⚠️ Scan all templates |
| Future: per-template column overrides | ❌ Need duplicate data_source rows | ✅ Inline per ref |
| Phase 1 simplicity | ❌ Over-engineered | ✅ Minimal |
| Phase 3 RAG indexing | ✅ Index data_source row once | ⚠️ Index per template ref |

---

## Decision Guide

**Choose A if**:
- Merchant has 5+ templates sharing the same catalog/pricing sheet
- Need to deactivate all templates using a sheet instantly (compliance/regulatory)
- Want a Loom "Data Sources" management page for visibility
- Plan to add RAG indexing (index once per data_source, not per-template)

**Choose B if**:
- Most merchants have 1-2 templates, rarely sharing sheets
- Simplicity and speed to market matter most
- OK with template-by-template updates
- Want to ship Phase 1 fast, revisit separation if demand emerges

---

## My Recommendation: Architecture B

**Rationale**:

1. **YAGNI**: No merchant has asked for multi-template sheet sharing. We built a generic "entity management" system for a use case that doesn't exist yet.

2. **Complexity tax**: Architecture A adds 1,100 lines of deadweight (CRUD API, accessors, queries, decoders, Loom pages) for a feature nobody needs today.

3. **Inline is sufficient**: The actual value is "inject sheet content into prompts." Whether the config lives in `template.data_sources` or a separate table doesn't change the runtime behavior.

4. **Easy migration path**: If merchants later need sharing, migrate inline refs to a `data_source` table with a simple data migration. The API contract barely changes.

5. **Phase 2 readiness**: Both architectures support adding `mode: "preload" | "tool" | "rag"` identically.

**Tradeoff accepted**: Editing a sheet URL in 3 templates requires 3 PUTs. For small teams with 1-2 templates, this is acceptable.
