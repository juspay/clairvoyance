import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  decodeProtobufMetricsRequest,
  decodeProtobufLogsRequest,
  countProtobufSpans,
} from "../src/protobuf.mjs";

test("protobuf metrics capture decodes to the same semantics as the JSON capture", () => {
  const m = decodeProtobufMetricsRequest(fs.readFileSync("test/fixtures/004-metrics.bin"));
  const rm = m.resourceMetrics[0];
  const res = Object.fromEntries(rm.resource.attributes.map((a) => [a.key, a.value?.stringValue]));
  assert.equal(res["service.name"], "claude-code");
  assert.equal(res["service.version"], "2.1.280");

  const scope = rm.scopeMetrics[0];
  assert.equal(scope.scope.name, "com.anthropic.claude_code");
  const names = scope.metrics.map((x) => x.name);
  assert.ok(names.includes("claude_code.token.usage"));
  assert.ok(names.includes("claude_code.cost.usage"));

  const token = scope.metrics.find((x) => x.name === "claude_code.token.usage");
  assert.equal(token.sum.isMonotonic, true);
  assert.equal(token.sum.aggregationTemporality, 1); // DELTA
  const byType = new Map(
    token.sum.dataPoints.map((dp) => {
      const a = Object.fromEntries(dp.attributes.map((x) => [x.key, x.value?.stringValue]));
      return [a.type, dp];
    }),
  );
  assert.equal(byType.get("input").asDouble, 2);
  assert.equal(byType.get("output").asDouble, 4);
  assert.equal(byType.get("cacheRead").asDouble, 23994);
  assert.equal(byType.get("cacheCreation").asDouble, 11458);
  for (const dp of token.sum.dataPoints) {
    assert.match(dp.startTimeUnixNano, /^\d+$/);
    assert.match(dp.timeUnixNano, /^\d+$/);
  }

  const cost = scope.metrics.find((x) => x.name === "claude_code.cost.usage");
  assert.equal(cost.unit, "USD");
  assert.ok(Math.abs(cost.sum.dataPoints[0].asDouble - 0.0506748) < 1e-9);
});

test("protobuf logs capture decodes with api_request attributes intact", () => {
  const l = decodeProtobufLogsRequest(fs.readFileSync("test/fixtures/002-logs.bin"));
  const logs = l.resourceLogs.flatMap((rl) => rl.scopeLogs.flatMap((sl) => sl.logRecords));
  assert.ok(logs.length > 10);
  const api = logs.find((lr) => {
    const a = Object.fromEntries(lr.attributes.map((x) => [x.key, x.value?.stringValue]));
    return a["event.name"] === "api_request";
  });
  assert.ok(api, "api_request record present");
  const attrs = Object.fromEntries(
    api.attributes.map((x) => [x.key, x.value?.stringValue ?? x.value?.intValue ?? x.value?.doubleValue]),
  );
  assert.equal(attrs.model, "claude-sonnet-5");
  assert.equal(attrs.input_tokens, 2);
  assert.equal(attrs.cache_read_tokens, 23994);
  assert.equal(typeof attrs.cost_usd, "number");
  assert.equal(attrs.duration_ms > 0, true);
  assert.equal(api.body.stringValue, "claude_code.api_request");
  assert.match(api.timeUnixNano, /^\d+$/);
});

test("malformed protobuf does not throw for span counting", () => {
  assert.doesNotThrow(() => countProtobufSpans(Buffer.from([0xff, 0xff, 0xff])));
});
