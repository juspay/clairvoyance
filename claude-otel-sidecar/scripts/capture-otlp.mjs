#!/usr/bin/env node
// Capture server: records raw OTLP http/json exports from Claude Code to files.
// Usage: node scripts/capture-otlp.mjs [port] [outdir]
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const port = Number(process.argv[2] ?? 4318);
const outdir = path.resolve(process.argv[3] ?? "fixtures/capture");
fs.mkdirSync(outdir, { recursive: true });

let n = 0;
const server = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const body = Buffer.concat(chunks);
    const kind = req.url.includes("/metrics")
      ? "metrics"
      : req.url.includes("/traces")
        ? "traces"
        : req.url.includes("/logs")
          ? "logs"
          : "other";
    const file = path.join(outdir, `${String(++n).padStart(3, "0")}-${kind}.json`);
    let parsed = null;
    try {
      parsed = JSON.parse(body.toString("utf8"));
    } catch {
      // protobuf payload — save raw
      fs.writeFileSync(file.replace(/\.json$/, ".bin"), body);
    }
    if (parsed) fs.writeFileSync(file, JSON.stringify(parsed, null, 2));
    console.log(`[capture] ${req.method} ${req.url} ct=${req.headers["content-type"]} bytes=${body.length} -> ${path.basename(file)}`);
    res.writeHead(200, { "content-type": "application/json" });
    res.end("{}");
  });
});
server.listen(port, "127.0.0.1", () => console.log(`[capture] listening on http://127.0.0.1:${port} -> ${outdir}`));
