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

# v2 proof (W5). bench-v2: 60 real postings from fixtures/postings through an in-process stack (fixture form,
# control plane, scripted agent) on free ports, no model calls -> bench/v2/results.json (GET /api/bench/v2).
# smoke-v2: the 2-minute script, every beat asserted, against `make up` (PATCHWORK_FORM=google for the demo Form;
# it resets the v2 demo state). smoke-v2-offline: the same beats on an own in-process stack.
.PHONY: bench-v2 smoke-v2 smoke-v2-offline
bench-v2:
	$(PY) -m bench.v2.run

smoke-v2:
	$(PY) scripts/smoke_v2.py

smoke-v2-offline:
	$(PY) scripts/smoke_v2.py --in-process

# smoke-v2-airtable-offline: the same beats with an Airtable destination (V14), into the fake Airtable.
.PHONY: smoke-v2-airtable-offline
smoke-v2-airtable-offline:
	$(PY) scripts/smoke_v2.py --sink airtable --in-process

# smoke-new-integration-offline (plan A Task 9): the New integration wizard, one flow for three unrelated domains
# (products -> fake Airtable, events -> fixture form, papers -> fake papers API), each on an own in-process stack:
# new -> destination -> 2 example pages -> teach -> approve -> rows -> a third site learned -> rename, held, repaired.
NEW_INTEGRATION_DOMAINS ?= products events papers
.PHONY: smoke-new-integration-offline
smoke-new-integration-offline:
	@for d in $(NEW_INTEGRATION_DOMAINS); do $(PY) scripts/smoke_v2.py --new-integration $$d --in-process || exit 1; done

# v2 stage (I-2): control plane on :38091 over workspace-v2/ (gitignored; holds the TEACH + Greenhouse/Ashby baseline
# built on the demo Google Form), deep agent. Never touches 8000-8003/5180. smoke-v2-stage runs the 2-minute script
# against it on the demo Form + Sheet: it pauses for you to rename "Company" -> "Employer" in the Form editor.
DEMO_FORM_URL ?= https://docs.google.com/forms/d/e/1FAIpQLSeJSRbMx1pr9nMwj4n1E5vOUxmz2Zo19-lJXyz0-8HhmfeNoQ/viewform
DEMO_SHEET_CSV ?= https://docs.google.com/spreadsheets/d/1sEDvJMO4sQN3RJxo1JKFqqfrDfQTjZrNL2Ztlz0j60M/gviz/tq?tqx=out:csv&sheet=Form%20Responses%201
STAGE_PORT ?= 38091
STAGE_AGENT ?= deep
# The model comes from PATCHWORK_MODEL / PATCHWORK_MODEL_FALLBACK (shell, else .env), e.g.
# PATCHWORK_MODEL=anthropic:claude-haiku-5-5 with ANTHROPIC_API_KEY in .env; or STAGE_MODEL=... on the make line.
STAGE_MODEL ?=
.PHONY: stage-v2 web-stage smoke-v2-stage
stage-v2:
	PATCHWORK_WORKSPACE=$(CURDIR)/workspace-v2 PATCHWORK_AGENT=$(STAGE_AGENT) \
	$(if $(STAGE_MODEL),PATCHWORK_MODEL=$(STAGE_MODEL)) \
	PATCHWORK_AI_ENABLED=1 $(PY) -m control.api --port $(STAGE_PORT)

web-stage:
	test -d web/node_modules || (cd web && npm ci)
	cd web && PATCHWORK_API=http://127.0.0.1:$(STAGE_PORT) npm run dev -- --port 5181

smoke-v2-stage:
	PATCHWORK_CONTROL=http://127.0.0.1:$(STAGE_PORT) PATCHWORK_FORM=google PATCHWORK_DEMO_FORM_URL='$(DEMO_FORM_URL)' \
	PATCHWORK_DEMO_SHEET_CSV='$(DEMO_SHEET_CSV)' PATCHWORK_SMOKE_TIMEOUT=$${PATCHWORK_SMOKE_TIMEOUT:-300} \
	$(PY) scripts/smoke_v2.py
