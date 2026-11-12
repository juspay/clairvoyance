// OTLP protobuf ENCODER for the three Export*ServiceRequest messages.
// Mirrors NeuroLink's exporters, which spoke OTLP/http protobuf — the path
// OpenObserve handles most robustly (its JSON logs parser rejects legal
// doubleValue attributes on older builds).
//
// Field numbers follow opentelemetry-proto v1. Round-trips are covered by
// tests against the decoder in protobuf.mjs.

import { METRIC_DEFS } from "./aggregator.mjs";

// --- primitives ---------------------------------------------------------------

class Writer {
  constructor() {
    this.chunks = [];
  }
  get buffer() {
    return Buffer.concat(this.chunks);
  }
  raw(buf) {
    this.chunks.push(buf);
    return this;
  }
  varint(v) {
    let n = BigInt.asUintN(64, BigInt(v));
    for (;;) {
      const b = Number(n & 0x7fn);
      n >>= 7n;
      if (n === 0n) return this.raw(Buffer.from([b]));
      this.raw(Buffer.from([b | 0x80]));
    }
  }
  tag(field, wire) {
    return this.varint((BigInt(field) << 3n) | BigInt(wire));
  }
  str(field, s) {
    const bytes = Buffer.from(String(s), "utf8");
    return this.tag(field, 2).varint(bytes.length).raw(bytes);
  }
  bytes(field, buf) {
    return this.tag(field, 2).varint(buf.length).raw(buf);
  }
  msg(field, fn) {
    const inner = new Writer();
    fn(inner);
    const innerBuf = inner.buffer;
    return this.tag(field, 2).varint(innerBuf.length).raw(innerBuf);
  }
  varintField(field, v) {
    return this.tag(field, 0).varint(v);
  }
  fixed64Field(field, v) {
    const buf = Buffer.alloc(8);
    buf.writeBigUInt64LE(BigInt.asUintN(64, BigInt(v)));
    return this.tag(field, 1).raw(buf);
  }
  doubleField(field, v) {
    const buf = Buffer.alloc(8);
    buf.writeDoubleLE(Number(v));
    return this.tag(field, 1).raw(buf);
  }
  boolField(field, v) {
    return this.tag(field, 0).varint(v ? 1n : 0n);
  }
}

// --- shared submessages ---------------------------------------------------------

/** value: primitive JS value (string|number|boolean) */
function encodeValue(w, value) {
  if (typeof value === "boolean") return w.boolField(2, value);
  if (typeof value === "number") {
    if (Number.isInteger(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER) {
      return w.varintField(3, value); // int_value
    }
    return w.doubleField(4, value); // double_value
  }
  return w.str(1, value ?? ""); // string_value
}

/**
 * Attributes at the message-specific field number. Attribute fields differ per
 * message: Resource=1, NumberDataPoint=7, LogRecord=6, Span=8,
 * HistogramDataPoint=9 — KeyValue itself always uses key=1/value=2.
 * pairs: [key, value] tuples; values may also be OTLP-JSON anyValue maps.
 */
function encodeAttrs(w, fieldNo, pairs) {
  for (const p of pairs ?? []) {
    const key = Array.isArray(p) ? p[0] : p.key;
    let value = Array.isArray(p) ? p[1] : p.value;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      // unwrap {"stringValue"|"intValue"|"doubleValue"|"boolValue": x}
      value = value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue ?? null;
    }
    w.msg(fieldNo, (kv) => {
      kv.str(1, key);
      kv.msg(2, (av) => encodeValue(av, value));
    });
  }
}

/** attrs: OTLP-JSON attribute pairs [{key, value:{...}}] */
function encodeJsonAttrs(w, fieldNo, attrs) {
  encodeAttrs(w, fieldNo, attrs ?? []);
}

function encodeScope(w, name, version) {
  w.str(1, name).str(2, version);
}

// normalized entry: {resourcePairs, scopeName, scopeVersion, metrics}
// metric: {name, description, unit, kind: 'sum'|'gauge'|'histogram',
//          temporality, isMonotonic, dataPoints}
// dataPoint (number): {attrs, startTimeUnixNano, timeUnixNano, asDouble|asInt}
// dataPoint (histogram): {attrs, startTimeUnixNano, timeUnixNano, count, sum,
//                          bucketCounts, explicitBounds, min, max}

function encodeNumberPoints(w, fieldNo, points) {
  for (const dp of points) {
    w.msg(fieldNo, (p) => {
      encodeJsonAttrs(p, 7, dp.attributes ?? dp.attrs);
      if (dp.startTimeUnixNano !== undefined) p.fixed64Field(2, dp.startTimeUnixNano);
      if (dp.timeUnixNano !== undefined) p.fixed64Field(3, dp.timeUnixNano);
      if (dp.asInt !== undefined) p.fixed64Field(6, dp.asInt); // sfixed64
      else p.doubleField(4, dp.asDouble ?? 0);
    });
  }
}

