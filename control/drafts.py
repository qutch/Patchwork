"""Draft integrations (the new-integration wizard): an integration that has a name and an intent but no
deployed version yet. Stored as $PATCHWORK_WORKSPACE/drafts/<name>.json.

States: draft (collecting pages, destination, credential) -> teaching (a TEACH thread is building it) ->
live (a version is deployed; `control.ops.deploy` flips it). Pages sent to a draft wait in the inbox: no run
and no learn starts until the integration is live.

`delete_integration` removes an integration completely (draft, versions, runs, pages, learn links, agent
threads, recording) and never touches credentials.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from runner.store import Store, workspace_dir

NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RECORD_ID = re.compile(r"\brec[A-Za-z0-9]{14}\b")


class DraftError(ValueError):
    """Bad name or intent."""


class DraftExists(DraftError):
    """The name is taken by a draft or an integration."""


class Draft(BaseModel):
    name: str
    intent: str
    state: Literal["draft", "teaching", "live"] = "draft"
    destination: dict[str, Any] | None = None
    example_page_ids: list[str] = Field(default_factory=list)
    thread_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


def drafts_dir() -> Path:
    return workspace_dir() / "drafts"


def _path(name: str) -> Path:
    if not NAME.match(name or ""):
        raise DraftError(f"integration name {name!r} must be lowercase letters, digits and _, starting with a letter")
    return drafts_dir() / f"{name}.json"


def _write(draft: Draft) -> None:
    path = _path(draft.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".draft.")
    with os.fdopen(fd, "w") as f:
        f.write(draft.model_dump_json(indent=2))
    os.replace(tmp, path)


def create(name: str, intent: str) -> Draft:
    intent = " ".join((intent or "").split())
    if not intent:
        raise DraftError("describe what the integration should do")
    path = _path(name)
    store = Store()
    try:
        taken = path.exists() or any(i["name"] == name for i in store.integrations())
    finally:
        store.close()
    if taken:
        raise DraftExists(f"an integration named {name} already exists; pick another name")
    draft = Draft(name=name, intent=intent)
    _write(draft)
    return draft


def get(name: str) -> Draft | None:
    try:
        return Draft.model_validate_json(_path(name).read_text())
    except (OSError, ValueError):
        return None


def list_all() -> list[Draft]:
    out = []
    for p in sorted(drafts_dir().glob("*.json")) if drafts_dir().is_dir() else []:
        d = get(p.stem)
        if d is not None:
            out.append(d)
    return sorted(out, key=lambda d: (d.created_at, d.name))


def update(name: str, **fields: Any) -> Draft:
    draft = get(name)
    if draft is None:
        raise KeyError(name)
    draft = Draft.model_validate({**draft.model_dump(), **fields})
    _write(draft)
    return draft


def is_waiting(name: str | None) -> bool:
    """True for a draft that is not live: its pages wait, nothing runs or learns for it."""
    d = get(name) if name else None
    return d is not None and d.state != "live"


def waiting_names() -> list[str]:
    return [d.name for d in list_all() if d.state != "live"]


def mark_live(name: str) -> None:
    """Called when a version of `name` is deployed (control.ops.deploy)."""
    d = get(name)
    if d is not None and d.state != "live":
        update(name, state="live")


def state_of(name: str, active_version: int | None) -> str:
    d = get(name)
    if d is not None and d.state != "live" and active_version is None:
        return d.state
    return "live"


def annotate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`GET /api/integrations`: `state` on every row, and the drafts that have no version yet."""
    out = [{**r, "state": state_of(r["name"], r["active_version"])} for r in rows]
    seen = {r["name"] for r in rows}
    for d in list_all():
        if d.name not in seen and d.state != "live":
            out.append({"name": d.name, "active_version": None, "latest_version": 0, "open_incidents": 0,
                        "state": d.state})
    return out


# ---- delete --------------------------------------------------------------------------------------

