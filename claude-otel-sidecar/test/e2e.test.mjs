// End-to-end: sidecar server + mock OTLP sink. Feeds the real captured Claude
// Code payloads (JSON and protobuf) through /v1/logs and /v1/metrics and
// asserts the sink receives Neurolink-shaped telemetry.

import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import { createServer } from "../src/server.mjs";
import { loadConfig } from "../src/config.mjs";
import {
  decodeProtobufMetricsRequest,
  decodeProtobufLogsRequest,
  decodeProtobufTracesRequest,
} from "../src/protobuf.mjs";

function startSink() {
  const received = { metrics: [], logs: [], traces: [] };
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const buf = Buffer.concat(chunks);
      const isPb = (req.headers["content-type"] ?? "").includes("protobuf");
      const body = isPb
        ? req.url.includes("metrics")
          ? decodeProtobufMetricsRequest(buf)
          : req.url.includes("traces")
            ? decodeProtobufTracesRequest(buf)
            : decodeProtobufLogsRequest(buf)
        : JSON.parse(buf.toString("utf8"));
      const entry = { headers: req.headers, body, protobuf: isPb };
      if (req.url.includes("metrics")) received.metrics.push(entry);
      else if (req.url.includes("logs")) received.logs.push(entry);
      else if (req.url.includes("traces")) received.traces.push(entry);
      res.writeHead(200, { "content-type": "application/x-protobuf" });
      res.end("");
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () =>
      resolve({ server, port: server.address().port, received }),
    );
  });
}

async function post(port, path, body, headers = {}) {
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body,
  });
  return res;
}

async function withSidecar(t, sink, extraEnv = {}) {
  const cfg = loadConfig({
    PORT: "0",
    HOST: "127.0.0.1",
    OTEL_EXPORT_ENDPOINT: `http://127.0.0.1:${sink.port}`,
    LOGS_FLUSH_INTERVAL_MS: "200",
    METRICS_EXPORT_INTERVAL_MS: "60000", // manual flush in tests
    ...extraEnv,
  });
  const server = createServer(cfg, { autostart: false });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const port = server.address().port;
  t.after(async () => {
    await server.__exporter.shutdown();
    server.closeAllConnections?.();
    server.close();
  });
  return { server, port, cfg };
}

const flushLogs = async (s) => s.server.__exporter.flushLogsAndSpans();
const flushMetrics = async (s) => s.server.__exporter.flushMetrics();

test("captured claude code telemetry becomes neurolink metrics, logs and spans", async (t) => {
  const sink = await startSink();
  t.after(() => {
    sink.server.closeAllConnections?.();
    sink.server.close();
  });
  const sidecar = await withSidecar(t, sink);

  // 1. feed the logs capture (contains one api_request among other events)
  const logsJson = fs.readFileSync("test/fixtures/002-logs.json");
  const resLogs = await post(sidecar.port, "/v1/logs", logsJson);
  assert.equal(resLogs.status, 200);

  // 2. feed the metrics capture (passthrough + records count)
  const metricsJson = fs.readFileSync("test/fixtures/004-metrics.json");
  const resMetrics = await post(sidecar.port, "/v1/metrics", metricsJson);
  assert.equal(resMetrics.status, 200);

  // 3. feed the protobuf captures as a different client would
  const resLogsPb = await post(
    sidecar.port,
    "/v1/logs",
    fs.readFileSync("test/fixtures/002-logs.bin"),
    { "content-type": "application/x-protobuf" },
  );
  assert.equal(resLogsPb.status, 200);
  const resMetricsPb = await post(
    sidecar.port,
    "/v1/metrics",
    fs.readFileSync("test/fixtures/004-metrics.bin"),
    { "content-type": "application/x-protobuf" },
  );
  assert.equal(resMetricsPb.status, 200);

  // 4. flush logs/spans, then metrics
  await flushLogs(sidecar);
  await flushMetrics(sidecar);

  // ---- logs: one request_final record per api_request event, neurolink_proxy stream
  assert.ok(sink.received.logs.length >= 1);
  const logReq = sink.received.logs.find((r) =>
    r.body.resourceLogs?.[0]?.scopeLogs?.[0]?.logRecords?.some((lr) =>
      lr.attributes?.some((a) => a.key === "proxy.record_kind"),
    ),
  );
  assert.ok(logReq, "a log batch containing request_final records was exported");
  assert.equal(logReq.headers["stream-name"], "neurolink_proxy");
  const record = logReq.body.resourceLogs[0].scopeLogs[0].logRecords[0];
  const attrs = Object.fromEntries(record.attributes.map((a) => [a.key, Object.values(a.value)[0]]));
  assert.equal(attrs["proxy.record_kind"], "request_final");
  assert.equal(attrs["ai.model"], "claude-sonnet-5");
  assert.equal(attrs["account.name"], "dev00000@company.example");
  assert.equal(attrs["ai.input_tokens"], 2);
  assert.equal(attrs["ai.cache_read_tokens"], 23994);

  // ---- traces: proxy.request span
  assert.ok(sink.received.traces.length >= 1);
  const span = sink.received.traces[0].body.resourceSpans[0].scopeSpans[0].spans[0];
  assert.equal(span.name, "proxy.request");
  assert.equal(sink.received.traces[0].headers["stream-name"], "neurolink_proxy");
  assert.equal(sink.received.traces[0].protobuf, true, "exports use OTLP protobuf like neurolink");
  const spanAttrs = Object.fromEntries(span.attributes.map((a) => [a.key, Object.values(a.value ?? {})[0]]));
  assert.equal(spanAttrs["gen_ai.request.model"], "claude-sonnet-5");
  assert.equal(spanAttrs["proxy.account"], "dev00000@company.example");

  // ---- metrics: proxy_* cumulative counters with neurolink label sets,
  //      plus passthrough of the original claude_code.* metrics
  assert.ok(sink.received.metrics.length >= 1);
  const metricsBody = sink.received.metrics[0].body;
  const flat = metricsBody.resourceMetrics.flatMap((rm) =>
    rm.scopeMetrics.flatMap((sm) => sm.metrics.map((m) => ({ rm, m }))),
  );
  const names = flat.map((x) => x.m.name);
  for (const expected of [
    "proxy_requests_total",
    "proxy_request_duration_ms",
    "proxy_tokens_input",
    "proxy_tokens_output",
    "proxy_tokens_cache_read",
    "proxy_tokens_cache_creation",
    "proxy_cost_usd_total",
    // passthrough keeps original streams alive (OpenObserve stores these in
    // claude_code_token_usage / claude_code_cost_usage streams)
    "claude_code.token.usage",
    "claude_code.cost.usage",
  ]) {
    assert.ok(names.includes(expected), `metric ${expected} present (got: ${names.join(",")})`);
  }

  const requests = flat.find((x) => x.m.name === "proxy_requests_total").m;
  assert.equal(requests.sum.aggregationTemporality, 2); // CUMULATIVE like neurolink
  assert.equal(requests.sum.isMonotonic, true);
  const dp = requests.sum.dataPoints.find((d) =>
    d.attributes.some((a) => a.key === "account" && a.value.stringValue === "dev00000@company.example"),
  );
  assert.ok(dp, "requests datapoint for dev00000@company.example");
  const labelMap = Object.fromEntries(dp.attributes.map((a) => [a.key, a.value.stringValue]));
  assert.deepEqual(labelMap, {
    model: "claude-sonnet-5",
    account: "dev00000@company.example",
    status: "200",
    stream: "true",
    mode: "full",
  });
  // both JSON and protobuf captures of the same session were ingested
  assert.equal(dp.asDouble, 2);

  const duration = flat.find((x) => x.m.name === "proxy_request_duration_ms").m;
  const histDp = duration.histogram.dataPoints[0];
  assert.equal(histDp.count, 2);
  assert.ok(histDp.sum >= 2 * 2000);
  assert.ok(Array.isArray(histDp.bucketCounts) && histDp.bucketCounts.length === 16);
});

