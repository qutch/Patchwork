"""Agents tests. Real-model tests carry @pytest.mark.live and only run with `-m live`; the default
suite stays offline."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls a real model through OpenRouter (opt in with -m live)")


def pytest_collection_modifyitems(config, items):
    # These tests import langchain/langgraph/deepagents into the pytest process. Other tests run the
    # runner in-process, and the runner refuses to start when AI modules are loaded, so run ours last.
    ours = [i for i in items if "tests/agents/" in str(i.fspath).replace("\\", "/")]
    items[:] = [i for i in items if i not in ours] + ours
    if "live" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="real-model test; run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
