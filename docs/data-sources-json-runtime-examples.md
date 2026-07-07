# Data Sources Runtime Examples

Date: 2026-07-07

## Mental Model

The template stores configuration only. It does not store Google Sheet rows in Postgres.

For each connected source, the template stores:

- which reusable source to use
- which tabs to fetch
- the custom-code key for each tab
- optional template variable names for tabs that should be inserted into flow text

At runtime, the backend fetches selected tabs and builds:

```python
context["data"]
```

Each selected tab becomes one text payload:

```python
context["data"][source_name][target]["content"]
```

The tab content is CSV text by default. This keeps the data-source feature source-agnostic and easy to use: Google Sheets today, docs or other providers later, with the same runtime shape.

## Google Sheets Credentials

Google Sheets can use credentials in two ways:

- `GOOGLE_CREDENTIALS_JSON` as raw service-account JSON or a mounted JSON file path.
- Application Default Credentials when `GOOGLE_CREDENTIALS_JSON` is not set, for example GCP Workload Identity in Kubernetes.

## Example Sheet

Assume a Google Sheet has these tabs:

```text
Overview
FAQ
Clinic Directory
Protocols
```

The user connects the sheet in Knowledge Base and names the runtime source:

```text
health_companion
```

## Template Config Before Fetching

This is what the template stores:

```json
{
  "data_sources": [
    {
      "name": "health_companion",
      "data_source_id": "00000000-0000-0000-0000-000000000000",
      "datasets": [
        {
          "target": "faq",
          "selector": {
            "sheet_name": "FAQ"
          },
          "format": "csv"
        },
        {
          "target": "clinics",
          "selector": {
            "sheet_name": "Clinic Directory",
            "max_rows": 200
          },
          "format": "csv"
        },
        {
          "target": "protocols",
          "selector": {
            "sheet_name": "Protocols",
            "max_rows": 200
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

Important:

- `target` is the key under `context["data"][source_name]`.
- `selector.sheet_name` chooses the tab.
- `selector.max_rows` is optional. If omitted, backend loads the selected tab's used range.
- `format` is currently `csv` or `markdown`; the UI uses `csv`.
- `variable_name` is optional. If set, the same fetched content is also available as `{variable_name}` in flow text.

## Runtime Fetch Flow

When a call starts:

```text
FlowConfigLoader.load_data_sources()
  -> get_or_fetch_bundle()
  -> GoogleSheetsAdapter.fetch_datasets()
  -> normalize()
  -> template.flow["_runtime_data"]
  -> Agent.runtime_data
  -> TemplateContext.runtime_data
  -> custom Python context["data"]
```

Backend may cache the fetched bundle in Redis. The call uses the fetched snapshot.

## Runtime Shape After Fetching

The runtime data looks like this:

```json
{
  "health_companion": {
    "faq": {
      "format": "csv",
      "content": "question,answer,category\nWhat documents are required?,Carry Aadhaar and recent reports,onboarding\nCan I book a clinic visit?,Yes ask for city and preferred time,booking\n"
    },
    "clinics": {
      "format": "csv",
      "content": "city,clinic_name,phone,address\nBengaluru,Aarokya Indiranagar,+91-90000-00001,12 CMH Road\n"
    },
    "protocols": {
      "format": "csv",
      "content": "condition_code,condition,questions,safe_response\nFEVER,Fever,\"Ask duration temperature age red flags\",Recommend doctor consult if high fever or red flags\n"
    },
    "overview": {
      "format": "csv",
      "variable_name": "health_overview",
      "content": "topic,value\nrole,health companion\ntone,warm and concise\n"
    }
  }
}
```

## Custom Python: Pick A Tab On Demand

Goal: the LLM passes simple arguments to a custom function, and custom Python returns the relevant tab's full text.

Function args:

```json
{
  "topic": "clinic"
}
```

Custom Python:

```python
def main(args, context):
    topic = (args.get("topic") or "").lower()
    source = context["data"]["health_companion"]

    if "clinic" in topic or "location" in topic:
        return source["clinics"]["content"]

    if "protocol" in topic or "symptom" in topic:
        return source["protocols"]["content"]

    return source["faq"]["content"]
```

The LLM receives only the returned tab text, not every configured tab.

## Flow Prompt: Template Variable

If a dataset has `variable_name`:

```json
{
  "target": "overview",
  "selector": {
    "sheet_name": "Overview",
    "max_rows": 20
  },
  "format": "csv",
  "variable_name": "health_overview"
}
```

The flow can use:

```text
Use this operating context:

{health_overview}
```

Backend replaces `{health_overview}` with the fetched tab text before rendering the flow prompt.

## Flow Prompt: Direct Runtime Data Is Not Inserted

For a normal dataset:

```json
{
  "target": "faq",
  "selector": {
    "sheet_name": "FAQ",
    "max_rows": 200
  },
  "format": "csv"
}
```

The flow should not use:

```text
{faq}
```

Normal datasets are available to custom Python through:

```python
context["data"]["health_companion"]["faq"]["content"]
```

Add `variable_name` only when the tab should be inserted directly into prompt text.

## What Is Loaded

Loaded:

- Only tabs selected in `data_sources[].datasets[]`.
- Whole selected tab used range, unless `selector.max_rows` is set.
- Rendered text for each selected tab.

Not loaded:

- Unselected tabs.
- Whole spreadsheet content by default.
- Separate keyed records.
- All workbook tabs as one object.
- Any automatic LLM lookup tool.

## Why This Covers The Main Cases

FAQ:

```python
return context["data"]["health_companion"]["faq"]["content"]
```

Clinic directory:

```python
return context["data"]["health_companion"]["clinics"]["content"]
```

Protocol tab:

```python
return context["data"]["health_companion"]["protocols"]["content"]
```

Small prompt snippet:

```text
{health_overview}
```

This keeps the backend and UI small while still allowing the agent to use different tabs in different parts of the flow.