test("ingest auth is enforced when SIDECAR_INGEST_TOKEN is set", async (t) => {
  const sink = await startSink();
  t.after(() => {
    sink.server.closeAllConnections?.();
    sink.server.close();
  });
  const sidecar = await withSidecar(t, sink, { SIDECAR_INGEST_TOKEN: "s3cret" });

  const denied = await post(sidecar.port, "/v1/logs", '{"resourceLogs":[]}');
  assert.equal(denied.status, 401);

  const allowed = await post(sidecar.port, "/v1/logs", '{"resourceLogs":[]}', {
    authorization: "Bearer s3cret",
  });
  assert.equal(allowed.status, 200);
});

test("health and stats endpoints", async (t) => {
  const sink = await startSink();
  t.after(() => {
    sink.server.closeAllConnections?.();
    sink.server.close();
  });
  const sidecar = await withSidecar(t, sink);
  const health = await fetch(`http://127.0.0.1:${sidecar.port}/healthz`);
  assert.equal(health.status, 200);
  const stats = await (await fetch(`http://127.0.0.1:${sidecar.port}/stats`)).json();
  assert.equal(stats.transformed.requests, 0);
  assert.ok(stats.series);
});

test("one-command rollout assets are served with injected origin", async (t) => {
  const sink = await startSink();
  t.after(() => {
    sink.server.closeAllConnections?.();
    sink.server.close();
  });
  const sidecar = await withSidecar(t, sink);

  const inst = await fetch(`http://127.0.0.1:${sidecar.port}/install.sh`);
  assert.equal(inst.status, 200);
  const text = await inst.text();
  assert.ok(text.includes("set -euo pipefail"));
  assert.ok(
    text.includes(`http://127.0.0.1:${sidecar.port}`),
    "installer has the sidecar origin injected",
  );
  assert.ok(text.includes("cc-usage-statusline.mjs"));
  assert.ok(!text.includes("__SIDECAR_BASE__"));
  assert.ok(!text.includes("__INGEST_TOKEN__"));

  const mjs = await fetch(`http://127.0.0.1:${sidecar.port}/cc-usage-statusline.mjs`);
  assert.equal(mjs.status, 200);
  assert.ok((await mjs.text()).includes("rate_limits"));
});

test("other claude code events are dropped unless forwarding is enabled", async (t) => {
  const sink = await startSink();
  t.after(() => {
    sink.server.closeAllConnections?.();
    sink.server.close();
  });
  const sidecar = await withSidecar(t, sink, { FORWARD_OTHER_EVENTS: "true" });

  await post(sidecar.port, "/v1/logs", fs.readFileSync("test/fixtures/003-logs.json"));
  await flushLogs(sidecar);

  const rawBatches = sink.received.logs.filter((r) =>
    r.body.resourceLogs?.[0]?.scopeLogs?.[0]?.logRecords?.some((lr) =>
      lr.attributes?.some((a) => a.key === "event.name"),
    ),
  );
  assert.ok(rawBatches.length >= 1, "raw events forwarded");
  assert.equal(rawBatches[0].headers["stream-name"], "claude_code_events");
});
