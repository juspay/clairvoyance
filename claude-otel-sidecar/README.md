# claude-code-metrics-sidecar

Receives **Claude Code's native OpenTelemetry exports** and re-emits them in
**NeuroLink proxy format**, so the OpenObserve dashboards your company built on
NeuroLink's telemetry (`proxy_*` metrics, `proxy.request` traces,
`neurolink_proxy` request logs) keep working — with NeuroLink removed from the
path:

```
Claude Code ──OTLP──▶ this sidecar (GCP, company-wide) ──OTLP──▶ OTel collector / OpenObserve
```

Zero runtime dependencies (plain Node ≥ 18.17, hand-rolled OTLP protobuf
codec). Accepts `http/json` and `http/protobuf` on the same port, and exports
OTLP **protobuf** — like NeuroLink's own exporters — because OpenObserve's
OTLP/JSON logs parser rejects legal `doubleValue` attributes on older builds
(their own regression suite covers this).

## What it maps

Claude Code (≥ 2.1.x, verified on 2.1.280) emits a `claude_code.api_request`
OTLP **log event** per model call containing `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `duration_ms`,
`ttft_ms`, `request_id`, `client_request_id`, `model`, `user.email`,
`session.id`, `query_source`, `effort`. That single event reconstructs the
entire NeuroLink request record.

### Metrics (names/units/labels copied verbatim from NeuroLink's proxyTracer.ts)

| NeuroLink metric | Labels | Sidecar source |
| --- | --- | --- |
| `proxy_requests_total` | `model, account, status, stream, mode` | each `api_request` event |
| `proxy_request_duration_ms` (histogram) | same as above | `duration_ms` |
| `proxy_tokens_input` | `model, account` | `input_tokens` |
| `proxy_tokens_output` | `model, account` | `output_tokens` |
| `proxy_tokens_cache_read` | `model, account` | `cache_read_tokens` |
| `proxy_tokens_cache_creation` | `model, account` | `cache_creation_tokens` |
| `proxy_cost_usd_total` | `model, account` | `cost_usd` |
| `proxy_errors_total` | `model, account, error_type, status` | `api_request_*` failure events |
| `proxy_retries_total` | `model, account, reason` | overload/fallback/retry events |

- `account` ← `user.email` (NeuroLink's per-account panels become per-developer).
- `status` ← `200` for completed calls, the event's `status_code` for failures,
  `unknown` when not reported. `stream="true"`, `mode="full"` (constants).
- `error_type` uses NeuroLink's own mapping: `rate_limit` (429), `auth`
  (401/403), `server` (5xx), `client`.
- Counters are exported **cumulative**, monotonic, every 15s — same temporality
  and cadence NeuroLink's MeterProvider used, so the dashboard's delta-per-window
  panels behave identically.
- Incoming `claude_code.*` metrics are **passed through untouched**, so the
  `claude_code_token_usage` / `claude_code_cost_usage` / `claude_code_session_count`
  streams (and tooling built on them) keep flowing.

### Not mapped (null — Claude Code never reports them)

`proxy_tokens_reasoning`, `proxy_request_body_bytes`,
`proxy_response_body_bytes`, `proxy_model_substitution_total`,
`proxy_fallback_*`, `proxy_codex_outbound_*`, account-selection/routing and
rate-limit-window attributes. Dashboard panels on these will simply show no
data rather than wrong data.

### Traces

One `proxy.request` span per model call (INTERNAL kind, like NeuroLink's root
span) with the full NeuroLink attribute set: `proxy.request_id`, `http.*`,
`gen_ai.request/response.model`, `ai.tokens.*` (NeuroLink semantics: standard
`gen_ai.usage.input_tokens` includes cache tokens), `ai.cost.*`,
`proxy.duration_ms`, `proxy.account`, `session.id`, plus `claude_code.*`
provenance extras (effort, query_source, version, ttft_ms, org id). Trace and
span IDs are derived deterministically from `client_request_id`, so re-ingests
are idempotent. Exported to the `neurolink_proxy` traces stream.

### Logs

One `request_final` log record per model call in the `neurolink_proxy` stream,
matching NeuroLink's requestLogger field names (`request.id`, `http.method`,
`http.status_code`, `response.time_ms`, `ai.model`, `ai.input_tokens`,
`ai.cache_read_tokens`, `account.name`, `error.type`, `trace.id`, `is_success`,
`is_rate_limit`, `is_overloaded`, `is_error`, `source=otlp`, …) with severity
WARN/ERROR/INFO mirroring NeuroLink's rules. Non-request Claude Code events are
dropped by default (set `FORWARD_OTHER_EVENTS=true` to keep them in a separate
`claude_code_events` stream).

## Quick start (local)

```bash
npm test                                   # 22 tests, no network needed
OTEL_EXPORT_ENDPOINT=http://127.0.0.1:14318 npm start
```

Then in another shell, run Claude Code against it (see `scripts/claude-env.sh`):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # required — see note in the script
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8080
claude -p "hi"
curl -s http://127.0.0.1:8080/stats
```

> `CLAUDE_CODE_ENABLE_TELEMETRY=1` is the enable flag on Claude Code 2.1.x
> (the older `CLAUDE_CODE_ENABLE_OTEL` name no longer activates it), and
> `OTEL_EXPORTER_OTLP_PROTOCOL` must be set explicitly or nothing is exported.

