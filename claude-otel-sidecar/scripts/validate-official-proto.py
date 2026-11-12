"""Validate the sidecar protobuf encoder output against the OFFICIAL
opentelemetry-proto descriptors (google.protobuf runtime, not our decoder).

Setup: venv with grpcio-tools; download opentelemetry-proto signal +
collector protos to /tmp/otlp-proto; protoc --python_out=/tmp/otlp-proto-gen.
Run: /tmp/pbenv/bin/python scripts/validate-official-proto.py
"""
import subprocess, sys, json

# 1. produce encoded payloads with the sidecar's own encoder
node_script = """
import { encodeLogsRequest, encodeTracesRequest, encodeMetricsRequest, snapshotEntries, passthroughEntries } from '/Users/narsimha.reddy/Desktop/breeze_repos/claude_code_metrics_sidecar/src/protobuf-encode.mjs';
import { transformEvent } from '/Users/narsimha.reddy/Desktop/breeze_repos/claude_code_metrics_sidecar/src/transform.mjs';
import { Aggregator } from '/Users/narsimha.reddy/Desktop/breeze_repos/claude_code_metrics_sidecar/src/aggregator.mjs';
import { attrsToObject } from '/Users/narsimha.reddy/Desktop/breeze_repos/claude_code_metrics_sidecar/src/decode.mjs';
import fs from 'node:fs';
const CFG = { serviceName: 'neurolink_proxy', serviceVersion: '1.0.0', environment: 'prod' };
const d = JSON.parse(fs.readFileSync('/Users/narsimha.reddy/Desktop/breeze_repos/claude_code_metrics_sidecar/test/fixtures/002-logs.json', 'utf8'));
let rec;
for (const rl of d.resourceLogs) for (const sl of rl.scopeLogs) for (const lr of sl.logRecords) {
  const a = attrsToObject(lr.attributes);
  if (a['event.name'] === 'api_request') rec = { timeUnixNano: lr.timeUnixNano, body: lr.body?.stringValue, attrs: a };
}
const out = transformEvent(rec);
fs.writeFileSync('/tmp/ref-logs.pb', encodeLogsRequest([out.logRecord], CFG));
fs.writeFileSync('/tmp/ref-traces.pb', encodeTracesRequest([out.span], CFG));
const agg = new Aggregator();
for (const op of out.metricOps) agg.apply(op);
fs.writeFileSync('/tmp/ref-metrics.pb', encodeMetricsRequest(snapshotEntries(agg.snapshot(), CFG)));
const m = JSON.parse(fs.readFileSync('/Users/narsimha.reddy/Desktop/breeze_repos/claude_code_metrics_sidecar/test/fixtures/004-metrics.json', 'utf8'));
fs.writeFileSync('/tmp/ref-passthrough.pb', encodeMetricsRequest(passthroughEntries(m.resourceMetrics)));
console.log('encoded');
"""
r = subprocess.run(["node", "--input-type=module", "-e", node_script], capture_output=True, text=True)
if r.returncode != 0:
    print(r.stderr); sys.exit(1)

sys.path.insert(0, "/tmp/otlp-proto-gen")
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2 as logs_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as trace_pb2
from opentelemetry.proto.trace.v1 import trace_pb2 as trace_sig_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2 as metrics_sig_pb2
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2 as metrics_pb2

def attrs_map(pairs):
    return {p.key: (p.value.string_value or p.value.int_value or p.value.double_value or p.value.bool_value if not p.value.string_value else p.value.string_value) for p in pairs}

# logs
lr = logs_pb2.ExportLogsServiceRequest()
lr.ParseFromString(open("/tmp/ref-logs.pb", "rb").read())
rec = lr.resource_logs[0].scope_logs[0].log_records[0]
res = {p.key: p.value.string_value for p in lr.resource_logs[0].resource.attributes}
assert res["service.name"] == "neurolink_proxy", res
assert rec.severity_number == 9, rec.severity_number
assert rec.severity_text == "INFO"
assert rec.body.string_value.startswith("POST /v1/messages")
a = {p.key: (p.value.string_value or p.value.int_value or p.value.double_value or p.value.bool_value) for p in rec.attributes}
assert a["proxy.record_kind"] == "request_final", a
assert a["ai.input_tokens"] == 2
assert abs(a["ai.cost.api_equivalent_usd"] - 0.0506748) < 1e-9, a["ai.cost.api_equivalent_usd"]
assert a["is_success"] in (True, 1)
print("LOGS: official parser OK —", len(rec.attributes), "attrs, severity", rec.severity_number)

# traces
tr = trace_pb2.ExportTraceServiceRequest()
tr.ParseFromString(open("/tmp/ref-traces.pb", "rb").read())
sp = tr.resource_spans[0].scope_spans[0].spans[0]
assert sp.name == "proxy.request", sp.name
assert sp.kind == 1, sp.kind  # INTERNAL
assert sp.status.code == 1  # OK
assert int(time := sp.start_time_unix_nano) > 0
sa = {p.key: (p.value.string_value or p.value.int_value or p.value.double_value or p.value.bool_value) for p in sp.attributes}
assert sa["gen_ai.request.model"] == "claude-sonnet-5", sa
assert abs(sa["ai.cost.total"] - 0.0506748) < 1e-9
assert sa["proxy.account"] == "dev00000@company.example"
print("TRACES: official parser OK — span", sp.name, "trace_id", bytes(sp.trace_id).hex()[:16], "…", len(sp.attributes), "attrs")

# metrics (own snapshot)
mr = metrics_pb2.ExportMetricsServiceRequest()
mr.ParseFromString(open("/tmp/ref-metrics.pb", "rb").read())
names = [m.name for m in mr.resource_metrics[0].scope_metrics[0].metrics]
assert "proxy_requests_total" in names and "proxy_request_duration_ms" in names, names
for m in mr.resource_metrics[0].scope_metrics[0].metrics:
    if m.name == "proxy_requests_total":
        dp = m.sum.data_points[0]
        assert m.sum.aggregation_temporality == 2  # CUMULATIVE
        la = {p.key: p.value.string_value for p in dp.attributes}
        assert la == {"model": "claude-sonnet-5", "account": "dev00000@company.example", "status": "200", "stream": "true", "mode": "full"}, la
        assert dp.as_double == 1.0
    if m.name == "proxy_request_duration_ms":
        h = m.histogram.data_points[0]
        assert h.count == 1 and h.sum > 2000 and len(h.bucket_counts) == 16, (h.count, h.sum, len(h.bucket_counts))
print("METRICS: official parser OK —", names)

# metrics (passthrough)
mp = metrics_pb2.ExportMetricsServiceRequest()
mp.ParseFromString(open("/tmp/ref-passthrough.pb", "rb").read())
res2 = {p.key: p.value.string_value for p in mp.resource_metrics[0].resource.attributes}
assert res2["service.name"] == "claude-code", res2
tok = [m for m in mp.resource_metrics[0].scope_metrics[0].metrics if m.name == "claude_code.token.usage"][0]
types = {p.key: p.value.string_value for p in tok.sum.data_points[0].attributes}
assert types["type"] == "input", types
assert tok.sum.aggregation_temporality == 1  # DELTA
print("PASSTHROUGH: official parser OK — claude_code metrics intact")

print("\nALL PAYLOADS VALIDATE AGAINST OFFICIAL OPENTELEMETRY PROTO DESCRIPTORS")
