#!/usr/bin/env bash
# MVP demo, AI off throughout. Needs scripts/dev.sh running in another terminal.
#   1. compile the fixture spec -> v1 and run its generated tests
#   2. approve v1, run 100 POs, then the rest  -> LLM calls: 0
#   3. accuracy against ground truth
#   4. Break 1: switch Stockroom to contract v2 -> preflight blocks the run before any write
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
export PATCHWORK_WORKSPACE=${PATCHWORK_WORKSPACE:-workspace}

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "reset demo state"
curl -sf -X POST localhost:8002/admin/reset >/dev/null
curl -sf -X POST localhost:8003/admin/reset >/dev/null
rm -rf "$PATCHWORK_WORKSPACE/integrations" "$PATCHWORK_WORKSPACE/runs" "$PATCHWORK_WORKSPACE/patchwork.db"*

step "compile fixtures/spec.yaml -> v1 (+ generated tests)"
$PY -m control compile fixtures/spec.yaml

step "approve v1"
$PY -m control deploy po_to_stockroom 1 --yes

step "dry run 10 records against staging"
$PY -m control run po_to_stockroom --dry-run --limit 10

step "run 100 records (AI off)"
$PY -m control run po_to_stockroom --limit 100

step "run the rest"
$PY -m control run po_to_stockroom

step "accuracy vs ground truth"
$PY -m bench.accuracy --live po_to_stockroom | head -5

step "Break 1: Stockroom switches to contract v2"
curl -sf -X POST localhost:8002/admin/schema -H 'content-type: application/json' -d '{"version": 2}'
echo
$PY -m control run po_to_stockroom || echo "(blocked as expected: exit $?)"
$PY -m control status po_to_stockroom | $PY -c 'import json,sys; s=json.load(sys.stdin); print(json.dumps(s["open_incidents"], indent=2))'