## GCP deployment (Cloud Run)

```bash
gcloud run deploy claude-otel-sidecar \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \        # or attach IAM + put the token in headers
  --set-env-vars OTEL_EXPORT_ENDPOINT=https://<openobserve-host>/api/<org> \
  --set-env-vars OTEL_EXPORT_HEADERS=Authorization=Basic%20<base64-creds> \
  --set-env-vars SIDECAR_INGEST_TOKEN=<shared-secret> \
  --set-env-vars LOGS_STREAM_NAME=neurolink_proxy \
  --set-env-vars TRACES_STREAM_NAME=neurolink_proxy \
  --min-instances=1                # keeps counters warm; scale-to-zero flushes on SIGTERM
```

Developers then only need (`scripts/claude-env.sh` does this):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=https://<sidecar-url>
export OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20<shared-secret>
```

Put those in the shell profile or the team's shared settings so every Claude
Code session reports from the start. Cloud Run's request timeout does not
affect ingestion (each OTLP POST is handled immediately; export to OpenObserve
is asynchronous with retry).

### Operation

- `GET /healthz`, `GET /readyz` — liveness.
- `GET /stats` — received/transformed/exported/dropped counters, series counts,
  last export status. Wire this to an uptime alert; `exported` flat while
  `received` grows means the backend path is broken.
- `SIGTERM` flushes queues before exit (verified).
- In-memory counters reset on restart — dashboards treat this as a counter
  reset, same as a NeuroLink proxy restart did.

## Configuration

See [.env.example](.env.example) for every knob and its default. Highlights:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OTEL_EXPORT_ENDPOINT` | — | OTLP base URL the sidecar exports to (collector or OpenObserve `/api/<org>`) |
| `SIDECAR_INGEST_TOKEN` | off | require `Authorization: Bearer <token>` on ingest |
| `FORWARD_RAW_METRICS` | `true` | pass `claude_code.*` metrics through unchanged |
| `TOKEN_SOURCE` | `event` | `event` (exact) or `metric` (fallback if log events disappear) |
| `FORWARD_OTHER_EVENTS` | `false` | keep non-request events in a `claude_code_events` stream |
| `LOGS_STREAM_NAME` / `TRACES_STREAM_NAME` | `neurolink_proxy` | OpenObserve stream routing |

## Repo layout

```
src/            server, config, decode (json+protobuf), classify, transform,
                aggregator (cumulative counters + histograms), otlp builders, exporter
test/           unit + e2e tests against a mock OTLP sink, using sanitized
                captures of real Claude Code 2.1.280 traffic (JSON and protobuf)
scripts/        capture-otlp.mjs    — record raw Claude Code OTLP for debugging
                make-fixtures.mjs   — scrub captures into test fixtures
                claude-env.sh       — developer-side env block
                verify-protobuf.mjs — decoder check against live captures
fixtures/       raw captures (gitignored — contain real user emails/ids)
```

## Testing against a changing Claude Code

The `claude_code.api_request` event shape is the contract. If a Claude Code
update renames fields, tests won't catch it — re-capture instead:

```bash
node scripts/capture-otlp.mjs 4318 fixtures/capture           # http/json
# run claude with OTEL_EXPORTER_OTLP_PROTOCOL=http/json + endpoint :4318 (see above)
node scripts/verify-protobuf.mjs                               # decoder sanity
node scripts/make-fixtures.mjs && npm test                     # regenerate + verify
```

## Exact plan-usage (5h / weekly % per developer)

NeuroLink filled `proxy.ratelimit.after.5h/7d` from Anthropic rate-limit
response headers — data Claude Code telemetry does not carry. Instead, the
sidecar ingests Claude Code's **statusline** payloads (>= 2.1.278 include the
server's own `rate_limits.limits[]`: percent per `session`/`weekly_all`
window), which are exact, not approximations:

1. `scripts/cc-usage-statusline.mjs` is installed as the dev's statusline
   command (`~/.claude/cc-usage-statusline.mjs`). It renders
   `Sonnet · 5h 43% · wk 67%` locally and POSTs the usage rows to the sidecar
   `/v1/usage` (debounced: every 30s or on >=1% change; token reused from
   `OTEL_EXPORTER_OTLP_HEADERS`).
2. The sidecar resolves the developer by `session_id -> user.email` (learned
   from `api_request` telemetry) and stamps the latest utilization onto every
   `proxy.request` span as `proxy.ratelimit.after.5h/.7d` (fractions 0-1) plus
   `proxy.ratelimit.reset.5h/.7d` — exactly the fields the "Account Usage %"
   dashboard tab queries. Data older than 2h is never stamped.

Note: usage rows exist only on claude.ai subscription lanes; API-key / custom
gateway models report none (panels simply show no data for those sessions).

## Known limits

- Incoming OTLP **traces** from Claude Code are counted and dropped (not part
  of the NeuroLink dashboard surface; the sidecar synthesizes better-shaped
  spans itself).
- `TOKEN_SOURCE=metric` assumes Claude Code's delta temporality (verified on
  2.1.280); cumulative sources fall back to last-value passthrough.
- One sidecar instance keeps its own counters; run a single instance (or accept
  counter resets per instance in dashboards).
- `test/cc-usage.test.mjs` is a leftover from an earlier CLI experiment
  (`cc-usage.mjs`, not in this repo) and is intentionally not part of `npm test`.
