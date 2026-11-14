"""Control-plane side of destination/source credentials (contracts V13).

The spec names a credential; the value lives in `.env` (loaded into this process's environment by
control.api.settings.load_dotenv). This module answers "which credentials does the workspace use, and are
they set?" (`GET /api/credentials`) and redacts every value the workspace's specs reference from text that
leaves the control plane (agent tool results and events, API error responses, runner stderr tails).

Names come from the specs on disk: every compiled version (integrations/<name>/v<N>/spec.yaml) and every
agent work dir (integrations/<name>/work/spec.yaml). Never a value is returned by anything here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

from runner import credentials as cred
from runner.store import workspace_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"  # tests monkeypatch this; never write the real file from a test

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


def _registry_path() -> Path:
    return workspace_dir() / "credentials.json"


def _registered() -> list[str]:
    """Names added from the UI (names only, never values): $WORKSPACE/credentials.json."""
    try:
        data = json.loads(_registry_path().read_text())
    except (OSError, ValueError):
        return []
    return [n for n in data if isinstance(n, str)] if isinstance(data, list) else []


def _env_names(path: Path) -> list[str]:
    """Names in the .env file that could be credentials (cached by mtime; used by redaction on every event)."""
    try:
        mtime = path.stat().st_mtime_ns
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        names = []
        for raw in path.read_text().splitlines():
            key = raw.strip().removeprefix("export ").partition("=")[0].strip()
            if "=" in raw and not raw.strip().startswith("#"):
                try:
                    names.append(cred.check_name(key))
                except ValueError:
                    pass
    except OSError:
        return []
    with _lock:
        _cache[path] = (mtime, names)
    return names


def set_credential(name: str, value: str, env_path: Path | None = None) -> None:
    """Set credential `name` in `.env` (rewrite its line or append one; atomic, mode 0600), in this process's
    environment, and in the workspace's list of UI-added names. ValueError for a bad name or value."""
    cred.check_name(name)
    if not isinstance(value, str) or not value or re.search(r"[\r\n\x00]", value):
        raise ValueError("credential value must be one non-empty line")
    path = Path(env_path) if env_path else ENV_PATH
    lines = path.read_text().splitlines() if path.is_file() else []
    out, done = [], False
    for line in lines:
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key == name and "=" in line and not line.strip().startswith("#"):
            if not done:
                out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(path, 0o600)
    os.environ[name] = value
    with _lock:
        reg = sorted({*_registered(), name})
        _registry_path().parent.mkdir(parents=True, exist_ok=True)
        _registry_path().write_text(json.dumps(reg))


def known_names(spec: Any = None) -> list[str]:
    """Every credential name the workspace references (specs, UI-added, .env), plus `spec`'s own."""
    return sorted({*workspace_credentials(), *_registered(), *_env_names(ENV_PATH), *cred.credential_names(spec)})


def redact(text: Any, spec: Any = None) -> str:
    """`runner.credentials.redact` over every credential the workspace (and `spec`) references."""
    return cred.redact(text, names=known_names(spec))


def redact_obj(obj: Any, spec: Any = None) -> Any:
    return cred.redact_obj(obj, names=known_names(spec))


cred.NAME_PROVIDERS.append(known_names)


def list_credentials() -> list[dict[str, Any]]:
    """`GET /api/credentials` (V13): [{name, set, used_by}] for every referenced credential. Never values."""
    used = workspace_credentials()
    names = sorted({*used, *_registered()})
    return [{"name": name, "set": cred.is_set(name), "used_by": used.get(name, [])} for name in names]
