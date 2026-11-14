"""HTTP binding for generated adapters: `request(target, method, path, ...)` where target is
"source" or "sink". Base URLs come from the version's config.yaml, overridable by env."""

from __future__ import annotations

import os
from typing import Any

import httpx


class Http:
    def __init__(self, config: dict[str, Any], *, staging: bool = False, timeout: float = 10.0):
        source = os.environ.get("PATCHWORK_SOURCE_BASE_URL") or config["source"]["base_url"]
        if staging:
            sink = (os.environ.get("PATCHWORK_STAGING_BASE_URL") or config["sink"].get("staging_base_url"))
            if not sink:
                raise RuntimeError("dry run needs sink.staging_base_url in the spec (agents never write to live)")
        else:
            sink = os.environ.get("PATCHWORK_SINK_BASE_URL") or config["sink"]["base_url"]
        self.base = {"source": source.rstrip("/"), "sink": sink.rstrip("/")}
        self.client = httpx.Client(timeout=timeout)

    def request(self, target: str, method: str, path: str, *, params: dict | None = None,
                json: Any = None) -> httpx.Response:
        return self.client.request(method, self.base[target] + path, params=params, json=json)

    def close(self) -> None:
        self.client.close()
