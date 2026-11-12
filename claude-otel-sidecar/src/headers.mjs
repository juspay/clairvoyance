// OTEL_EXPORTER_OTLP_HEADERS parsing per the OTEL spec:
// "key1=value1,key2=value2" where keys/values are URL-encoded.
export function readEnvHeaders(raw) {
  const out = {};
  if (!raw) return out;
  for (const pair of raw.split(",")) {
    const eq = pair.indexOf("=");
    if (eq <= 0) continue;
    const k = decodeURIComponent(pair.slice(0, eq).trim());
    const v = decodeURIComponent(pair.slice(eq + 1).trim());
    if (k) out[k] = v;
  }
  return out;
}
