// Decode an incoming OTLP HTTP request body (json or protobuf) into the
// normalized OTLP-JSON-shaped object the rest of the pipeline uses.

import { gunzipSync, inflateSync, brotliDecompressSync } from "node:zlib";
import {
  decodeProtobufMetricsRequest,
  decodeProtobufLogsRequest,
  countProtobufSpans,
} from "./protobuf.mjs";

export function decompressBody(buffer, contentEncoding) {
  const enc = (contentEncoding ?? "").toLowerCase();
  if (enc.includes("gzip")) return gunzipSync(buffer);
  if (enc.includes("deflate")) return inflateSync(buffer);
  if (enc.includes("br")) return brotliDecompressSync(buffer);
  return buffer;
}

export function decodeOtlp(body, contentType, signal) {
  const ct = (contentType ?? "").toLowerCase();
  if (ct.includes("json") || (!ct.includes("protobuf") && looksLikeJson(body))) {
    return JSON.parse(body.toString("utf8"));
  }
  if (ct.includes("protobuf") || signal === "traces") {
    if (signal === "metrics") return decodeProtobufMetricsRequest(body);
    if (signal === "logs") return decodeProtobufLogsRequest(body);
    if (signal === "traces") return { __spansOnly: countProtobufSpans(body) };
  }
  throw new Error(`unsupported content-type for ${signal}: ${contentType || "(none)"}`);
}

function looksLikeJson(buffer) {
  for (const b of buffer.subarray(0, 64)) {
    const c = String.fromCharCode(b);
    if (c === "{") return true;
    if (!/\s/.test(c)) return false;
  }
  return false;
}

/** Flatten OTLP attribute pairs into a plain JS object. */
export function attrsToObject(pairs) {
  const out = {};
  for (const p of pairs ?? []) {
    if (!p?.key) continue;
    const v = p.value ?? {};
    out[p.key] =
      v.stringValue ??
      (v.boolValue !== undefined ? v.boolValue : undefined) ??
      v.intValue ??
      v.doubleValue ??
      null;
  }
  return out;
}
