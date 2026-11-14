"""The runtime never calls an LLM: nothing under runner/, compiler/ or spec/ imports an
LLM SDK, and importing the runtime loads none (plan: "a test enforces that")."""

import ast
import subprocess
import sys
from pathlib import Path

from runner.lib import AI_MODULES

REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ["runner", "compiler", "spec", "demo_apps"]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_runtime_source_has_no_ai_imports():
    offenders = {}
    for d in RUNTIME_DIRS:
        for py in (REPO / d).rglob("*.py"):
            bad = _imports(py) & set(AI_MODULES)
            if bad:
                offenders[str(py.relative_to(REPO))] = sorted(bad)
    assert offenders == {}


def test_importing_the_runner_loads_no_ai_modules():
    probe = (
        "import sys, runner.run, runner.__main__, compiler.compile\n"
        f"banned = {AI_MODULES!r}\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in banned)\n"
        "assert not leaked, leaked\n"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_runner_env_strips_model_keys(monkeypatch):
    from control.ops import runner_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("STOCKROOM_TOKEN", "t")
    env = runner_env()
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STOCKROOM_TOKEN"} & set(env)
