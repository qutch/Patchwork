#!/usr/bin/env bash
# Full stack for humans: Inbox :8001, Stockroom :8002, staging :8003 and the control plane :8000.
# Ctrl-C stops everything. Ports are overridable (API_PORT, INBOX_PORT, STOCKROOM_PORT, STAGING_PORT).
# The UI dev server (web/, :5180) proxies /api to a control plane; point it here or run on 38080.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
API_PORT=${API_PORT:-8000}
INBOX_PORT=${INBOX_PORT:-8001}
STOCKROOM_PORT=${STOCKROOM_PORT:-8002}
STAGING_PORT=${STAGING_PORT:-8003}

export PATCHWORK_INBOX_URL="http://localhost:$INBOX_PORT"
export PATCHWORK_STOCKROOM_URL="http://localhost:$STOCKROOM_PORT"
export PATCHWORK_STAGING_URL="http://localhost:$STAGING_PORT"
export PATCHWORK_SOURCE_BASE_URL="$PATCHWORK_INBOX_URL"      # what the runner reads
export PATCHWORK_SINK_BASE_URL="$PATCHWORK_STOCKROOM_URL"
export PATCHWORK_STAGING_BASE_URL="$PATCHWORK_STAGING_URL"
export PATCHWORK_RUN_PACE_MS=${PATCHWORK_RUN_PACE_MS:-30}    # watchable live counters

trap 'kill 0' EXIT INT TERM
$PY -m uvicorn demo_apps.inbox.app:create_app --factory --port "$INBOX_PORT" --log-level warning &
$PY -m uvicorn demo_apps.stockroom.app:create_app --factory --port "$STOCKROOM_PORT" --log-level warning &
$PY -m uvicorn demo_apps.stockroom.app:create_app --factory --port "$STAGING_PORT" --log-level warning &
$PY -m control.api --port "$API_PORT" --log-level warning &
echo "inbox         $PATCHWORK_INBOX_URL"
echo "stockroom     $PATCHWORK_STOCKROOM_URL  (staging $PATCHWORK_STAGING_URL)"
echo "control plane http://localhost:$API_PORT/api/health   (docs: /docs; agent: ${PATCHWORK_AGENT:-scripted})"
wait
