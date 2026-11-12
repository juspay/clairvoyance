import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { UsageStore, extractWindows } from "../src/usage.mjs";
import { transformEvent } from "../src/transform.mjs";
import { attrsToObject } from "../src/decode.mjs";
import { createServer } from "../src/server.mjs";
import { loadConfig } from "../src/config.mjs";
import http from "node:http";

const LIMITS = [
  { kind: "session", group: "session", percent: 42.5, resets_at: "2026-10-14T18:00:00Z", severity: "normal", is_active: true },
  { kind: "weekly_all", group: "weekly", percent: 67.2, resets_at: "2026-10-19T09:00:00Z", severity: "normal" },
];

test("extractUsage handles the real 2.1.29x statusline shape", () => {
  const u = new UsageStore();
  const rl = {
    five_hour: { used_percentage: 11, resets_at: 1791973800 },
    seven_day: { used_percentage: 33, resets_at: 1792368000 },
  };
  assert.equal(u.record({ session_id: "sx", rate_limits: rl }), false); // parked
  u.associateSession("sx", "dev@x.io");
  const snap = u.forAccount("dev@x.io");
  assert.equal(snap.fiveHour, 0.11);
  assert.equal(snap.sevenDay, 0.33);
  assert.equal(snap.reset5h, new Date(1791973800 * 1000).toISOString());
  // null/absent rate_limits ignored
  assert.equal(u.record({ session_id: "s9", rate_limits: null }), false);
});

test("extractUsage handles both shapes via export", async () => {
  const { extractUsage } = await import("../src/usage.mjs");
  const a = extractUsage({ five_hour: { used_percentage: 11, resets_at: 1791973800 }, seven_day: { used_percentage: 33, resets_at: 1792368000 } });
  assert.equal(a.fiveHour, 0.11);
  assert.equal(a.sevenDay, 0.33);
  const b = extractUsage({ limits: [{ kind: "session", percent: 42.5, resets_at: "2026-10-14T18:00:00Z" }] });
  assert.equal(b.fiveHour, 0.425);
  assert.equal(extractUsage(null), null);
  assert.equal(extractUsage({}), null);
});

test("extractUsage: each window independently optional (Nov 2026 schema — seven_day-only payloads are normal)", async () => {
  const { extractUsage } = await import("../src/usage.mjs");
  // captured live 2026-11-12: five_hour window dropped, only seven_day present
  const only7d = extractUsage({ seven_day: { used_percentage: 13, resets_at: 1794787200 } });
  assert.equal(only7d.fiveHour, undefined);
  assert.equal(only7d.sevenDay, 0.13);
  assert.equal(only7d.reset5h, null);
  assert.equal(only7d.reset7d, new Date(1794787200 * 1000).toISOString());
  // five_hour alone also valid
  const only5h = extractUsage({ five_hour: { used_percentage: 58, resets_at: 1794480000 } });
  assert.equal(only5h.fiveHour, 0.58);
  assert.equal(only5h.sevenDay, undefined);
  // an empty rate_limits object still yields nothing
  assert.equal(extractUsage({}), null);
});

test("seven_day-only payload promotes and stamps (usage pipeline works without five_hour)", async () => {
  const u = new UsageStore();
  const rl = { seven_day: { used_percentage: 13, resets_at: 1794787200 } };
  assert.equal(u.record({ session_id: "s7", rate_limits: rl }), false); // parked
  u.associateSession("s7", "dev7@x.io");
  const snap = u.forAccount("dev7@x.io");
  assert.equal(snap.sevenDay, 0.13);
  assert.equal(snap.fiveHour, undefined);
  // span stamping omits the 5h attrs but carries 7d
  const out = transformEvent({
    attrs: { "event.name": "api_request", model: "m", "user.email": "dev7@x.io", "session.id": "s7", input_tokens: 1, output_tokens: 1 },
    timeUnixNano: "1794480000000000000",
    usage: u.forAccount("dev7@x.io"),
  });
  const spanAttrs = Object.fromEntries(out.span.attributes);
  assert.equal(spanAttrs["proxy.ratelimit.after.7d"], 0.13);
  assert.equal("proxy.ratelimit.after.5h" in spanAttrs, false);
});

test("extractWindows maps server rows to neurolink window semantics", () => {
  const w = extractWindows(LIMITS);
  assert.equal(w.fiveHour, 0.425);
  assert.equal(w.sevenDay, 0.672);
  assert.equal(w.reset5h, "2026-10-14T18:00:00Z");
  assert.equal(w.reset7d, "2026-10-19T09:00:00Z");
});

test("weekly_all wins over higher weekly_scoped rows", () => {
  const w = extractWindows([
    { kind: "weekly_scoped", percent: 90, resets_at: null },
    { kind: "weekly_all", percent: 50, resets_at: "r1" },
  ]);
  assert.equal(w.sevenDay, 0.5);
});

