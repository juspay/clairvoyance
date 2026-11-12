// Round-trip tests: encoder output decoded by our own wire-format decoder.
// This is the same wire format OpenObserve's protobuf path consumes.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  encodeLogsRequest,
  encodeTracesRequest,
  encodeMetricsRequest,
  snapshotEntries,
  passthroughEntries,
} from "../src/protobuf-encode.mjs";
import {
  decodeProtobufLogsRequest,
  decodeProtobufTracesRequest,
  decodeProtobufMetricsRequest,
} from "../src/protobuf.mjs";
import { transformEvent } from "../src/transform.mjs";
import { attrsToObject } from "../src/decode.mjs";
import { Aggregator } from "../src/aggregator.mjs";

const CFG = { serviceName: "neurolink_proxy", serviceVersion: "1.0.0", environment: "prod" };

function apiRequestRecord() {
  const d = JSON.parse(fs.readFileSync("test/fixtures/002-logs.json", "utf8"));
  for (const rl of d.resourceLogs) {
    for (const sl of rl.scopeLogs) {
      for (const lr of sl.logRecords) {
        const attrs = attrsToObject(lr.attributes);
        if (attrs["event.name"] === "api_request") {
          return { timeUnixNano: lr.timeUnixNano, body: lr.body?.stringValue, attrs };
        }
      }
    }
  }
  throw new Error("fixture missing");
}

test("logs: encode -> decode round-trip preserves every field shape", () => {
  const out = transformEvent(apiRequestRecord());
  const buf = encodeLogsRequest([out.logRecord], CFG);
  const decoded = decodeProtobufLogsRequest(buf);
  const rec = decoded.resourceLogs[0].scopeLogs[0].logRecords[0];

  assert.equal(rec.severityNumber, 9);
  assert.equal(rec.severityText, "INFO");
  assert.equal(rec.body.stringValue, rec.body.stringValue); // present
  const attrs = attrsToObject(rec.attributes);
  assert.equal(attrs["proxy.record_kind"], "request_final");
  assert.equal(attrs["ai.model"], "claude-sonnet-5");
  assert.equal(attrs["account.name"], "dev00000@company.example");
  assert.equal(attrs["ai.input_tokens"], 2);
  assert.equal(attrs["ai.cache_read_tokens"], 23994);
  // THE bug case: a legal doubleValue attribute must survive the wire
  assert.equal(attrs["ai.cost.api_equivalent_usd"], 0.0506748);
  assert.equal(attrs["is_success"], true);
  assert.equal(rec.timeUnixNano, String(out.logRecord.timeUnixNano));

  const res = attrsToObject(decoded.resourceLogs[0].resource.attributes);
  assert.equal(res["service.name"], "neurolink_proxy");
});

test("traces: encode -> decode round-trip preserves span identity and attrs", () => {
  const out = transformEvent(apiRequestRecord());
  const buf = encodeTracesRequest([out.span], CFG);
  const decoded = decodeProtobufTracesRequest(buf);
  const span = decoded.resourceSpans[0].scopeSpans[0].spans[0];

  assert.equal(span.name, "proxy.request");
  assert.equal(span.traceId, out.span.traceId);
  assert.equal(span.spanId, out.span.spanId);
  assert.equal(span.kind, 1);
  assert.equal(span.status.code, 1);
  assert.equal(span.startTimeUnixNano, out.span.startTimeUnixNano);
  assert.equal(span.endTimeUnixNano, out.span.endTimeUnixNano);
  const attrs = attrsToObject(span.attributes);
  assert.equal(attrs["gen_ai.request.model"], "claude-sonnet-5");
  assert.equal(attrs["proxy.account"], "dev00000@company.example");
  assert.equal(attrs["ai.cost.total"], 0.0506748);
});

test("metrics: aggregator snapshot -> encode -> decode preserves names, labels, values", () => {
  const agg = new Aggregator();
  const out = transformEvent(apiRequestRecord());
  for (const op of out.metricOps) agg.apply(op);
  const buf = encodeMetricsRequest(snapshotEntries(agg.snapshot(), CFG));
  const decoded = decodeProtobufMetricsRequest(buf);
  const rm = decoded.resourceMetrics[0];
  assert.equal(attrsToObject(rm.resource.attributes)["service.name"], "neurolink_proxy");

  const byName = new Map(rm.scopeMetrics[0].metrics.map((m) => [m.name, m]));
  assert.ok(byName.has("proxy_requests_total"));
  assert.ok(byName.has("proxy_request_duration_ms"));
  assert.ok(byName.has("proxy_tokens_cache_read"));
  assert.ok(byName.has("proxy_cost_usd_total"));

  const reqs = byName.get("proxy_requests_total");
  assert.equal(reqs.sum.isMonotonic, true);
  assert.equal(reqs.sum.aggregationTemporality, 2);
  const labels = attrsToObject(reqs.sum.dataPoints[0].attributes);
  assert.deepEqual(labels, {
    model: "claude-sonnet-5",
    account: "dev00000@company.example",
    status: "200",
    stream: "true",
    mode: "full",
  });
  assert.equal(reqs.sum.dataPoints[0].asDouble, 1);

  const cost = byName.get("proxy_cost_usd_total");
  assert.ok(Math.abs(cost.sum.dataPoints[0].asDouble - 0.0506748) < 1e-12);

  const hist = byName.get("proxy_request_duration_ms");
  const hdp = hist.histogram.dataPoints[0];
  assert.equal(hdp.count, 1);
  assert.ok(hdp.sum > 2000);
  assert.equal(hdp.bucketCounts.length, 16);
  assert.equal(hdp.explicitBounds.length, 15);
});

test("metrics: passthrough of decoded claude_code metrics survives round-trip", () => {
  const d = JSON.parse(fs.readFileSync("test/fixtures/004-metrics.json", "utf8"));
  const buf = encodeMetricsRequest(passthroughEntries(d.resourceMetrics));
  const decoded = decodeProtobufMetricsRequest(buf);
  const rm = decoded.resourceMetrics[0];
  assert.equal(attrsToObject(rm.resource.attributes)["service.name"], "claude-code");
  const names = rm.scopeMetrics[0].metrics.map((m) => m.name);
  assert.ok(names.includes("claude_code.token.usage"));
  const token = rm.scopeMetrics[0].metrics.find((m) => m.name === "claude_code.token.usage");
  const byType = new Map(
    token.sum.dataPoints.map((dp) => [attrsToObject(dp.attributes).type, dp.asDouble]),
  );
  assert.equal(byType.get("input"), 2);
  assert.equal(byType.get("cacheRead"), 23994);
  assert.equal(token.sum.aggregationTemporality, 1); // delta preserved
});
