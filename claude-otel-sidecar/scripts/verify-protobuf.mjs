// Validate protobuf decoding against real Claude Code captures.
import fs from "node:fs";
import { decodeProtobufMetricsRequest, decodeProtobufLogsRequest } from "../src/protobuf.mjs";

const m = decodeProtobufMetricsRequest(fs.readFileSync("fixtures/capture-protobuf/004-metrics.bin"));
const names = m.resourceMetrics.flatMap((rm) =>
  rm.scopeMetrics.flatMap((sm) => sm.metrics.map((x) => `${x.name}(${Object.keys(x).filter((k) => k !== "name" && k !== "description" && k !== "unit").join("/")})`)),
);
console.log("METRICS:", JSON.stringify(names, null, 1));
const rm0 = m.resourceMetrics[0];
console.log("resource attrs:", rm0.resource.attributes.map((a) => `${a.key}=${a.value?.stringValue}`).join(" "));
for (const sm of rm0.scopeMetrics) {
  console.log("scope:", sm.scope.name, sm.scope.version);
  for (const metric of sm.metrics) {
    if (metric.name === "claude_code.token.usage") {
      for (const dp of metric.sum.dataPoints) {
        const attrs = Object.fromEntries(dp.attributes.map((a) => [a.key, a.value?.stringValue ?? a.value?.intValue]));
        console.log(`  type=${attrs["type"]} model=${attrs.model} email=${attrs["user.email"]} value=${dp.asDouble} start=${dp.startTimeUnixNano} t=${dp.timeUnixNano} monotone=${metric.sum.isMonotonic} temporal=${metric.sum.aggregationTemporality}`);
      }
    }
    if (metric.name === "claude_code.cost.usage") {
      const dp = metric.sum.dataPoints[0];
      console.log(`  cost=${dp.asDouble} unit=${metric.unit}`);
    }
  }
}

const l = decodeProtobufLogsRequest(fs.readFileSync("fixtures/capture-protobuf/002-logs.bin"));
const logs = l.resourceLogs.flatMap((rl) => rl.scopeLogs.flatMap((sl) => sl.logRecords));
console.log("\nLOG RECORDS:", logs.length);
for (const lr of logs) {
  const attrs = Object.fromEntries(lr.attributes.map((a) => [a.key, a.value?.stringValue ?? a.value?.intValue ?? a.value?.doubleValue]));
  if (attrs["event.name"] === "api_request") {
    console.log("api_request attrs:", JSON.stringify(attrs, null, 1));
    console.log("time:", lr.timeUnixNano, "body:", lr.body?.stringValue);
    break;
  }
}
