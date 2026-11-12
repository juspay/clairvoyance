// Core mapping: one Claude Code telemetry log event -> NeuroLink-shaped
// metric increments, a `request_final` log record and a `proxy.request` span.
//
// Metric names, units, descriptions and label sets are copied verbatim from
// NeuroLink's proxyTracer.ts so existing OpenObserve dashboards keep working.
// Fields NeuroLink emits but Claude Code cannot know (account selection,
// routing, body bytes, fallbacks, rate-limit windows) are simply omitted —
// absence is the null.

import { createHash } from "node:crypto";
import { classifyEvent, errorTypeFromEvent, errorTypeFromStatus } from "./classify.mjs";

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const str = (v) => (typeof v === "string" && v !== "" ? v : undefined);
const round3 = (v) => Math.round(v * 1000) / 1000;

function stableTraceIds(seed) {
  const hex = createHash("sha256").update(`cc-metrics-sidecar:${seed}`).digest("hex");
  return { traceId: hex.slice(0, 32), spanId: hex.slice(32, 48) };
}

/**
 * @param {object} rec normalized log record: { timeUnixNano, body, attrs, resource, usage? }
 *   usage: latest plan-utilization snapshot for the account (from UsageStore):
 *   { fiveHour, sevenDay, reset5h, reset7d } — fractions 0..1.
 * @returns {{kind, metricOps: Array, logRecord?: object, span?: object}}
 */