function encodeEntry(w, entry) {
  w.msg(1, (rm) => {
    rm.msg(1, (r) => encodeAttrs(r, 1, entry.resourcePairs));
    for (const sm of entry.scopes) {
      rm.msg(2, (smw) => {
        smw.msg(1, (sc) => encodeScope(sc, sm.name, sm.version));
        for (const m of sm.metrics) {
          smw.msg(2, (mm) => {
            mm.str(1, m.name);
            if (m.description) mm.str(2, m.description);
            if (m.unit) mm.str(3, m.unit);
            if (m.kind === "histogram") {
              mm.msg(9, (h) => {
                h.varintField(2, m.temporality ?? 2);
                for (const dp of m.dataPoints) {
                  h.msg(1, (p) => {
                    encodeJsonAttrs(p, 9, dp.attributes ?? dp.attrs);
                    if (dp.startTimeUnixNano !== undefined) p.fixed64Field(2, dp.startTimeUnixNano);
                    if (dp.timeUnixNano !== undefined) p.fixed64Field(3, dp.timeUnixNano);
                    p.fixed64Field(4, dp.count);
                    p.doubleField(5, dp.sum ?? 0);
                    for (const bc of dp.bucketCounts ?? []) p.fixed64Field(6, bc);
                    for (const b of dp.explicitBounds ?? []) p.doubleField(7, b);
                    if (dp.min !== undefined) p.doubleField(11, dp.min);
                    if (dp.max !== undefined) p.doubleField(12, dp.max);
                  });
                }
              });
            } else {
              // sum (7) or gauge (5)
              const field = m.kind === "gauge" ? 5 : 7;
              mm.msg(field, (s) => {
                if (m.kind !== "gauge") {
                  s.varintField(2, m.temporality ?? 2);
                  s.boolField(3, m.isMonotonic ?? true);
                }
                encodeNumberPoints(s, 1, m.dataPoints);
              });
            }
          });
        }
      });
    }
  });
}

function resourcePairsFromJson(attrs) {
  return (attrs ?? []).map((a) => [a.key, a.value ?? null]);
}

/** Convert decoded (OTLP-JSON-shaped) resourceMetrics into encoder entries. */
export function passthroughEntries(resourceMetrics) {
  const entries = [];
  for (const rm of resourceMetrics ?? []) {
    entries.push({
      resourcePairs: resourcePairsFromJson(rm.resource?.attributes),
      scopes: (rm.scopeMetrics ?? []).map((sm) => ({
        name: sm.scope?.name ?? "",
        version: sm.scope?.version ?? "",
        metrics: (sm.metrics ?? []).map((m) => ({
          name: m.name,
          description: m.description,
          unit: m.unit,
          kind: m.sum ? "sum" : m.gauge ? "gauge" : m.histogram ? "histogram" : "gauge",
          temporality: m.sum?.aggregationTemporality ?? m.histogram?.aggregationTemporality ?? 2,
          isMonotonic: m.sum?.isMonotonic ?? true,
          dataPoints: m.sum?.dataPoints ?? m.gauge?.dataPoints ?? m.histogram?.dataPoints ?? [],
        })),
      })),
    });
  }
  return entries;
}

/** Aggregator snapshot -> encoder entry (NeuroLink scope). */
export function snapshotEntries(snapshot, cfg) {
  const metrics = [...snapshot.entries()].map(([name, m]) => ({
    name,
    description: METRIC_DEFS[name]?.description ?? m.description,
    unit: METRIC_DEFS[name]?.unit ?? m.unit,
    kind: m.kind,
    temporality: 2, // CUMULATIVE
    isMonotonic: true,
    dataPoints: m.dataPoints,
  }));
  if (!metrics.length) return [];
  return [
    {
      resourcePairs: [
        ["service.name", cfg.serviceName],
        ["service.version", cfg.serviceVersion],
        ["deployment.environment", cfg.environment],
        ["telemetry.sdk.name", "opentelemetry"],
        ["telemetry.sdk.language", "node"],
        ["telemetry.sdk.version", cfg.serviceVersion],
      ],
      scopes: [{ name: "neurolink.proxy", version: "1.0.0", metrics }],
    },
  ];
}

/** @returns Buffer ExportMetricsServiceRequest */
export function encodeMetricsRequest(entries) {
  const w = new Writer();
  for (const e of entries) encodeEntry(w, e);
  return w.buffer;
}

// --- logs --------------------------------------------------------------------------

/**
 * @param logRecords transformed records: {timeUnixNano, severityNumber,
 *   severityText, body:{stringValue}, attributes:[[k,v],...]}
 * @returns Buffer ExportLogsServiceRequest
 */
