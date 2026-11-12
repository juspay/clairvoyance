// Minimal protobuf wire-format reader plus OTLP message walkers for the three
// Export*ServiceRequest messages. Only the fields the transformer needs are
// decoded; unknown fields are skipped per protobuf rules. Zero dependencies.
//
// Wire representation used internally:
//   wire 0 (varint)  -> BigInt
//   wire 1 (64-bit)  -> BigInt holding the raw bits (reinterpret for double/sfixed)
//   wire 2 (bytes)   -> Buffer
//   wire 5 (32-bit)  -> Number
//
// Output shape deliberately matches the OTLP/JSON encoding (nanos as decimal
// strings, doubles as numbers) so JSON and protobuf inputs are
// indistinguishable downstream.

function readVarint(buf, pos) {
  let value = 0n;
  let shift = 0n;
  for (;;) {
    if (pos >= buf.length) throw new Error("truncated varint");
    const b = buf[pos++];
    value |= BigInt(b & 0x7f) << shift;
    if ((b & 0x80) === 0) return { value, pos };
    shift += 7n;
    if (shift > 63n) throw new Error("varint too long");
  }
}

function doubleFromBits(bits) {
  const tmp = Buffer.alloc(8);
  tmp.writeBigUInt64LE(bits);
  return tmp.readDoubleLE();
}

/** Parse one protobuf message into {field, wire, value} entries. */
function parseFields(buf) {
  const fields = [];
  let pos = 0;
  while (pos < buf.length) {
    const key = readVarint(buf, pos);
    pos = key.pos;
    const field = Number(key.value >> 3n);
    const wire = Number(key.value & 7n);
    let value;
    if (wire === 0) {
      const r = readVarint(buf, pos);
      pos = r.pos;
      value = r.value;
    } else if (wire === 1) {
      if (pos + 8 > buf.length) throw new Error("truncated fixed64");
      value = buf.readBigUInt64LE(pos);
      pos += 8;
    } else if (wire === 2) {
      const len = readVarint(buf, pos);
      pos = len.pos;
      const end = pos + Number(len.value);
      if (end > buf.length) throw new Error("truncated length-delimited field");
      value = buf.subarray(pos, end);
      pos = end;
    } else if (wire === 5) {
      if (pos + 4 > buf.length) throw new Error("truncated fixed32");
      value = buf.readUInt32LE(pos);
      pos += 4;
    } else {
      throw new Error(`unsupported wire type ${wire}`);
    }
    fields.push({ field, wire, value });
  }
  return fields;
}

const asStr = (v) => (Buffer.isBuffer(v) ? v.toString("utf8") : undefined);

/** Repeated numeric field: accept packed (length-delimited) and unpacked forms. */
function packedOrSingle(entry, readOne) {
  if (entry.wire === 2) {
    const buf = entry.value;
    const out = [];
    let pos = 0;
    while (pos < buf.length) {
      const r = readOne(buf, pos);
      pos = r.pos;
      out.push(r.value);
    }
    return out;
  }
  return [readOne(entry.value, 0).value];
}

const readDouble = (buf, pos) => ({ value: buf.readDoubleLE(pos), pos: pos + 8 });
const readFixed64 = (buf, pos) => ({ value: buf.readBigUInt64LE(pos), pos: pos + 8 });

/** Repeated fixed64: packed (wire 2) or unpacked (wire 1, bits already in value). */
function fixed64List(entry) {
  if (entry.wire === 1) return [Number(entry.value)];
  return packedOrSingle(entry, readFixed64).map(Number);
}

/** Repeated double: packed (wire 2) or unpacked (wire 1, reinterpret bits). */
function doubleList(entry) {
  if (entry.wire === 1) return [doubleFromBits(entry.value)];
  return packedOrSingle(entry, readDouble);
}

// --- common OTLP submessages --------------------------------------------------

function parseAnyValue(buf) {
  for (const f of parseFields(buf)) {
    if (f.field === 1) return { stringValue: asStr(f.value) ?? "" };
    if (f.field === 2) return { boolValue: f.value === 1n };
    if (f.field === 3) return { intValue: Number(BigInt.asIntN(64, f.value)) };
    if (f.field === 4 && f.wire === 1) return { doubleValue: doubleFromBits(f.value) };
    if (f.field === 5 || f.field === 6 || f.field === 7) return { stringValue: "[composite]" };
  }
  return null;
}

function parseKeyValue(kvBuf) {
  let key = "";
  let value = null;
  for (const f of parseFields(kvBuf)) {
    if (f.field === 1) key = asStr(f.value) ?? "";
    else if (f.field === 2 && Buffer.isBuffer(f.value)) value = parseAnyValue(f.value);
  }
  return { key, value };
}

function collectAttributes(fields, attrFieldNo) {
  const attributes = [];
  for (const f of fields) {
    if (f.field === attrFieldNo && Buffer.isBuffer(f.value)) {
      attributes.push(parseKeyValue(f.value));
    }
  }
  return attributes;
}