export function transformEvent(rec) {
  const a = rec.attrs;
  const eventName = str(a["event.name"]) ?? "unknown";
  const kind = classifyEvent(eventName);

  const model = str(a.model) ?? "unknown";
  const account = str(a["user.email"]) ?? "unknown";
  const sessionId = str(a["session.id"]);
  const requestId = str(a.client_request_id) ?? str(a.request_id);
  const durationMs = num(a.duration_ms) ?? 0;
  const statusAttr = a.status_code ?? a.http_status_code;
  const statusNum =
    typeof statusAttr === "number"
      ? statusAttr
      : Number.parseInt(String(statusAttr ?? ""), 10) || undefined;

  if (kind === "ignore" || kind === "other") return { kind, metricOps: [] };

  if (kind === "retry") {
    return {
      kind,
      metricOps: [
        {
          instrument: "counter",
          name: "proxy_retries_total",
          labels: { model, account, reason: eventName.replace(/^api_request_/, "") },
          value: 1,
        },
      ],
    };
  }

  // ---- request | error ------------------------------------------------------

  const isError = kind === "error";
  const status = isError ? (statusNum !== undefined ? String(statusNum) : "unknown") : "200";
  const errorType = isError ? errorTypeFromEvent(eventName, statusNum) : undefined;
  const errorMessage =
    isError ? (str(a.error_message) ?? str(a.error) ?? eventName.replace(/^api_request_/, "")) : undefined;

  const tokens = {
    input: num(a.input_tokens) ?? 0,
    output: num(a.output_tokens) ?? 0,
    cacheRead: num(a.cache_read_tokens) ?? 0,
    cacheCreation: num(a.cache_creation_tokens) ?? 0,
  };
  const costUsd = num(a.cost_usd);
  const totalTokens = tokens.input + tokens.cacheRead + tokens.cacheCreation + tokens.output;

  const requestLabels = { model, account, status, stream: "true", mode: "full" };
  const tokenLabels = { model, account };

  const metricOps = [
    { instrument: "counter", name: "proxy_requests_total", labels: requestLabels, value: 1 },
    {
      instrument: "histogram",
      name: "proxy_request_duration_ms",
      labels: requestLabels,
      value: durationMs,
    },
  ];
  if (!isError) {
    metricOps.push(
      { instrument: "counter", name: "proxy_tokens_input", labels: tokenLabels, value: tokens.input },
      { instrument: "counter", name: "proxy_tokens_output", labels: tokenLabels, value: tokens.output },
      { instrument: "counter", name: "proxy_tokens_cache_read", labels: tokenLabels, value: tokens.cacheRead },
      {
        instrument: "counter",
        name: "proxy_tokens_cache_creation",
        labels: tokenLabels,
        value: tokens.cacheCreation,
      },
    );
    if (costUsd !== undefined) {
      metricOps.push({
        instrument: "counter",
        name: "proxy_cost_usd_total",
        labels: tokenLabels,
        value: costUsd,
      });
    }
  } else {
    metricOps.push({
      instrument: "counter",
      name: "proxy_errors_total",
      labels: { model, account, error_type: errorType, status },
      value: 1,
    });
  }

  const endNs = Number(rec.timeUnixNano ?? Date.now() * 1e6);
  const startNs = endNs - Math.max(0, durationMs) * 1e6;
  const { traceId, spanId } = stableTraceIds(requestId ?? `${sessionId ?? "?"}:${a["event.sequence"] ?? eventName}`);

  // Provenance extras Claude Code gives us that NeuroLink never had. Prefixed
  // so they can never collide with the mirrored NeuroLink attribute names.
  const claudeExtras = (recAttrs => {
    const out = [];
    const add = (k, v) => {
      if (v !== undefined && v !== null && v !== "") out.push([k, v]);
    };
    add("claude_code.query_source", recAttrs.query_source);
    add("claude_code.effort", recAttrs.effort);
    add("claude_code.user_id", recAttrs["user.id"]);
    add("claude_code.organization_id", recAttrs["organization.id"]);
    add("claude_code.terminal_request_id", recAttrs.request_id);
    add("claude_code.version", rec.resourceAttrs?.["service.version"]);
    add("claude_code.ttft_ms", num(recAttrs.ttft_ms));
    return out;
  })(a);

  const spanAttributes = [
    ["proxy.request_id", requestId ?? "unknown"],
    ["http.method", "POST"],
    ["http.target", "/v1/messages"],
    ["gen_ai.request.model", model],
    ["gen_ai.response.model", model],
    ["proxy.stream", true],
    ["proxy.client_app", "cli"],
    ...(sessionId ? [["session.id", sessionId]] : []),
    ["ai.tokens.input", tokens.input],
    ["ai.tokens.output", tokens.output],
    ["ai.tokens.total", totalTokens],
    ["ai.tokens.cache_read", tokens.cacheRead],
    ["ai.tokens.cache_creation", tokens.cacheCreation],
    ["gen_ai.usage.input_tokens", totalTokens - tokens.output],
    ["gen_ai.usage.output_tokens", tokens.output],
    ["gen_ai.usage.total_tokens", totalTokens],
    ["ai.cost.basis", "api-equivalent"],
    ["ai.cost.pricing_status", costUsd !== undefined ? "exact" : "unavailable"],
    ...(costUsd !== undefined
      ? [
          ["ai.cost.total", costUsd],
          ["ai.cost.currency", "USD"],
        ]
      : []),
    ["proxy.duration_ms", durationMs],
    ["proxy.mode", "full"],
    ["proxy.account", account],
    // Exact plan utilization from Claude Code statusline payloads (/v1/usage).
    // NeuroLink's attribute names: utilization fractions 0..1 per window.
    ...(rec.usage?.fiveHour !== undefined
      ? [["proxy.ratelimit.after.5h", round3(rec.usage.fiveHour)]]
      : []),
    ...(rec.usage?.sevenDay !== undefined
      ? [["proxy.ratelimit.after.7d", round3(rec.usage.sevenDay)]]
      : []),
    ...(rec.usage?.reset5h ? [["proxy.ratelimit.reset.5h", rec.usage.reset5h]] : []),
    ...(rec.usage?.reset7d ? [["proxy.ratelimit.reset.7d", rec.usage.reset7d]] : []),
    ["http.status_code", statusNum !== undefined ? statusNum : isError ? 0 : 200],
    ...(isError
      ? [
          ["error.type", errorType],
          ["error.message", errorMessage],
          ["error", true],
        ]
      : []),
    ...claudeExtras,
  ];

  const span = {
    traceId,
    spanId,
    parentSpanId: undefined,
    name: "proxy.request",
    kind: 1, // INTERNAL — matches NeuroLink's default root span kind
    startTimeUnixNano: String(Math.max(0, Math.round(startNs))),
    endTimeUnixNano: String(Math.round(endNs)),
    attributes: spanAttributes,
    status: isError ? { code: 2, message: errorMessage } : { code: 1 },
    events: [],
  };

  // Severity mirrors NeuroLink's emitOtlpLogRecord: WARN for 429, ERROR for
  // other failures, INFO otherwise.
  const severity = isError
    ? statusNum === 429
      ? { number: 13, text: "WARN" }
      : { number: 17, text: "ERROR" }
    : { number: 9, text: "INFO" };

  const logAttributes = [
    ["proxy.record_kind", "request_final"],
    ["proxy.accounting_scope", "client"],
    ["request.id", requestId ?? "unknown"],
    ["http.method", "POST"],
    ["http.path", "/v1/messages"],
    ...(statusNum !== undefined
      ? [["http.status_code", statusNum]]
      : isError
        ? [["http.status_code", 0]]
        : [["http.status_code", 200]]),
    ["response.time_ms", durationMs],
    ["ai.model", model],
    ["ai.stream", true],
    ["ai.pricing.basis", "api-equivalent"],
    ["ai.pricing.status", costUsd !== undefined ? "exact" : "unavailable"],
    ["ai.pricing.provider", "claude-code"],
    ...(costUsd !== undefined ? [["ai.cost.api_equivalent_usd", costUsd]] : []),
    ["account.name", account],
    ["account.type", "claude_code"],
    ["ai.input_tokens", tokens.input],
    ["ai.output_tokens", tokens.output],
    ["ai.cache_creation_tokens", tokens.cacheCreation],
    ["ai.cache_read_tokens", tokens.cacheRead],
    ...(isError ? [["error.type", errorType], ["error.message", errorMessage]] : []),
    ["trace.id", traceId],
    ["span.id", spanId],
    ...(sessionId ? [["session.id", sessionId]] : []),
    ["is_success", !isError && (statusNum === undefined || (statusNum >= 200 && statusNum < 400))],
    ["is_rate_limit", statusNum === 429],
    ["is_overloaded", statusNum === 529],
    ["is_error", isError],
    ["source", "otlp"],
    ...claudeExtras,
  ];

  const logRecord = {
    timeUnixNano: String(Math.round(endNs)),
    severityNumber: severity.number,
    severityText: severity.text,
    body: { stringValue: `POST /v1/messages → ${status} (${durationMs}ms)` },
    attributes: logAttributes,
  };

  return { kind, metricOps, logRecord, span };
}

