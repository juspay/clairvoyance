// In-process aggregation state for the proxy_* metrics, mirroring how
// NeuroLink's MeterProvider behaved: incremental adds at request time,
// cumulative monotonic exports every flush interval, JS SDK default
// explicit-bucket histogram boundaries.

// OpenTelemetry JS SDK default explicit bucket boundaries.
export const DEFAULT_BOUNDS = [0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000, 7500, 10000];

// Verbatim from NeuroLink's proxyTracer.ts instrument definitions.
export const METRIC_DEFS = {
  proxy_requests_total: {
    instrument: "counter",
    description: "Total number of proxy requests",
    unit: "{request}",
  },
  proxy_request_duration_ms: {
    instrument: "histogram",
    description: "Proxy request duration in milliseconds",
    unit: "ms",
  },
  proxy_tokens_input: {
    instrument: "counter",
    description: "Total input tokens consumed via proxy",
    unit: "{token}",
  },
  proxy_tokens_output: {
    instrument: "counter",
    description: "Total output tokens produced via proxy",
    unit: "{token}",
  },
  proxy_tokens_cache_read: {
    instrument: "counter",
    description: "Total cache-read tokens via proxy",
    unit: "{token}",
  },
  proxy_tokens_cache_creation: {
    instrument: "counter",
    description: "Total cache-creation tokens via proxy",
    unit: "{token}",
  },
  proxy_tokens_reasoning: {
    instrument: "counter",
    description: "Total reasoning tokens via proxy",
    unit: "{token}",
  },
  proxy_cost_usd_total: {
    instrument: "counter",
    description: "Total estimated cost in USD",
    unit: "USD",
  },
  proxy_errors_total: {
    instrument: "counter",
    description: "Total proxy errors",
    unit: "{error}",
  },
  proxy_retries_total: {
    instrument: "counter",
    description: "Total upstream retry attempts",
    unit: "{retry}",
  },
  proxy_model_substitution_total: {
    instrument: "counter",
    description:
      "Total proxy requests where the response model differs from the requested model",
    unit: "{substitution}",
  },
  proxy_request_body_bytes: {
    instrument: "histogram",
    description: "Request body size in bytes sent upstream",
    unit: "By",
  },
  proxy_response_body_bytes: {
    instrument: "histogram",
    description: "Response body size in bytes received from upstream",
    unit: "By",
  },
  proxy_fallback_attempts_total: {
    instrument: "counter",
    description: "Total fallback provider attempts",
    unit: "{attempt}",
  },
  proxy_fallback_success_total: {
    instrument: "counter",
    description: "Total successful fallback provider responses",
    unit: "{success}",
  },
  proxy_fallback_failure_total: {
    instrument: "counter",
    description: "Total failed fallback provider responses",
    unit: "{failure}",
  },
};

const seriesKey = (name, labels) =>
  name + "|" + Object.keys(labels).sort().map((k) => `${k}=${labels[k]}`).join(",");

export class Aggregator {
  constructor({ maxSeries = 20000 } = {}) {
    this.maxSeries = maxSeries;
    this.counters = new Map(); // key -> { name, labels, value, startNs }
    this.histograms = new Map(); // key -> { name, labels, count, sum, buckets, bounds, min, max, startNs }
    this.cumulativePassthrough = new Map(); // TOKEN_SOURCE=metric fallback
    this.droppedSeries = 0;
    this.acceptedOps = 0;
  }

  #startNs() {
    return String(Date.now() * 1e6);
  }

  apply(op) {
    const def = METRIC_DEFS[op.name];
    if (!def) return; // unknown metric: ignore
    if (def.instrument === "counter" && op.instrument === "counter") {
      const key = seriesKey(op.name, op.labels);
      let s = this.counters.get(key);
      if (!s) {
        if (this.counters.size + this.histograms.size >= this.maxSeries) {
          this.droppedSeries++;
          return;
        }
        s = { name: op.name, labels: op.labels, value: 0, startNs: this.#startNs() };
        this.counters.set(key, s);
      }
      s.value += op.value;
      this.acceptedOps++;
    } else if (def.instrument === "histogram" && op.instrument === "histogram") {
      const key = seriesKey(op.name, op.labels);
      let s = this.histograms.get(key);
      if (!s) {
        if (this.counters.size + this.histograms.size >= this.maxSeries) {
          this.droppedSeries++;
          return;
        }
        s = {
          name: op.name,
          labels: op.labels,
          count: 0,
          sum: 0,
          buckets: new Array(DEFAULT_BOUNDS.length + 1).fill(0),
          bounds: DEFAULT_BOUNDS,
          min: Infinity,
          max: -Infinity,
          startNs: this.#startNs(),
        };
        this.histograms.set(key, s);
      }
      const v = op.value;
      s.count += 1;
      s.sum += v;
      if (v < s.min) s.min = v;
      if (v > s.max) s.max = v;
      let placed = false;
      for (let i = 0; i < s.bounds.length; i++) {
        if (v <= s.bounds[i]) {
          s.buckets[i]++;
          placed = true;
          break;
        }
      }
      if (!placed) s.buckets[s.bounds.length]++;
      this.acceptedOps++;
    }
  }

  /** TOKEN_SOURCE=metric fallback: cumulative value passthrough. */
  applyCumulative(op) {
    const key = seriesKey(op.name, op.labels);
    this.cumulativePassthrough.set(key, {
      name: op.name,
      labels: op.labels,
      value: op.value,
      timeUnixNano: op.timeUnixNano,
      startTimeUnixNano: op.startTimeUnixNano ?? this.#startNs(),
    });
  }

  /** OTLP metric objects for the current cumulative state. */
  snapshot(nowNs = Date.now() * 1e6) {
    const byName = new Map();
    const now = String(Math.round(nowNs));
    for (const s of this.counters.values()) {
      const dp = {
        attributes: objToPairs(s.labels),
        startTimeUnixNano: s.startNs,
        timeUnixNano: now,
        asDouble: s.value,
      };
      pushDp(byName, s.name, { kind: "sum", dp });
    }
    for (const s of this.histograms.values()) {
      const dp = {
        attributes: objToPairs(s.labels),
        startTimeUnixNano: s.startNs,
        timeUnixNano: now,
        count: s.count,
        sum: s.sum,
        bucketCounts: s.buckets,
        explicitBounds: s.bounds,
        min: s.min === Infinity ? undefined : s.min,
        max: s.max === -Infinity ? undefined : s.max,
      };
      pushDp(byName, s.name, { kind: "histogram", dp });
    }
    for (const s of this.cumulativePassthrough.values()) {
      const dp = {
        attributes: objToPairs(s.labels),
        startTimeUnixNano: s.startTimeUnixNano ?? now,
        timeUnixNano: s.timeUnixNano ?? now,
        asDouble: s.value,
      };
      pushDp(byName, s.name, { kind: "sum", dp });
    }
    return byName;
  }
}

function pushDp(byName, name, { kind, dp }) {
  let entry = byName.get(name);
  if (!entry) {
    const def = METRIC_DEFS[name];
    entry = { description: def.description, unit: def.unit, kind, dataPoints: [] };
    byName.set(name, entry);
  }
  entry.dataPoints.push(dp);
}

function objToPairs(obj) {
  return Object.entries(obj).map(([key, value]) => ({ key, value: { stringValue: String(value) } }));
}