def _thread_files(name: str, thread_id: str | None) -> list[Path]:
    root = workspace_dir() / "agent_threads"
    if not root.is_dir():
        return []
    needles = (f'"integration": "{name}"', f'"name": "{name}"')
    out = []
    for p in root.glob("*.jsonl"):
        if p.stem == thread_id:
            out.append(p)
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        if any(n in text for n in needles):
            out.append(p)
    return out


def _stored_destination_rows(store: Store, name: str) -> list[str]:
    """Destination record ids Patchwork stored for `name` (in its run records), newest first, unique."""
    seen: dict[str, None] = {}
    for r in store.db.execute("SELECT r.detail, r.sink_body FROM records r JOIN runs u ON u.id = r.run_id "
                              "WHERE u.integration = ? AND u.dry_run = 0 ORDER BY r.run_id DESC", (name,)):
        for text in (r["detail"] or "", r["sink_body"] or ""):
            for rid in RECORD_ID.findall(text):
                seen.setdefault(rid)
    return list(seen)


def _delete_airtable_rows(dest: dict[str, Any], ids: list[str]) -> int:
    import httpx

    from runner import airtable, credentials as cred

    headers = cred.headers({"scheme": "bearer", "credential": dest.get("credential") or "AIRTABLE_TOKEN"})
    deleted = 0
    with httpx.Client(timeout=15) as client:
        for rid in ids:
            url = f"{airtable.api_url()}/{dest['base_id']}/{airtable._table_path(dest['table'])}/{rid}"
            resp = client.delete(url, headers=headers)
            if resp.status_code < 300:
                deleted += 1
    return deleted


def _spec_destination(name: str, store: Store) -> dict[str, Any] | None:
    from control import autorun

    v = store.active_version(name)
    spec = autorun.version_spec(name, v) if v is not None else None
    sink = (spec or {}).get("sink") or {}
    if sink.get("kind") == "airtable":
        return {"kind": "airtable", "base_id": sink.get("base_id"), "table": sink.get("table"),
                "credential": (sink.get("auth") or {}).get("credential")}
    return None


def delete_integration(name: str, *, delete_rows: bool) -> dict[str, Any]:
    """Remove `name` completely. With `delete_rows`, also delete the destination rows whose record ids
    Patchwork stored (never any other row). Credentials are kept."""
    from control.pages import PageInbox

    draft = get(name)
    store = Store()
    removed: list[str] = []
    rows_deleted = 0
    try:
        if delete_rows:
            dest = (draft.destination if draft else None) or _spec_destination(name, store)
            ids = _stored_destination_rows(store, name)
            if ids and dest and dest.get("kind") == "airtable":
                rows_deleted = _delete_airtable_rows(dest, ids)
        run_ids = [r["id"] for r in store.db.execute("SELECT id FROM runs WHERE integration = ?", (name,))]
        for chunk in range(0, len(run_ids), 500):
            part = run_ids[chunk:chunk + 500]
            store.db.execute(f"DELETE FROM records WHERE run_id IN ({','.join('?' * len(part))})", part)
        for table, col in (("runs", "integration"), ("versions", "name"), ("processed", "integration"),
                           ("cursors", "integration"), ("incidents", "integration"), ("integrations", "name")):
            n = store.db.execute(f"DELETE FROM {table} WHERE {col} = ?", (name,)).rowcount
            if n:
                removed.append(table)
    finally:
        store.close()
    if PageInbox().clear(name, include_untagged=False):
        removed.append("pages")
    for p in _thread_files(name, draft.thread_id if draft else None):
        p.unlink(missing_ok=True)
        removed.append(f"thread {p.stem}")
    ws = workspace_dir()
    for path, label in ((ws / "integrations" / name, "versions on disk"),
                        (ws / "recordings" / f"draft-{name}", "recording")):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(label)
    if draft is not None:
        _path(name).unlink(missing_ok=True)
        removed.append("draft")
    return {"name": name, "removed": removed, "rows_deleted": rows_deleted}