function parseResource(buf) {
  return { attributes: collectAttributes(parseFields(buf), 1) };
}

function parseScope(buf) {
  const scope = { name: "", version: "" };
  for (const f of parseFields(buf)) {
    if (f.field === 1) scope.name = asStr(f.value) ?? "";
    else if (f.field === 2) scope.version = asStr(f.value) ?? "";
  }
  return scope;
}

// --- metrics -------------------------------------------------------------------

function parseNumberDataPoint(buf) {
  const fields = parseFields(buf);
  const dp = { attributes: [] };
  for (const f of fields) {
    if (f.field === 7 && Buffer.isBuffer(f.value)) dp.attributes = collectAttributes(fields, 7);
    else if (f.field === 2 && f.wire === 1) dp.startTimeUnixNano = f.value.toString();
    else if (f.field === 3 && f.wire === 1) dp.timeUnixNano = f.value.toString();
    else if (f.field === 4 && f.wire === 1) dp.asDouble = doubleFromBits(f.value);
    else if (f.field === 6 && f.wire === 1) dp.asInt = Number(BigInt.asIntN(64, f.value));
  }
  return dp;
}

function parseHistogramDataPoint(buf) {
  const fields = parseFields(buf);
  const dp = { attributes: [], bucketCounts: [], explicitBounds: [] };
  for (const f of fields) {
    if (f.field === 9 && Buffer.isBuffer(f.value)) dp.attributes = collectAttributes(fields, 9);
    else if (f.field === 2 && f.wire === 1) dp.startTimeUnixNano = f.value.toString();
    else if (f.field === 3 && f.wire === 1) dp.timeUnixNano = f.value.toString();
    else if (f.field === 4 && f.wire === 1) dp.count = Number(f.value);
    else if (f.field === 5 && f.wire === 1) dp.sum = doubleFromBits(f.value);
    else if (f.field === 6) dp.bucketCounts.push(...fixed64List(f));
    else if (f.field === 7) dp.explicitBounds.push(...doubleList(f));
    else if (f.field === 11 && f.wire === 1) dp.min = doubleFromBits(f.value);
    else if (f.field === 12 && f.wire === 1) dp.max = doubleFromBits(f.value);
  }
  return dp;
}

function parseMetric(buf) {
  const fields = parseFields(buf);
  const metric = {};
  for (const f of fields) {
    if (f.field === 1) metric.name = asStr(f.value) ?? "";
    else if (f.field === 2) metric.description = asStr(f.value) ?? "";
    else if (f.field === 3) metric.unit = asStr(f.value) ?? "";
    else if (f.field === 5 && Buffer.isBuffer(f.value)) {
      metric.gauge = { dataPoints: [] };
      for (const g of parseFields(f.value)) {
        if (g.field === 1 && Buffer.isBuffer(g.value)) metric.gauge.dataPoints.push(parseNumberDataPoint(g.value));
      }
    } else if (f.field === 7 && Buffer.isBuffer(f.value)) {
      const sum = { dataPoints: [], aggregationTemporality: 0, isMonotonic: false };
      for (const g of parseFields(f.value)) {
        if (g.field === 1 && Buffer.isBuffer(g.value)) sum.dataPoints.push(parseNumberDataPoint(g.value));
        else if (g.field === 2) sum.aggregationTemporality = Number(g.value);
        else if (g.field === 3) sum.isMonotonic = g.value === 1n;
      }
      metric.sum = sum;
    } else if (f.field === 9 && Buffer.isBuffer(f.value)) {
      const h = { dataPoints: [], aggregationTemporality: 0 };
      for (const g of parseFields(f.value)) {
        if (g.field === 1 && Buffer.isBuffer(g.value)) h.dataPoints.push(parseHistogramDataPoint(g.value));
        else if (g.field === 2) h.aggregationTemporality = Number(g.value);
      }
      metric.histogram = h;
    }
  }
  return metric;
}

export function decodeProtobufMetricsRequest(buf) {
  const resourceMetrics = [];
  for (const top of parseFields(buf)) {
    if (top.field !== 1 || !Buffer.isBuffer(top.value)) continue;
    const rm = { resource: { attributes: [] }, scopeMetrics: [] };
    for (const f of parseFields(top.value)) {
      if (f.field === 1 && Buffer.isBuffer(f.value)) rm.resource = parseResource(f.value);
      else if (f.field === 2 && Buffer.isBuffer(f.value)) {
        const sm = { scope: { name: "", version: "" }, metrics: [] };
        for (const g of parseFields(f.value)) {
          if (g.field === 1 && Buffer.isBuffer(g.value)) sm.scope = parseScope(g.value);
          else if (g.field === 2 && Buffer.isBuffer(g.value)) sm.metrics.push(parseMetric(g.value));
        }
        rm.scopeMetrics.push(sm);
      }
    }
    resourceMetrics.push(rm);
  }
  return { resourceMetrics };
}

// --- logs ----------------------------------------------------------------------

