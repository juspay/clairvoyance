// Environment-driven configuration for the sidecar.
// Every knob mirrors either a NeuroLink OTel default or a standard OTEL_* variable.

import { readEnvHeaders } from "./headers.mjs";

const bool = (env, name, def) => {
  const v = env[name];
  if (v === undefined) return def;
  return ["1", "true", "yes", "on"].includes(v.toLowerCase());
};

const int = (env, name, def) => {
  const v = Number.parseInt(env[name] ?? "", 10);
  return Number.isFinite(v) && v > 0 ? v : def;
};

export function loadConfig(env = process.env) {
  const exportEndpoint = (
    env.OTEL_EXPORT_ENDPOINT ??
    env.OTEL_EXPORTER_OTLP_ENDPOINT ??
    ""
  ).replace(/\/+$/, "");

  // Standard per-signal endpoints win over the base endpoint when set.
  const metricsEndpoint =
    env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT ??
    (exportEndpoint ? `${exportEndpoint}/v1/metrics` : "");
  const tracesEndpoint =
    env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT ??
    (exportEndpoint ? `${exportEndpoint}/v1/traces` : "");
  const logsEndpoint =
    env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT ??
    (exportEndpoint ? `${exportEndpoint}/v1/logs` : "");

  const cfg = {
    host: env.HOST ?? "0.0.0.0",
    port: int(env, "PORT", 8080),

    ingestToken: env.SIDECAR_INGEST_TOKEN ?? "",

    metricsEndpoint,
    tracesEndpoint,
    logsEndpoint,
    exportHeaders: readEnvHeaders(env.OTEL_EXPORT_HEADERS ?? ""),
    // OpenObserve routes OTLP logs/traces into a stream named by this header
    // (mirrors NeuroLink's collector: `stream-name: neurolink_proxy`).
    logsStreamName: env.LOGS_STREAM_NAME ?? "neurolink_proxy",
    tracesStreamName: env.TRACES_STREAM_NAME ?? "neurolink_proxy",

    // NeuroLink's MeterProvider flushed every 15s with cumulative temporality.
    metricsIntervalMs: int(env, "METRICS_EXPORT_INTERVAL_MS", 15000),
    // NeuroLink's BatchLogRecordProcessor: 2s delay, batches of 64.
    logsFlushIntervalMs: int(env, "LOGS_FLUSH_INTERVAL_MS", 2000),
    logsBatchSize: int(env, "LOGS_BATCH_SIZE", 64),
    exportTimeoutMs: int(env, "EXPORT_TIMEOUT_MS", 15000),
    exportRetries: int(env, "EXPORT_RETRIES", 2),

    forwardRawMetrics: bool(env, "FORWARD_RAW_METRICS", true),
    forwardOtherEvents: bool(env, "FORWARD_OTHER_EVENTS", false),
    forwardIncomingTraces: bool(env, "FORWARD_INCOMING_TRACES", false),

    // 'event'  — tokens/cost counters driven by claude_code.api_request log events.
    // 'metric' — tokens/cost derived from claude_code.token.usage / cost.usage
    //            cumulative datapoints (value passthrough). Use only if log
    //            events are unavailable; never run both sources for the same
    //            metric or you will double-count.
    tokenSource: (env.TOKEN_SOURCE ?? "event").toLowerCase(),

    serviceName: env.SERVICE_NAME ?? "neurolink_proxy",
    environment: env.DEPLOYMENT_ENVIRONMENT ?? env.ENVIRONMENT ?? "prod",
    serviceVersion: env.SERVICE_VERSION ?? "1.0.0",

    maxSeries: int(env, "MAX_SERIES", 20000),
  };
  return cfg;
}
