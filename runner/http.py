"""HTTP binding for generated adapters: `request(target, method, path, ...)` where target is
"source" or "sink". Base URLs come from the version's config.yaml, overridable by env.

v2 (contracts V5, V7):
  * source `patchwork://control` resolves to $PATCHWORK_CONTROL_URL (required; the control plane sets
    it for every runner it launches). PATCHWORK_SOURCE_BASE_URL does not apply to it.
  * auth (contracts V13): config.yaml's `source.auth` / `sink.auth` blocks name a credential; its value comes
    from PATCHWORK_CRED__<NAME> (set by control.ops.runner_env) and is sent as a header on every request to
    that target. A missing value is a ConfigError (a failed run that names the credential, never a value).
  * form sinks (config sink.kind == "form") have no base URL, no staging URL and ignore
    PATCHWORK_SINK_BASE_URL; `form_submit` / `form_contract` drive the run's one headless browser.
    A dry run (`staging=True`) fills forms but never submits (runner/forms.py enforces it).

v3 (contracts V14): Airtable sinks (config sink.kind == "airtable") have no base URL either (the API is
https://api.airtable.com/v0 or $PATCHWORK_AIRTABLE_API_URL); auth headers come from
`runner.credentials.headers(sink.auth)`; `airtable_contract` / `airtable_submit` use one
`runner.airtable.AirtableSession` per run. A dry run validates against the live table and never POSTs.

v4 (contracts V16): SQLite sinks (config sink.kind == "sqlite") have no base URL and no credential: a database
file under the workspace. `sqlite_contract` / `sqlite_submit` use one `runner.sqlite_sink.SqliteSession` per
run. A dry run validates against the live schema and never INSERTs.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx

from runner import credentials

CONTROL_SCHEME = "patchwork://control"


class ConfigError(RuntimeError):
    """The run cannot start with this configuration (message is meant for a person)."""


def resolve_source(base_url: str) -> str:
    if base_url.rstrip("/") == CONTROL_SCHEME:
        url = os.environ.get("PATCHWORK_CONTROL_URL", "").strip()
        if not url:
            raise ConfigError("source base_url is patchwork://control but PATCHWORK_CONTROL_URL is not set "
                              "(the control plane sets it for every run it launches)")
        return url
    return os.environ.get("PATCHWORK_SOURCE_BASE_URL") or base_url


class Http:
    def __init__(self, config: dict[str, Any], *, staging: bool = False, timeout: float = 10.0):
        source = resolve_source(config["source"]["base_url"])
        self.dry_run = staging
        self.sink_kind = config["sink"].get("kind", "api")
        self._forms: Any = None
        self._airtable: Any = None
        self._sqlite: Any = None
        self.base = {"source": source.rstrip("/")}
        if self.sink_kind == "form":
            self.form_url = config["sink"]["url"]
        elif self.sink_kind == "airtable":  # no base URL: runner/airtable.py (sink.auth headers below)
            self._airtable_config = dict(config["sink"])
        elif self.sink_kind == "sqlite":  # no base URL, no auth: runner/sqlite_sink.py
            self._sqlite_config = dict(config["sink"])
        elif staging:
            sink = (os.environ.get("PATCHWORK_STAGING_BASE_URL") or config["sink"].get("staging_base_url"))
            if not sink:
                raise RuntimeError("dry run needs sink.staging_base_url in the spec (agents never write to live)")
            self.base["sink"] = sink.rstrip("/")
        else:
            sink = os.environ.get("PATCHWORK_SINK_BASE_URL") or config["sink"]["base_url"]
            self.base["sink"] = sink.rstrip("/")
        self.auth = {"source": config["source"].get("auth"),
                     "sink": None if self.sink_kind == "form" else config["sink"].get("auth")}
        try:
            self.headers = {t: credentials.headers(a) for t, a in self.auth.items()}
        except (credentials.CredentialError, ValueError) as e:
            raise ConfigError(str(e)) from None
        self.client = httpx.Client(timeout=timeout)

    @property
    def sink_credential(self) -> str | None:
        """The credential name the sink requests carry (for AUTH quarantine details), else None."""
        return (self.auth.get("sink") or {}).get("credential")

    @property
    def sink_label(self) -> str:
        return urlparse(self.base.get("sink", "")).netloc or "the destination"

    def request(self, target: str, method: str, path: str, *, params: dict | None = None,
                json: Any = None) -> httpx.Response:
        return self.client.request(method, self.base[target] + path, params=params, json=json,
                                   headers=self.headers.get(target) or None)

    # -- form sinks -----------------------------------------------------------------------------
    @property
    def forms(self) -> Any:
        if self._forms is None:
            from runner.forms import FormSession

            self._forms = FormSession(dry_run=self.dry_run)
        return self._forms

    def form_contract(self, url: str) -> dict:
        return self.forms.contract(url)

    def form_submit(self, url: str, values: dict, *, submit: str = "Submit",
                    confirmation_text: str | None = None) -> Any:
        return self.forms.submit(url, {k: "" if v is None else str(v) for k, v in values.items()},
                                 submit=submit, confirmation_text=confirmation_text)

    # -- Airtable sinks (contracts V14) ------------------------------------------------------------
    @property
    def airtable(self) -> Any:
        if self._airtable is None:
            from runner.airtable import AirtableSession

            cfg = self._airtable_config
            self._airtable = AirtableSession(
                cfg["base_id"], cfg["table"], self.headers.get("sink") or {},
                credential=(cfg.get("auth") or {}).get("credential"), typecast=bool(cfg.get("typecast")),
                read_back=bool(cfg.get("read_back", True)), dry_run=self.dry_run)
        return self._airtable

    def airtable_contract(self, base_id: str, table: str) -> dict:
        return self.airtable.contract(refresh=True)

    def airtable_submit(self, base_id: str, table: str, values: dict) -> Any:
        return self.airtable.submit(values)

    # -- SQLite sinks (contracts V16) ----------------------------------------------------------------
    @property
    def sqlite(self) -> Any:
        if self._sqlite is None:
            from runner.sqlite_sink import SqliteSession

            cfg = self._sqlite_config
            self._sqlite = SqliteSession(cfg["path"], cfg["table"], read_back=bool(cfg.get("read_back", True)),
                                         dry_run=self.dry_run)
        return self._sqlite

    def sqlite_contract(self, path: str, table: str) -> dict:
        return self.sqlite.contract(refresh=True)

    def sqlite_submit(self, path: str, table: str, values: dict) -> Any:
        return self.sqlite.submit(values)

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            if self._forms is not None:
                self._forms.close()
                self._forms = None
            if self._airtable is not None:
                self._airtable.close()
                self._airtable = None
            if self._sqlite is not None:
                self._sqlite.close()
                self._sqlite = None