function parseLogRecord(buf) {
  const fields = parseFields(buf);
  const rec = { attributes: [] };
  for (const f of fields) {
    if (f.field === 1 && f.wire === 1) rec.timeUnixNano = f.value.toString();
    else if (f.field === 11 && f.wire === 1) rec.observedTimeUnixNano = f.value.toString();
    else if (f.field === 2) rec.severityNumber = Number(f.value);
    else if (f.field === 3) rec.severityText = asStr(f.value) ?? "";
    else if (f.field === 5 && Buffer.isBuffer(f.value)) rec.body = parseAnyValue(f.value);
    else if (f.field === 6 && Buffer.isBuffer(f.value)) rec.attributes = collectAttributes(fields, 6);
    else if (f.field === 7) rec.droppedAttributesCount = Number(f.value);
    else if (f.field === 9) rec.traceId = f.value.toString("hex");
    else if (f.field === 10) rec.spanId = f.value.toString("hex");
  }
  return rec;
}

export function decodeProtobufLogsRequest(buf) {
  const resourceLogs = [];
  for (const top of parseFields(buf)) {
    if (top.field !== 1 || !Buffer.isBuffer(top.value)) continue;
    const rl = { resource: { attributes: [] }, scopeLogs: [] };
    for (const f of parseFields(top.value)) {
      if (f.field === 1 && Buffer.isBuffer(f.value)) rl.resource = parseResource(f.value);
      else if (f.field === 2 && Buffer.isBuffer(f.value)) {
        const sl = { scope: { name: "", version: "" }, logRecords: [] };
        for (const g of parseFields(f.value)) {
          if (g.field === 1 && Buffer.isBuffer(g.value)) sl.scope = parseScope(g.value);
          else if (g.field === 2 && Buffer.isBuffer(g.value)) sl.logRecords.push(parseLogRecord(g.value));
        }
        rl.scopeLogs.push(sl);
      }
    }
    resourceLogs.push(rl);
  }
  return { resourceLogs };
}

// --- traces ----------------------------------------------------------------------

function parseSpan(buf) {
  const fields = parseFields(buf);
  const span = { attributes: [] };
  for (const f of fields) {
    if (f.field === 1) span.traceId = asHex(f.value);
    else if (f.field === 2) span.spanId = asHex(f.value);
    else if (f.field === 3) span.traceState = asStr(f.value) ?? "";
    else if (f.field === 4) span.parentSpanId = asHex(f.value);
    else if (f.field === 5) span.name = asStr(f.value) ?? "";
    else if (f.field === 6) span.kind = Number(f.value);
    else if (f.field === 7 && f.wire === 1) span.startTimeUnixNano = f.value.toString();
    else if (f.field === 8 && f.wire === 1) span.endTimeUnixNano = f.value.toString();
    else if (f.field === 9 && Buffer.isBuffer(f.value)) span.attributes = collectAttributes(fields, 9);
    else if (f.field === 15 && Buffer.isBuffer(f.value)) {
      span.status = {};
      for (const g of parseFields(f.value)) {
        if (g.field === 2) span.status.message = asStr(g.value) ?? "";
        else if (g.field === 3) span.status.code = Number(g.value);
      }
    }
  }
  return span;
}

const asHex = (v) => (Buffer.isBuffer(v) ? v.toString("hex") : undefined);

export function decodeProtobufTracesRequest(buf) {
  const resourceSpans = [];
  for (const top of parseFields(buf)) {
    if (top.field !== 1 || !Buffer.isBuffer(top.value)) continue;
    const rs = { resource: { attributes: [] }, scopeSpans: [] };
    for (const f of parseFields(top.value)) {
      if (f.field === 1 && Buffer.isBuffer(f.value)) rs.resource = parseResource(f.value);
      else if (f.field === 2 && Buffer.isBuffer(f.value)) {
        const ss = { scope: { name: "", version: "" }, spans: [] };
        for (const g of parseFields(f.value)) {
          if (g.field === 1 && Buffer.isBuffer(g.value)) ss.scope = parseScope(g.value);
          else if (g.field === 2 && Buffer.isBuffer(g.value)) ss.spans.push(parseSpan(g.value));
        }
        rs.scopeSpans.push(ss);
      }
    }
    resourceSpans.push(rs);
  }
  return { resourceSpans };
}

// --- span counting (used for stats on the ingest path) ---------------------------

export function countProtobufSpans(buf) {
  let spans = 0;
  let resourceSpans = 0;
  try {
    for (const top of parseFields(buf)) {
      if (top.field !== 1 || !Buffer.isBuffer(top.value)) continue;
      resourceSpans++;
      for (const f of parseFields(top.value)) {
        if (f.field === 2 && Buffer.isBuffer(f.value)) {
          for (const g of parseFields(f.value)) {
            if (g.field === 2 && Buffer.isBuffer(g.value)) spans++;
          }
        }
      }
    }
  } catch {
    // malformed payload: best-effort count
  }
  return { resourceSpans, spans };
}