/**
 * Fallback path for TOKEN_SOURCE=metric: derive proxy token/cost counters from
 * claude_code.token.usage / cost.usage sum datapoints.
 *
 * Claude Code (2.1.x) exports these with DELTA temporality, so each datapoint
 * is an increment that can be added straight into the aggregator's cumulative
 * counters. If a future client exports CUMULATIVE instead, datapoints are
 * returned as `passthrough` entries (last cumulative value per series) to
 * avoid double counting.
 *
 * @param {object} metricsRequest normalized OTLP metrics request
 * @returns {{ ops: Array, passthrough: Array }}
 */
export function transformMetricsFallback(metricsRequest) {
  const ops = [];
  const passthrough = [];
  for (const rm of metricsRequest.resourceMetrics ?? []) {
    const resourceAttrs = Object.fromEntries(
      (rm.resource?.attributes ?? []).map((p) => [p.key, p.value?.stringValue]),
    );
    if (resourceAttrs["service.name"] !== "claude-code") continue;
    for (const sm of rm.scopeMetrics ?? []) {
      for (const m of sm.metrics ?? []) {
        const sum = m.sum;
        if (!sum?.dataPoints) continue;
        const isDelta = sum.aggregationTemporality === 1;
        const targets =
          m.name === "claude_code.token.usage"
            ? {
                input: "proxy_tokens_input",
                output: "proxy_tokens_output",
                cacheRead: "proxy_tokens_cache_read",
                cacheCreation: "proxy_tokens_cache_creation",
              }
            : m.name === "claude_code.cost.usage"
              ? { "*": "proxy_cost_usd_total" }
              : {};
        for (const dp of sum.dataPoints) {
          const attrs = Object.fromEntries(
            (dp.attributes ?? []).map((p) => [p.key, p.value?.stringValue]),
          );
          const target = targets[attrs.type] ?? targets["*"];
          if (!target) continue;
          const value = dp.asDouble ?? dp.asInt ?? 0;
          const entry = {
            name: target,
            labels: { model: attrs.model ?? "unknown", account: attrs["user.email"] ?? "unknown" },
            value,
            timeUnixNano: dp.timeUnixNano,
            startTimeUnixNano: dp.startTimeUnixNano,
          };
          if (isDelta) ops.push({ instrument: "counter", ...entry });
          else passthrough.push(entry);
        }
      }
    }
  }
  return { ops, passthrough };
}
