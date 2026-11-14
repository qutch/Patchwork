"""Control-plane side of destination/source credentials (contracts V13).

The spec names a credential; the value lives in `.env` (loaded into this process's environment by
control.api.settings.load_dotenv). This module answers "which credentials does the workspace use, and are
they set?" (`GET /api/credentials`) and redacts every value the workspace's specs reference from text that
leaves the control plane (agent tool results and events, API error responses, runner stderr tails).

Names come from the specs on disk: every compiled version (integrations/<name>/v<N>/spec.yaml) and every
agent work dir (integrations/<name>/work/spec.yaml). Never a value is returned by anything here.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from runner import credentials as cred
from runner.store import workspace_dir

_cache: dict[Path, tuple[int, list[str]]] = {}
_lock = threading.Lock()


def _names_in(path: Path) -> list[str]:
    """Credential names in one spec file, cached by mtime (redaction runs on every agent event)."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return []
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        names = cred.credential_names(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError):
        names = []
    with _lock:
        _cache[path] = (mtime, names)
    return names


def _spec_files() -> list[tuple[str, Path]]:
    root = workspace_dir() / "integrations"
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*/*/spec.yaml")):
        out.append((p.parent.parent.name, p))
    return out


def workspace_credentials() -> dict[str, list[str]]:
    """{credential name: [integration, ...]} for every credential a spec in the workspace references."""
    used: dict[str, set[str]] = {}
    for integration, path in _spec_files():
        for name in _names_in(path):
            used.setdefault(name, set()).add(integration)
    return {k: sorted(v) for k, v in sorted(used.items())}


def known_names(spec: Any = None) -> list[str]:
    """Every credential name the workspace references, plus the ones `spec` (model or dict) references."""
    return sorted({*workspace_credentials(), *cred.credential_names(spec)})


def redact(text: Any, spec: Any = None) -> str:
    """`runner.credentials.redact` over every credential the workspace (and `spec`) references."""
    return cred.redact(text, names=known_names(spec))


def redact_obj(obj: Any, spec: Any = None) -> Any:
    return cred.redact_obj(obj, names=known_names(spec))


def list_credentials() -> list[dict[str, Any]]:
    """`GET /api/credentials` (V13): [{name, set, used_by}] for every referenced credential. Never values."""
    return [{"name": name, "set": cred.is_set(name), "used_by": used_by}
            for name, used_by in workspace_credentials().items()]
