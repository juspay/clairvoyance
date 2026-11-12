// claude-code-metrics-sidecar HTTP server.
//
// Receives Claude Code's native OTLP exports (http/json or http/protobuf) and
// re-emits NeuroLink-proxy-shaped telemetry:
//   - /v1/metrics: claude_code.* metrics are passed through untouched (keeps
//     existing claude_code_* OpenObserve streams alive); with TOKEN_SOURCE=metric
//     they additionally drive proxy_tokens_* / proxy_cost_usd_total.
//   - /v1/logs: `api_request*` events are transformed into proxy_* metric
//     increments, a `request_final` log record and a `proxy.request` span
//     (stream `neurolink_proxy`). Other events are dropped unless
//     FORWARD_OTHER_EVENTS=true (then forwarded to `claude_code_events`).
//   - /v1/traces: counted and dropped unless FORWARD_INCOMING_TRACES=true.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./config.mjs";
import { decompressBody, decodeOtlp, attrsToObject } from "./decode.mjs";
import { transformEvent, transformMetricsFallback } from "./transform.mjs";
import { Aggregator } from "./aggregator.mjs";
import { Exporter } from "./exporter.mjs";
import { UsageStore } from "./usage.mjs";

const SIGNALS = { "/v1/metrics": "metrics", "/v1/logs": "logs", "/v1/traces": "traces" };

