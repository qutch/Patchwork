"""Runs the extension's discovery unit tests (tests/extension/discover.test.js) with node's built-in test runner,
so the full pytest suite covers them. Skipped when node is not installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_discover_js_with_node():
    done = subprocess.run(["node", "--test", str(HERE / "discover.test.js")], capture_output=True, text=True,
                          timeout=60, cwd=HERE)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "# fail 0" in done.stdout
