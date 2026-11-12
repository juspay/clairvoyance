import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { transformEvent, transformMetricsFallback } from "../src/transform.mjs";
import { attrsToObject } from "../src/decode.mjs";

function loadApiRequestRecord() {
  const d = JSON.parse(fs.readFileSync("test/fixtures/002-logs.json", "utf8"));
  for (const rl of d.resourceLogs) {
    for (const sl of rl.scopeLogs) {
      for (const lr of sl.logRecords) {
        const attrs = attrsToObject(lr.attributes);
        if (attrs["event.name"] === "api_request") {
          return {
            timeUnixNano: lr.timeUnixNano,
            body: lr.body?.stringValue,
            attrs,
            resourceAttrs: attrsToObject(rl.resource.attributes),
          };
        }
      }
    }
  }
  throw new Error("api_request fixture not found");
}

test("api_request event maps to the full neurolink metric set with exact labels", () => {
  const rec = loadApiRequestRecord();
  const out = transformEvent(rec);
  assert.equal(out.kind, "request");

  const byName = Object.fromEntries(out.metricOps.map((o) => [o.name, o]));

  const labels = {
    model: rec.attrs.model,
    account: rec.attrs["user.email"],
    status: "200",
    stream: "true",
    mode: "full",
  };
  assert.deepEqual(byName.proxy_requests_total.labels, labels);
  assert.equal(byName.proxy_requests_total.value, 1);

  assert.equal(byName.proxy_request_duration_ms.instrument, "histogram");
  assert.equal(byName.proxy_request_duration_ms.value, rec.attrs.duration_ms);
  assert.deepEqual(byName.proxy_request_duration_ms.labels, labels);

  const tokenLabels = { model: rec.attrs.model, account: rec.attrs["user.email"] };
  assert.equal(byName.proxy_tokens_input.value, rec.attrs.input_tokens);
  assert.equal(byName.proxy_tokens_output.value, rec.attrs.output_tokens);
  assert.equal(byName.proxy_tokens_cache_read.value, rec.attrs.cache_read_tokens);
  assert.equal(byName.proxy_tokens_cache_creation.value, rec.attrs.cache_creation_tokens);
  for (const n of [
    "proxy_tokens_input",
    "proxy_tokens_output",
    "proxy_tokens_cache_read",
    "proxy_tokens_cache_creation",
  ]) {
    assert.deepEqual(byName[n].labels, tokenLabels, n);
  }
  assert.equal(byName.proxy_cost_usd_total.value, rec.attrs.cost_usd);
  assert.equal(out.metricOps.find((o) => o.name === "proxy_errors_total"), undefined);
});

test("span mirrors neurolink proxy.request attribute names", () => {
  const rec = loadApiRequestRecord();
  const out = transformEvent(rec);
  const span = out.span;
  const attrs = Object.fromEntries(span.attributes);

  assert.equal(span.name, "proxy.request");
  assert.equal(span.kind, 1);
  assert.equal(attrs["proxy.request_id"], rec.attrs.client_request_id);
  assert.equal(attrs["http.method"], "POST");
  assert.equal(attrs["http.target"], "/v1/messages");
  assert.equal(attrs["gen_ai.request.model"], rec.attrs.model);
  assert.equal(attrs["proxy.stream"], true);
  assert.equal(attrs["proxy.client_app"], "cli");
  assert.equal(attrs["session.id"], rec.attrs["session.id"]);

  // NeuLink gen_ai semantics: standard input includes cache tokens.
  const inTok = rec.attrs.input_tokens;
  const cacheSum = rec.attrs.cache_read_tokens + rec.attrs.cache_creation_tokens;
  assert.equal(attrs["ai.tokens.input"], inTok);
  assert.equal(attrs["ai.tokens.total"], inTok + cacheSum + rec.attrs.output_tokens);
  assert.equal(attrs["gen_ai.usage.input_tokens"], inTok + cacheSum);
  assert.equal(attrs["gen_ai.usage.output_tokens"], rec.attrs.output_tokens);
  assert.equal(attrs["ai.cost.total"], rec.attrs.cost_usd);
  assert.equal(attrs["ai.cost.currency"], "USD");
  assert.equal(attrs["proxy.duration_ms"], rec.attrs.duration_ms);
  assert.equal(attrs["proxy.mode"], "full");
  assert.equal(attrs["proxy.account"], rec.attrs["user.email"]);
  assert.equal(span.status.code, 1);

  const endNs = Number(span.endTimeUnixNano);
  const startNs = Number(span.startTimeUnixNano);
  assert.equal(Math.round((endNs - startNs) / 1e6), rec.attrs.duration_ms);
});