export function encodeLogsRequest(logRecords, cfg) {
  const w = new Writer();
  w.msg(1, (rl) => {
    rl.msg(1, (r) =>
      encodeAttrs(r, 1, [
        ["service.name", cfg.serviceName],
        ["service.version", cfg.serviceVersion],
        ["deployment.environment", cfg.environment],
        ["telemetry.sdk.name", "opentelemetry"],
        ["telemetry.sdk.language", "node"],
        ["telemetry.sdk.version", cfg.serviceVersion],
      ]),
    );
    rl.msg(2, (sl) => {
      sl.msg(1, (sc) => encodeScope(sc, "neurolink-proxy", "1.0.0"));
      for (const rec of logRecords) {
        sl.msg(2, (lr) => {
          lr.fixed64Field(1, rec.timeUnixNano);
          if (rec.severityNumber !== undefined) lr.varintField(2, rec.severityNumber);
          if (rec.severityText) lr.str(3, rec.severityText);
          if (rec.body?.stringValue !== undefined) {
            lr.msg(5, (b) => encodeValue(b, rec.body.stringValue));
          }
          encodeAttrs(lr, 6, rec.attributes);
        });
      }
    });
  });
  return w.buffer;
}

/** Raw forward of other Claude Code events (already OTLP-JSON shaped). */
export function encodeRawLogsRequest(resourceLogs) {
  const w = new Writer();
  for (const rl of resourceLogs ?? []) {
    w.msg(1, (rlw) => {
      encodeJsonAttrs(rlw, 1, rl.resource?.attributes);
      for (const sl of rl.scopeLogs ?? []) {
        rlw.msg(2, (slw) => {
          slw.msg(1, (sc) => encodeScope(sc, sl.scope?.name ?? "", sl.scope?.version ?? ""));
          for (const rec of sl.logRecords ?? []) {
            slw.msg(2, (lr) => {
              if (rec.timeUnixNano !== undefined) lr.fixed64Field(1, rec.timeUnixNano);
              if (rec.severityNumber !== undefined) lr.varintField(2, rec.severityNumber);
              if (rec.severityText) lr.str(3, rec.severityText);
              if (rec.body) {
                lr.msg(5, (b) => {
                  const v = rec.body.stringValue ?? rec.body.intValue ?? rec.body.doubleValue ?? rec.body.boolValue;
                  encodeValue(b, v);
                });
              }
              if (rec.observedTimeUnixNano !== undefined) lr.fixed64Field(11, rec.observedTimeUnixNano);
              if (rec.traceId) lr.bytes(9, Buffer.from(rec.traceId, "hex"));
              if (rec.spanId) lr.bytes(10, Buffer.from(rec.spanId, "hex"));
              encodeJsonAttrs(lr, 6, rec.attributes);
            });
          }
        });
      }
    });
  }
  return w.buffer;
}

// --- traces ------------------------------------------------------------------------

/**
 * @param spans transformed spans: {traceId, spanId, name, kind, startTimeUnixNano,
 *   endTimeUnixNano, attributes:[[k,v],...], status:{code,message}}
 * @returns Buffer ExportTraceServiceRequest
 */
export function encodeTracesRequest(spans, cfg) {
  const w = new Writer();
  w.msg(1, (rs) => {
    rs.msg(1, (r) =>
      encodeAttrs(r, 1, [
        ["service.name", cfg.serviceName],
        ["service.version", cfg.serviceVersion],
        ["deployment.environment", cfg.environment],
        ["telemetry.sdk.name", "opentelemetry"],
        ["telemetry.sdk.language", "node"],
        ["telemetry.sdk.version", cfg.serviceVersion],
      ]),
    );
    rs.msg(2, (ss) => {
      ss.msg(1, (sc) => encodeScope(sc, "neurolink.proxy", "1.0.0"));
      for (const s of spans) {
        ss.msg(2, (sp) => {
          sp.bytes(1, Buffer.from(s.traceId, "hex"));
          sp.bytes(2, Buffer.from(s.spanId, "hex"));
          if (s.parentSpanId) sp.bytes(4, Buffer.from(s.parentSpanId, "hex"));
          sp.str(5, s.name);
          sp.varintField(6, s.kind ?? 1);
          sp.fixed64Field(7, s.startTimeUnixNano);
          sp.fixed64Field(8, s.endTimeUnixNano);
          encodeAttrs(sp, 9, s.attributes);
          if (s.status) {
            sp.msg(15, (st) => {
              if (s.status.message) st.str(2, s.status.message);
              st.varintField(3, s.status.code ?? 1);
            });
          }
        });
      }
    });
  });
  return w.buffer;
}
