#!/usr/bin/env bash
# Rebuild docs/walkthrough.html from a fresh, real MVP run (servers start in-process).
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${PY:-.venv/bin/python}
mkdir -p workspace
$PY -W ignore scripts/walkthrough/collect.py "$PWD" workspace/walkthrough-data.json
$PY scripts/walkthrough/build.py "$PWD" workspace/walkthrough-data.json docs/walkthrough.html