test("log record mirrors neurolink request_final field names", () => {
  const rec = loadApiRequestRecord();
  const out = transformEvent(rec);
  const attrs = Object.fromEntries(out.logRecord.attributes);

  assert.equal(attrs["proxy.record_kind"], "request_final");
  assert.equal(attrs["request.id"], rec.attrs.client_request_id);
  assert.equal(attrs["http.method"], "POST");
  assert.equal(attrs["http.path"], "/v1/messages");
  assert.equal(attrs["http.status_code"], 200);
  assert.equal(attrs["response.time_ms"], rec.attrs.duration_ms);
  assert.equal(attrs["ai.model"], rec.attrs.model);
  assert.equal(attrs["account.name"], rec.attrs["user.email"]);
  assert.equal(attrs["account.type"], "claude_code");
  assert.equal(attrs["ai.input_tokens"], rec.attrs.input_tokens);
  assert.equal(attrs["ai.cache_read_tokens"], rec.attrs.cache_read_tokens);
  assert.equal(attrs["ai.cost.api_equivalent_usd"], rec.attrs.cost_usd);
  assert.equal(attrs["is_success"], true);
  assert.equal(attrs["is_error"], false);
  assert.equal(attrs["source"], "otlp");
  assert.equal(attrs["trace.id"], out.span.traceId);
  assert.equal(attrs["span.id"], out.span.spanId);
  assert.equal(out.logRecord.severityText, "INFO");
});

test("transform is deterministic per request id (stable trace ids)", () => {
  const rec = loadApiRequestRecord();
  const a = transformEvent(rec);
  const b = transformEvent(rec);
  assert.equal(a.span.traceId, b.span.traceId);
  assert.equal(a.span.spanId, b.span.spanId);
  assert.match(a.span.traceId, /^[0-9a-f]{32}$/);
  assert.match(a.span.spanId, /^[0-9a-f]{16}$/);
});

test("retry event produces only proxy_retries_total", () => {
  const out = transformEvent({
    attrs: {
      "event.name": "api_request_overload_repeated",
      model: "claude-sonnet-5",
      "user.email": "dev00000@company.example",
    },
    timeUnixNano: "1791836583697000000",
  });
  assert.equal(out.kind, "retry");
  assert.equal(out.metricOps.length, 1);
  assert.equal(out.metricOps[0].name, "proxy_retries_total");
  assert.deepEqual(out.metricOps[0].labels, {
    model: "claude-sonnet-5",
    account: "dev00000@company.example",
    reason: "overload_repeated",
  });
  assert.equal(out.logRecord, undefined);
});

test("error event produces requests_total, errors_total, error span and log", () => {
  const out = transformEvent({
    attrs: {
      "event.name": "api_request_non_retryable",
      model: "claude-opus-4",
      "user.email": "dev00000@company.example",
      status_code: 500,
      error_message: "boom",
    },
    timeUnixNano: "1791836583697000000",
  });
  assert.equal(out.kind, "error");
  const byName = Object.fromEntries(out.metricOps.map((o) => [o.name, o]));
  assert.equal(byName.proxy_requests_total.labels.status, "500");
  assert.equal(byName.proxy_errors_total.labels.error_type, "server");
  assert.equal(byName.proxy_errors_total.labels.status, "500");
  assert.equal(byName.proxy_tokens_input, undefined);

  const spanAttrs = Object.fromEntries(out.span.attributes);
  assert.equal(spanAttrs["error.type"], "server");
  assert.equal(spanAttrs["error.message"], "boom");
  assert.equal(out.span.status.code, 2);
  const logAttrs = Object.fromEntries(out.logRecord.attributes);
  assert.equal(logAttrs["is_error"], true);
  assert.equal(out.logRecord.severityText, "ERROR");
});

test("missing email falls back to unknown account like neurolink", () => {
  const out = transformEvent({
    attrs: { "event.name": "api_request", model: "m" },
    timeUnixNano: "1",
  });
  const byName = Object.fromEntries(out.metricOps.map((o) => [o.name, o]));
  assert.equal(byName.proxy_requests_total.labels.account, "unknown");
});

test("metrics fallback: delta token.usage datapoints become counter increments", () => {
  const d = JSON.parse(fs.readFileSync("test/fixtures/004-metrics.json", "utf8"));
  const { ops, passthrough } = transformMetricsFallback(d);
  assert.equal(passthrough.length, 0);
  const byLabels = new Map(ops.map((o) => [`${o.name}|${o.labels.account}`, o]));
  assert.equal(byLabels.get("proxy_tokens_input|dev00000@company.example").value, 2);
  assert.equal(byLabels.get("proxy_tokens_output|dev00000@company.example").value, 4);
  assert.equal(byLabels.get("proxy_tokens_cache_read|dev00000@company.example").value, 23994);
  assert.equal(byLabels.get("proxy_tokens_cache_creation|dev00000@company.example").value, 11458);
  assert.equal(byLabels.get("proxy_cost_usd_total|dev00000@company.example").value, 0.0506748);
});
