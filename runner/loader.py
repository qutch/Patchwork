"""Load a compiled integration package (workspace/integrations/<name>/vN/) by path."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_integration(version_dir: str | Path) -> ModuleType:
    version_dir = Path(version_dir).resolve()
    path_tag = hashlib.sha1(str(version_dir).encode()).hexdigest()[:10]
    name = f"pw_{version_dir.parent.name}_{version_dir.name}_{path_tag}"
    if name in sys.modules:
        return sys.modules[f"{name}.integration"]
    spec = importlib.util.spec_from_file_location(
        name, version_dir / "__init__.py", submodule_search_locations=[str(version_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load integration package at {version_dir}")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    return importlib.import_module(f"{name}.integration")
