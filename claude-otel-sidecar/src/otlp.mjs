// OTLP/JSON request builders for the outgoing NeuroLink-shaped exports.

const kv = (key, value) => {
  if (typeof value === "boolean") return { key, value: { boolValue: value } };
  if (typeof value === "number" && Number.isInteger(value) && Math.abs(value) < 2 ** 31)
    return { key, value: { intValue: value } };
  if (typeof value === "number") return { key, value: { doubleValue: value } };
  return { key, value: { stringValue: String(value) } };
};

export function resourceAttributes(cfg) {
  return [
    kv("service.name", cfg.serviceName),
    kv("service.version", cfg.serviceVersion),
    kv("deployment.environment", cfg.environment),
    { key: "telemetry.sdk.name", value: { stringValue: "opentelemetry" } },
    { key: "telemetry.sdk.language", value: { stringValue: "node" } },
    { key: "telemetry.sdk.version", value: { stringValue: cfg.serviceVersion } },
  ];
}

const scope = (name, version) => ({ scope: { name, version } });

/** aggregator snapshot ({name -> {description, unit, kind, dataPoints}}) -> OTLP metrics request */
export function buildMetricsRequest(snapshot, cfg) {
  const metrics = [...snapshot.entries()].map(([name, m]) => {
    if (m.kind === "sum") {
      return {
        name,
        description: m.description,
        unit: m.unit,
        sum: {
          dataPoints: m.dataPoints,
          aggregationTemporality: 2, // CUMULATIVE
          isMonotonic: true,
        },
      };
    }
    return {
      name,
      description: m.description,
      unit: m.unit,
      histogram: {
        dataPoints: m.dataPoints,
        aggregationTemporality: 2, // CUMULATIVE
      },
    };
  });
  return {
    resourceMetrics: [
      {
        resource: { attributes: resourceAttributes(cfg) },
        scopeMetrics: [{ ...scope("neurolink.proxy", "1.0.0"), metrics }],
      },
    ],
  };
}

/** Wrap raw passthrough resourceMetrics (already OTLP-JSON shaped) unchanged. */
export function buildPassthroughMetricsRequest(resourceMetrics) {
  return { resourceMetrics };
}

export function buildLogsRequest(logRecords, cfg) {
  return {
    resourceLogs: [
      {
        resource: { attributes: resourceAttributes(cfg) },
        scopeLogs: [
          {
            ...scope("neurolink-proxy", "1.0.0"),
            logRecords: logRecords.map((r) => ({
              timeUnixNano: r.timeUnixNano,
              severityNumber: r.severityNumber,
              severityText: r.severityText,
              body: r.body,
              attributes: Array.isArray(r.attributes?.[0])
                ? r.attributes.map(([k, v]) => kv(k, v))
                : r.attributes,
            })),
          },
        ],
      },
    ],
  };
}

export function buildTracesRequest(spans, cfg) {
  return {
    resourceSpans: [
      {
        resource: { attributes: resourceAttributes(cfg) },
        scopeSpans: [
          {
            ...scope("neurolink.proxy", "1.0.0"),
            spans: spans.map((s) => ({
              traceId: s.traceId,
              spanId: s.spanId,
              name: s.name,
              kind: s.kind,
              startTimeUnixNano: s.startTimeUnixNano,
              endTimeUnixNano: s.endTimeUnixNano,
              attributes: s.attributes.map(([k, v]) => kv(k, v)),
              status: s.status,
              events: s.events ?? [],
            })),
          },
        ],
      },
    ],
  };
}

/** Raw forward of other Claude Code events keeps their original shape. */
export function buildRawLogsRequest(resourceLogs) {
  return { resourceLogs };
}

export { kv as kvPair };
