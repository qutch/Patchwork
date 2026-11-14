"""`.env` loading and the control plane's settings (the AI on/off switch).

Settings persist to $PATCHWORK_WORKSPACE/settings.json so the switch survives a restart.
`PATCHWORK_AI_ENABLED=0` sets the default for a fresh workspace.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from runner.store import workspace_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> list[str]:
    """Minimal .env reader: KEY=VALUE lines, optional `export`, quotes stripped. Never overrides
    variables already set. Returns the keys it set. Model keys stay out of the runner because
    `control.ops.runner_env` strips them."""
    path = path or REPO_ROOT / ".env"
    if os.environ.get("PATCHWORK_NO_DOTENV") or not path.is_file():
        return []
    loaded = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class Settings:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    @staticmethod
    def _path() -> Path:
        return workspace_dir() / "settings.json"

    def _read(self) -> dict[str, Any]:
        default = {"ai_enabled": os.environ.get("PATCHWORK_AI_ENABLED", "1").lower() not in ("0", "false", "no", "off")}
        try:
            return {**default, **json.loads(self._path().read_text())}
        except (OSError, ValueError):
            return default

    @property
    def ai_enabled(self) -> bool:
        return bool(self._read()["ai_enabled"])

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            data = {**self._read(), **changes}
            self._path().parent.mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps(data, indent=2))
            return data


def extension_token() -> str:
    """The token the extension sends as X-Patchwork-Token (V9). Kept in $PATCHWORK_WORKSPACE/extension_token,
    created on first use (mode 0600); `PATCHWORK_EXTENSION_TOKEN` overrides it. The runner never sees it
    (`control.ops.runner_env` strips *_TOKEN variables, and it reads pages without one)."""
    override = os.environ.get("PATCHWORK_EXTENSION_TOKEN")
    if override:
        return override
    path = workspace_dir() / "extension_token"
    try:
        token = path.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # created by someone else between our read and now
        return path.read_text().strip() or token
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token
