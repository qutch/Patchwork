#!/usr/bin/env bash
# Start the demo apps: Inbox :8001, Stockroom live :8002, Stockroom staging :8003.
# Ctrl-C stops all three.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

trap 'kill 0' EXIT INT TERM
$PY -m uvicorn demo_apps.inbox.app:create_app --factory --port 8001 --log-level warning &
$PY -m uvicorn demo_apps.stockroom.app:create_app --factory --port 8002 --log-level warning &
$PY -m uvicorn demo_apps.stockroom.app:create_app --factory --port 8003 --log-level warning &
echo "inbox     http://localhost:8001"
echo "stockroom http://localhost:8002  (staging http://localhost:8003)"
wait
