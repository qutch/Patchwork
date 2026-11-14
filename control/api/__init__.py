"""Control-plane API. `control.api:app` is created on first access (it loads the repo `.env`),
so importing `create_app` for tests has no side effects."""

from __future__ import annotations

from typing import Any

from control.api.server import create_app

__all__ = ["app", "create_app"]

_app = None


def __getattr__(name: str) -> Any:
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