export function createServer(cfg, { autostart = true } = {}) {
  const stats = {
    startedAt: new Date().toISOString(),
    received: { metrics: 0, logs: 0, traces: 0 },
    records: { logRecords: 0, spans: 0, metricsDatapoints: 0 },
    transformed: { requests: 0, retries: 0, errors: 0, otherEvents: 0 },
    exported: {},
    dropped: {},
    rejected: { auth: 0, decode: 0 },
    lastExportAt: null,
    lastExportError: null,
    lastIngestAt: null,
    usage: { received: 0, accounts: 0, lastAppliedAt: null },
  };
  const aggregator = new Aggregator({ maxSeries: cfg.maxSeries });
  const usageStore = new UsageStore();
  const exporter = new Exporter(cfg, stats, () => aggregator.snapshot());

  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === "GET" && (req.url === "/healthz" || req.url === "/readyz")) {
        return json(res, 200, { ok: true });
      }
      if (req.method === "GET" && req.url === "/stats") {
        return json(res, 200, {
          ...stats,
          series: {
            counters: aggregator.counters.size,
            histograms: aggregator.histograms.size,
            droppedSeries: aggregator.droppedSeries,
          },
        });
      }
      const signal = SIGNALS[req.url?.split("?")[0]];
      if (req.method === "POST" && signal) {
        return await handleOtlp(req, res, signal);
      }
      if (req.method === "POST" && req.url?.split("?")[0] === "/v1/usage") {
        return await handleUsage(req, res);
      }
      if (req.method === "GET") {
        const served = serveStatic(req, res);
        if (served) return served;
      }
      return json(res, 404, { error: "not found" });
    } catch (err) {
      stats.rejected.decode++;
      return json(res, 400, { error: err.message });
    }
  });

  async function handleOtlp(req, res, signal) {
    if (cfg.ingestToken && !authorized(req)) {
      stats.rejected.auth++;
      return json(res, 401, { error: "unauthorized" });
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const raw = Buffer.concat(chunks);
    let body;
    try {
      body = decompressBody(raw, req.headers["content-encoding"]);
    } catch {
      stats.rejected.decode++;
      return json(res, 400, { error: "bad content-encoding" });
    }

    stats.received[signal]++;
    stats.lastIngestAt = new Date().toISOString();

    try {
      if (signal === "metrics") {
        ingestMetrics(body, req.headers["content-type"]);
      } else if (signal === "logs") {
        ingestLogs(body, req.headers["content-type"]);
      } else {
        ingestTraces(body, req.headers["content-type"]);
      }
      return json(res, 200, {});
    } catch (err) {
      stats.rejected.decode++;
      return json(res, 400, { error: `decode failed: ${err.message}` });
    }
  }

  function ingestMetrics(body, contentType) {
    const decoded = decodeOtlp(body, contentType, "metrics");
    if (decoded?.__spansOnly || !decoded.resourceMetrics) return;
    const resourceMetrics = decoded.resourceMetrics;

    // Keep the original claude-code resourceMetrics for passthrough.
    exporter.enqueueRawMetrics(resourceMetrics);

    if (cfg.tokenSource === "metric") {
      const { ops, passthrough } = transformMetricsFallback(decoded);
      for (const op of ops) aggregator.apply(op);
      for (const p of passthrough) aggregator.applyCumulative(p);
    }
    for (const rm of resourceMetrics) {
      for (const sm of rm.scopeMetrics ?? []) {
        for (const m of sm.metrics ?? []) {
          const n =
            (m.sum?.dataPoints?.length ?? 0) +
            (m.gauge?.dataPoints?.length ?? 0) +
            (m.histogram?.dataPoints?.length ?? 0);
          stats.records.metricsDatapoints += n;
        }
      }
    }
  }

  // One-command rollout assets: GET /install.sh and GET /cc-usage-statusline.mjs
  function serveStatic(req, res) {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const proto = req.headers["x-forwarded-proto"] ?? (req.socket.encrypted ? "https" : "http");
    const host = `${proto}://${req.headers.host ?? "localhost:8080"}`;
    if (req.url === "/install.sh") {
      let text = fs.readFileSync(path.join(here, "..", "scripts", "install-hosted.sh"), "utf8");
      text = text
        .replaceAll("__SIDECAR_BASE__", host)
        .replaceAll("__INGEST_TOKEN__", cfg.ingestToken || "TOKEN-NOT-SET");
      res.writeHead(200, { "content-type": "text/x-shellscript", "cache-control": "no-cache" });
      res.end(text);
      stats.staticServed = (stats.staticServed ?? 0) + 1;
      return true;
    }
    if (req.url === "/cc-usage-statusline.mjs") {
      const text = fs.readFileSync(
        path.join(here, "..", "scripts", "cc-usage-statusline.mjs"),
        "utf8",
      );
      res.writeHead(200, { "content-type": "text/javascript", "cache-control": "no-cache" });
      res.end(text);
      stats.staticServed = (stats.staticServed ?? 0) + 1;
      return true;
    }
    return null;
  }

  async function handleUsage(req, res) {
    if (cfg.ingestToken && !authorized(req)) {
      stats.rejected.auth++;
      return json(res, 401, { error: "unauthorized" });
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    try {
      const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const applied = usageStore.record(payload);
      stats.usage = {
        received: usageStore.received,
        accounts: usageStore.byAccount.size,
        lastAppliedAt: applied ? new Date().toISOString() : null,
      };
      stats.lastIngestAt = new Date().toISOString();
      return json(res, 200, { ok: true, applied });
    } catch (err) {
      return json(res, 400, { error: err.message });
    }
  }

  function ingestLogs(body, contentType) {
    const decoded = decodeOtlp(body, contentType, "logs");
    for (const rl of decoded.resourceLogs ?? []) {
      const resourceAttrs = attrsToObject(rl.resource?.attributes);
      for (const sl of rl.scopeLogs ?? []) {
        for (const record of sl.logRecords ?? []) {
          stats.records.logRecords++;
          const attrs = attrsToObject(record.attributes);
          const eventName = attrs["event.name"] ?? record.body?.stringValue ?? "";
          if (attrs["session.id"] && attrs["user.email"]) {
            usageStore.associateSession(attrs["session.id"], attrs["user.email"]);
          }
          if (!eventName.startsWith("api_request") && eventName !== "api_request") {
            stats.transformed.otherEvents++;
            if (cfg.forwardOtherEvents) exporter.enqueueLog(record, "claude_code_events");
            continue;
          }
          const out = transformEvent({
            timeUnixNano: record.timeUnixNano,
            body: record.body?.stringValue,
            attrs,
            resourceAttrs,
            usage: usageStore.forAccount(attrs["user.email"] ?? "unknown"),
          });
          for (const op of out.metricOps) aggregator.apply(op);
          if (out.logRecord) exporter.enqueueLog(out.logRecord);
          if (out.span) {
            exporter.enqueueSpan(out.span);
            stats.records.spans++;
          }
          stats.transformed[out.kind === "request" ? "requests" : out.kind === "retry" ? "retries" : out.kind === "error" ? "errors" : "otherEvents"]++;
        }
      }
    }
  }

  function ingestTraces(body, contentType) {
    const decoded = decodeOtlp(body, contentType, "traces");
    const n = decoded?.__spansOnly?.spans ?? 0;
    stats.records.spans += n;
    if (!cfg.forwardIncomingTraces) return;
    // JSON passthrough of the original trace payload is not implemented; only
    // counting is supported for traces (Claude Code's own spans are not part
    // of the NeuroLink dashboard surface).
    stats.dropped["traces:passthrough-unimplemented"] = (stats.dropped["traces:passthrough-unimplemented"] ?? 0) + n;
  }

  function authorized(req) {
    const auth = req.headers.authorization ?? "";
    const token = auth.replace(/^Bearer\s+/i, "");
    return token === cfg.ingestToken || auth === cfg.ingestToken;
  }

  server.shutdown = async () => {
    await exporter.shutdown();
    server.close();
  };
  server.__exporter = exporter;
  server.__stats = stats;
  if (autostart) exporter.start();
  return server;
}

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

export function main() {
  const cfg = loadConfig();
  const server = createServer(cfg);
  server.listen(cfg.port, cfg.host, () => {
    console.log(
      `[sidecar] listening on http://${cfg.host}:${cfg.port} ` +
        `(metrics->${cfg.metricsEndpoint || "dry"} logs->${cfg.logsEndpoint || "dry"} ` +
        `traces->${cfg.tracesEndpoint || "dry"} tokenSource=${cfg.tokenSource} auth=${cfg.ingestToken ? "on" : "off"})`,
    );
  });
  const shutdown = async (sig) => {
    console.log(`[sidecar] ${sig} received, flushing…`);
    await server.shutdown();
    process.exit(0);
  };
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
