#!/usr/bin/env bash
# Hosted one-command installer, served by the sidecar itself at GET /install.sh.
# The sidecar injects its own origin and the ingest token before serving.
#
# Developer setup is just:
#   curl -fsSL https://<sidecar>/install.sh | bash
#
# Env overrides: TOKEN=<per-team-token> (advanced).

set -euo pipefail

BASE="__SIDECAR_BASE__"
TOKEN="${TOKEN:-__INGEST_TOKEN__}"
SETTINGS="$HOME/.claude/settings.json"

command -v node >/dev/null || { echo "node not found on PATH — install node first"; exit 1; }
command -v curl >/dev/null || { echo "curl not found"; exit 1; }

mkdir -p "$HOME/.claude"
curl -fsSL "$BASE/cc-usage-statusline.mjs" -o "$HOME/.claude/cc-usage-statusline.mjs"

STATUSLINE_CMD="env OTEL_EXPORTER_OTLP_ENDPOINT=$BASE OTEL_EXPORTER_OTLP_HEADERS=authorization=$TOKEN node $HOME/.claude/cc-usage-statusline.mjs"

node -e '
const fs = require("fs");
const p = process.argv[1];
const [base, token, statuslineCmd] = process.argv.slice(2);
let s = {};
try { s = JSON.parse(fs.readFileSync(p, "utf8")); } catch { /* fresh */ }
s.env = {
  ...(s.env ?? {}),
  CLAUDE_CODE_ENABLE_TELEMETRY: "1",
  OTEL_METRICS_EXPORTER: "otlp",
  OTEL_LOGS_EXPORTER: "otlp",
  OTEL_TRACES_EXPORTER: "otlp",
  OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf",
  OTEL_EXPORTER_OTLP_ENDPOINT: base,
  OTEL_EXPORTER_OTLP_HEADERS: "authorization=" + token,
};
s.statusLine = { type: "command", command: statuslineCmd, padding: 0 };
fs.writeFileSync(p, JSON.stringify(s, null, 2) + "\n");
console.log("settings.json updated (existing keys preserved)");
' "$SETTINGS" "$BASE" "$TOKEN" "$STATUSLINE_CMD"

echo
echo "Done. Restart any running Claude Code sessions."
echo "Verify: open claude — the bottom line should show:  Sonnet · 5h NN% · wk NN%"
echo "Pipeline stats: curl -s $BASE/stats"
