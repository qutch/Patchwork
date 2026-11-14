"""The deep-agent Airtable tests import langchain/deepagents into the pytest process; tests that run the runner
in-process refuse when AI modules are loaded, so run them last (as tests/agents/conftest.py does for its own)."""

from __future__ import annotations


def pytest_collection_modifyitems(config, items):
    ours = [i for i in items if "tests/airtable/test_deep_" in str(i.fspath).replace("\\", "/")]
    items[:] = [i for i in items if i not in ours] + ours
