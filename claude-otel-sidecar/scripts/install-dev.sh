#!/usr/bin/env bash
# One-command setup for developers: wires Claude Code telemetry to the company
# sidecar. Idempotent — safe to re-run; keeps all existing settings.
#
# Usage:
#   bash install-dev.sh
#   TOKEN=xxxx bash install-dev.sh            # custom ingest token
#   SIDECAR_URL=https://... bash install-dev.sh
#
# Requires: node (for the statusline collector + the settings merge).

set -euo pipefail

SIDECAR_URL="${SIDECAR_URL:-https://claude-otel-sidecar-840384202515.asia-south1.run.app}"
TOKEN="${TOKEN:-6411c2deaef3f02e93a6a2890ad4e5911b025029}"
SETTINGS="$HOME/.claude/settings.json"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v node >/dev/null || { echo "node not found on PATH — install node first"; exit 1; }

mkdir -p "$HOME/.claude"
cp "$HERE/cc-usage-statusline.mjs" "$HOME/.claude/cc-usage-statusline.mjs"

STATUSLINE_CMD="env OTEL_EXPORTER_OTLP_ENDPOINT=$SIDECAR_URL OTEL_EXPORTER_OTLP_HEADERS=authorization=$TOKEN node $HOME/.claude/cc-usage-statusline.mjs"

node -e '
const fs = require("fs");
const p = process.argv[1];
const [sidcarUrl, token, statuslineCmd] = process.argv.slice(2);
let s = {};
try { s = JSON.parse(fs.readFileSync(p, "utf8")); } catch { /* fresh */ }
s.env = {
  ...(s.env ?? {}),
  CLAUDE_CODE_ENABLE_TELEMETRY: "1",
  OTEL_METRICS_EXPORTER: "otlp",
  OTEL_LOGS_EXPORTER: "otlp",
  OTEL_TRACES_EXPORTER: "otlp",
  OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf",
  OTEL_EXPORTER_OTLP_ENDPOINT: sidcarUrl,
  OTEL_EXPORTER_OTLP_HEADERS: "authorization=" + token,
};
s.statusLine = { type: "command", command: statuslineCmd, padding: 0 };
fs.writeFileSync(p, JSON.stringify(s, null, 2) + "\n");
console.log("settings.json updated (existing keys preserved)");
' "$SETTINGS" "$SIDECAR_URL" "$TOKEN" "$STATUSLINE_CMD"

echo
echo "Done. Restart any running Claude Code sessions."
echo "Verify: open claude, you should see a status line like:  Sonnet · 5h 11% · wk 33%"
echo "Stats:  curl -s $SIDECAR_URL/stats"
