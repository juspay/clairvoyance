// Outgoing OTLP exporter: batching, retries, and the periodic metric flush
// loop. Exports OTLP/protobuf (like NeuroLink's own exporters did), which is
// the path OpenObserve handles most robustly. Best-effort — a backend outage
// must never block ingestion.

import {
  encodeMetricsRequest,
  encodeLogsRequest,
  encodeRawLogsRequest,
  encodeTracesRequest,
  snapshotEntries,
  passthroughEntries,
} from "./protobuf-encode.mjs";

export class Exporter {
  /**
   * @param cfg config
   * @param stats shared counters object
   * @param metricsProvider () => aggregator snapshot; called on every flush so
   *        cumulative series are re-exported each interval exactly like
   *        NeuroLink's PeriodicExportingMetricReader did.
   */
  constructor(cfg, stats, metricsProvider) {
    this.cfg = cfg;
    this.stats = stats;
    this.metricsProvider = metricsProvider ?? (() => new Map());
    this.logQueues = new Map(); // streamName -> logRecords[]
    this.spanQueue = [];
    this.rawMetricsBatch = []; // decoded claude_code.* resourceMetrics
    this.timers = [];
    this.stopped = false;
    this.exporting = false;
  }

  start() {
    if (!this.cfg.metricsEndpoint && !this.cfg.logsEndpoint && !this.cfg.tracesEndpoint) return;
    if (this.cfg.metricsEndpoint) {
      this.timers.push(setInterval(() => this.#safe(this.flushMetrics()), this.cfg.metricsIntervalMs));
    }
    if (this.cfg.logsEndpoint || this.cfg.tracesEndpoint) {
      this.timers.push(
        setInterval(() => this.#safe(this.flushLogsAndSpans()), this.cfg.logsFlushIntervalMs),
      );
    }
    for (const t of this.timers) t.unref?.();
  }

  #safe(promise) {
    promise.catch(() => {});
  }

  enqueueLog(record, streamName = this.cfg.logsStreamName) {
    let q = this.logQueues.get(streamName);
    if (!q) {
      q = [];
      this.logQueues.set(streamName, q);
    }
    q.push(record);
  }

  enqueueSpan(span) {
    this.spanQueue.push(span);
  }

  enqueueRawMetrics(resourceMetrics) {
    this.rawMetricsBatch.push(...resourceMetrics);
  }

  async flushMetrics() {
    if (!this.cfg.metricsEndpoint) return;
    const entries = [];
    if (this.rawMetricsBatch.length) {
      if (this.cfg.forwardRawMetrics) entries.push(...passthroughEntries(this.rawMetricsBatch));
      this.rawMetricsBatch = [];
    }
    entries.push(...snapshotEntries(this.metricsProvider(), this.cfg));
    if (!entries.length) return;
    await this.post(
      this.cfg.metricsEndpoint,
      { "content-type": "application/x-protobuf" },
      encodeMetricsRequest(entries),
      "metrics",
    );
  }

  async flushLogsAndSpans() {
    if (this.exporting) return;
    this.exporting = true;
    try {
      for (const [streamName, queue] of this.logQueues) {
        while (queue.length) {
          const batch = queue.splice(0, this.cfg.logsBatchSize);
          const body =
            streamName === this.cfg.logsStreamName
              ? encodeLogsRequest(batch, this.cfg)
              : encodeRawLogsRequest([
                  {
                    resource: { attributes: [] },
                    scopeLogs: [
                      { scope: { name: "claude-code-events", version: "0" }, logRecords: batch },
                    ],
                  },
                ]);
          await this.post(
            this.cfg.logsEndpoint,
            {
              "content-type": "application/x-protobuf",
              "stream-name": streamName,
            },
            body,
            `logs:${streamName}`,
          );
        }
      }

      if (this.spanQueue.length && this.cfg.tracesEndpoint) {
        while (this.spanQueue.length) {
          const batch = this.spanQueue.splice(0, this.cfg.logsBatchSize);
          await this.post(
            this.cfg.tracesEndpoint,
            {
              "content-type": "application/x-protobuf",
              "stream-name": this.cfg.tracesStreamName,
            },
            encodeTracesRequest(batch, this.cfg),
            "traces",
          );
        }
      }
    } finally {
      this.exporting = false;
    }
  }

  async post(url, headers, body, kind) {
    if (!url) return;
    const allHeaders = { ...headers, ...this.cfg.exportHeaders };
    let attempt = 0;
    for (;;) {
      attempt++;
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: allHeaders,
          body,
          signal: AbortSignal.timeout(this.cfg.exportTimeoutMs),
        });
        if (res.ok) {
          this.stats.exported[kind] = (this.stats.exported[kind] ?? 0) + 1;
          this.stats.lastExportAt = new Date().toISOString();
          return;
        }
        const text = await res.text().catch(() => "");
        // 4xx (except 429) will not succeed on retry — drop.
        if (res.status < 500 && res.status !== 429) {
          this.stats.dropped[kind] = (this.stats.dropped[kind] ?? 0) + 1;
          this.stats.lastExportError = `${kind}: HTTP ${res.status} ${text.slice(0, 200)}`;
          return;
        }
        throw new Error(`HTTP ${res.status} ${text.slice(0, 120)}`);
      } catch (err) {
        if (attempt > this.cfg.exportRetries) {
          this.stats.dropped[kind] = (this.stats.dropped[kind] ?? 0) + 1;
          this.stats.lastExportError = `${kind}: ${err.message}`;
          return;
        }
        await sleep(Math.min(500 * 2 ** (attempt - 1), 5000));
      }
    }
  }

  async shutdown() {
    this.stopped = true;
    for (const t of this.timers) clearInterval(t);
    await this.#safeAsync(this.flushMetrics());
    await this.#safeAsync(this.flushLogsAndSpans());
  }

  async #safeAsync(p) {
    try {
      await p;
    } catch {
      /* best-effort shutdown flush */
    }
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