test("usage records resolve account via session index and park when unknown", () => {
  const u = new UsageStore();
  // parked before the account is known
  assert.equal(u.record({ session_id: "s1", rate_limits: { limits: LIMITS } }), false);
  assert.equal(u.byAccount.size, 0);
  // api_request event teaches the session -> account mapping, promoting parked data
  u.associateSession("s1", "dev@company.example");
  assert.equal(u.byAccount.size, 1);
  const snap = u.forAccount("dev@company.example");
  assert.equal(snap.fiveHour, 0.425);
  assert.equal(snap.sevenDay, 0.672);
  // direct account works too
  u.record({ account: "other@company.example", rate_limits: { limits: LIMITS } });
  assert.equal(u.forAccount("other@company.example").sevenDay, 0.672);
  // empty / null limits are ignored
  assert.equal(u.record({ session_id: "s2", rate_limits: { limits: [] } }), false);
  assert.equal(u.record({ session_id: "s2", rate_limits: null }), false);
});

test("spans get proxy.ratelimit.* stamped when usage is known", () => {
  const d = JSON.parse(fs.readFileSync("test/fixtures/002-logs.json", "utf8"));
  let rec;
  for (const rl of d.resourceLogs)
    for (const sl of rl.scopeLogs)
      for (const lr of sl.logRecords) {
        const a = attrsToObject(lr.attributes);
        if (a["event.name"] === "api_request")
          rec = { timeUnixNano: lr.timeUnixNano, body: lr.body?.stringValue, attrs: a };
      }
  const out = transformEvent({
    ...rec,
    usage: { fiveHour: 0.425, sevenDay: 0.672, reset5h: "2026-10-14T18:00:00Z", reset7d: null },
  });
  const attrs = Object.fromEntries(out.span.attributes);
  assert.equal(attrs["proxy.ratelimit.after.5h"], 0.425);
  assert.equal(attrs["proxy.ratelimit.after.7d"], 0.672);
  assert.equal(attrs["proxy.ratelimit.reset.5h"], "2026-10-14T18:00:00Z");
  assert.equal(attrs["proxy.ratelimit.reset.7d"], undefined);

  // without usage: no ratelimit attrs (absence, like other unmapped fields)
  const bare = transformEvent(rec);
  const bareAttrs = Object.fromEntries(bare.span.attributes);
  assert.equal(bareAttrs["proxy.ratelimit.after.5h"], undefined);
});

test("e2e: /v1/usage before telemetry produces stamped spans at the sink", async (t) => {
  const received = { logs: [], traces: [], metrics: [] };
  const sink = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const isUsage = req.url.includes("/v1/usage");
      if (isUsage) {
        res.writeHead(200);
        res.end('{"ok":true}');
        return;
      }
      const body = Buffer.concat(chunks);
      received[req.url.includes("traces") ? "traces" : req.url.includes("logs") ? "logs" : "metrics"].push(body);
      res.writeHead(200);
      res.end("");
    });
  });
  await new Promise((r) => sink.listen(0, "127.0.0.1", r));
  t.after(() => {
    sink.closeAllConnections?.();
    sink.close();
  });

  const cfg = loadConfig({
    PORT: "0",
    HOST: "127.0.0.1",
    OTEL_EXPORT_ENDPOINT: `http://127.0.0.1:${sink.address().port}`,
    SIDECAR_INGEST_TOKEN: "t0k",
  });
  const server = createServer(cfg, { autostart: false });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    await server.__exporter.shutdown();
    server.closeAllConnections?.();
    server.close();
  });
  const port = server.address().port;

  // 1. telemetry first (teaches session -> account)
  const logsJson = fs.readFileSync("test/fixtures/002-logs.json");
  const r1 = await fetch(`http://127.0.0.1:${port}/v1/logs`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "t0k" },
    body: logsJson,
  });
  assert.equal(r1.status, 200);

  // 2. statusline usage post for the same session
  const d = JSON.parse(logsJson.toString());
  let sessionId;
  for (const rl of d.resourceLogs)
    for (const sl of rl.scopeLogs)
      for (const lr of sl.logRecords) {
        const a = attrsToObject(lr.attributes);
        if (a["event.name"] === "api_request") sessionId = a["session.id"];
      }
  const r2 = await fetch(`http://127.0.0.1:${port}/v1/usage`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "t0k" },
    body: JSON.stringify({ session_id: sessionId, rate_limits: { limits: LIMITS } }),
  });
  assert.equal(r2.status, 200);
  assert.equal((await r2.json()).applied, true);

  // auth enforced
  const denied = await fetch(`http://127.0.0.1:${port}/v1/usage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  assert.equal(denied.status, 401);

  // 3. another api_request -> its span carries ratelimit attrs
  await fetch(`http://127.0.0.1:${port}/v1/logs`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "t0k" },
    body: logsJson,
  });
  await server.__exporter.flushLogsAndSpans();

  const { decodeProtobufTracesRequest } = await import("../src/protobuf.mjs");
  const spans = received.traces
    .flatMap((buf) => decodeProtobufTracesRequest(buf).resourceSpans)
    .flatMap((rs) => rs.scopeSpans)
    .flatMap((ss) => ss.spans);
  assert.ok(spans.length >= 2);
  const stamped = spans.filter((s) =>
    s.attributes.some((a) => a.key === "proxy.ratelimit.after.5h"),
  );
  assert.ok(stamped.length >= 1, "at least one span stamped with usage");
  const attr = stamped[0].attributes.find((a) => a.key === "proxy.ratelimit.after.5h");
  assert.equal(attr.value.doubleValue, 0.425);
});
