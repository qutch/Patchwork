PY ?= .venv/bin/python

.PHONY: setup test apps demo seed walkthrough up api

setup:
	uv venv -p 3.12 .venv
	uv pip install -p $(PY) -r pyproject.toml --extra dev --extra agents --extra capture
	$(PY) -m playwright install chromium

test:
	$(PY) -m pytest -q

apps:
	scripts/dev.sh

demo:
	scripts/demo.sh

seed:
	$(PY) -m demo_apps.seed

walkthrough:
	scripts/walkthrough/make.sh

up:
	PY=$(PY) scripts/up.sh

api:
	$(PY) -m control.api --port 38080

# Pre-demo check: drive the whole demo over HTTP against `make up` (resets the demo state).
smoke:
	$(PY) scripts/smoke_demo.py

# UI (web/): dev server on :5180. `web` proxies to the control plane (PATCHWORK_API, default :8000 = `make up`).
PATCHWORK_API ?= http://127.0.0.1:8000
.PHONY: web web-mock smoke
web:
	test -d web/node_modules || (cd web && npm ci)
	cd web && PATCHWORK_API=$(PATCHWORK_API) npm run dev

web-mock:
	test -d web/node_modules || (cd web && npm ci)
	cd web && npm run dev:mock

# Benchmark: Patchwork vs a one-shot script on the demo scenario, own in-process stack on free ports
# (never 8000-8003), scripted agent, no model calls. Writes bench/results.json (served at /api/bench/results).
.PHONY: bench
bench:
	$(PY) -m bench.run
