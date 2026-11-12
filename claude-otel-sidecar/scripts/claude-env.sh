#!/usr/bin/env bash
# Environment for developer machines: point Claude Code's native OTel
# telemetry at the company sidecar.
#
# Usage:
#   source scripts/claude-env.sh                       # no auth
#   SIDECAR_INGEST_TOKEN=xxx source scripts/claude-env.sh
#
# Verify with:
#   curl -s "$OTEL_EXPORTER_OTLP_ENDPOINT/stats" | jq .
#
# NOTE: Claude Code 2.1.x exports OTLP only when OTEL_EXPORTER_OTLP_PROTOCOL
# is set explicitly (verified against 2.1.280) — do not drop that line.

export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT="${SIDECAR_URL:-https://claude-otel-sidecar-<hash>-el.a.run.app}"

if [ -n "${SIDECAR_INGEST_TOKEN:-}" ]; then
  # OTEL spec: values are URL-encoded; %20 encodes the space in "Bearer x".
  export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer%20${SIDECAR_INGEST_TOKEN}"
fi
