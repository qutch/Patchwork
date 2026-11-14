PY ?= .venv/bin/python

.PHONY: setup test apps demo seed walkthrough

setup:
	uv venv -p 3.12 .venv
	uv pip install -p $(PY) -r pyproject.toml --extra dev

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
