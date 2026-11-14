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
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from runner.store import Store, workspace_dir

NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")  # \Z: a trailing newline is not a name


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

class DeleteRowsUnsupported(DraftError):
    """`delete_rows` was asked for: Patchwork doesn't store the destination's record ids, so it can't."""


DELETE_ROWS_MESSAGE = ("Deleting destination rows isn't supported yet: Patchwork doesn't store the destination's "
                       "record ids. Delete them in the destination.")


def _thread_ids(name: str, draft: Draft | None, live_threads: Iterable[dict[str, Any]]) -> set[str]:
    """Agent threads that belong to `name` by exact identity: the draft's own thread, learn threads the page
    inbox links to it, threads the agent service reports for it, and thread files whose `deployed` event
    names it exactly. Never a substring match on a thread's contents."""
    from control.pages import PageInbox

    ids: set[str] = set()
    if draft is not None and draft.thread_id:
        ids.add(draft.thread_id)
    ids.update(l["thread_id"] for l in PageInbox().learns(name).values())
    ids.update(t["thread_id"] for t in live_threads if t.get("integration") == name and t.get("thread_id"))
    root = workspace_dir() / "agent_threads"
    for p in root.glob("*.jsonl") if root.is_dir() else []:
        try:
            for line in p.read_text().splitlines():
                ev = json.loads(line)
                if ev.get("type") == "deployed" and ev.get("name") == name:
                    ids.add(p.stem)
                    break
        except (OSError, ValueError):
            continue
    return ids


def delete_integration(name: str, *, delete_rows: bool = False,
                       live_threads: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """Remove `name` completely: draft, versions, runs, records, pages, learn links, its agent threads and its
    synthetic recording. Credentials are kept. Destination rows are never deleted: `delete_rows=True` raises
    DeleteRowsUnsupported before anything is removed. `live_threads`: the agent service's `list_threads()`."""
    from control.pages import PageInbox

    if delete_rows:
        raise DeleteRowsUnsupported(DELETE_ROWS_MESSAGE)
    draft = get(name)
    thread_ids = _thread_ids(name, draft, live_threads)
    store = Store()
    removed: list[str] = []
    try:
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
    for tid in sorted(thread_ids):
        f = workspace_dir() / "agent_threads" / f"{tid}.jsonl"
        if f.is_file():
            f.unlink(missing_ok=True)
            removed.append(f"thread {tid}")
    ws = workspace_dir()
    for path, label in ((ws / "integrations" / name, "versions on disk"),
                        (ws / "recordings" / f"draft-{name}", "recording")):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(label)
    if draft is not None:
        _path(name).unlink(missing_ok=True)
        removed.append("draft")
    return {"name": name, "removed": removed, "rows_deleted": 0}


# ---- a teaching draft that did not end in a deploy ------------------------------------------------

ACTIVE_STATES = ("running", "waiting")


def thread_active(svc: Any | None, thread_id: str | None) -> bool:
    """True while the agent thread is running or waiting. No service, or a thread it doesn't know, is ended."""
    if svc is None or not thread_id:
        return False
    try:
        return svc.status(thread_id).get("state") in ACTIVE_STATES
    except KeyError:
        return False


def reconcile(name: str, svc: Any | None) -> Draft | None:
    """A `teaching` draft whose build thread ended without a deployed version goes back to `draft`
    (destination and examples kept), so it can be taught again."""
    d = get(name)
    if d is None or d.state != "teaching" or thread_active(svc, d.thread_id):
        return d
    store = Store()
    try:
        deployed = store.active_version(name) is not None
    finally:
        store.close()
    if deployed:
        mark_live(name)
        return get(name)
    return update(name, state="draft")


def release_rejected(thread_id: str) -> None:
    """A rejected request_deploy: the draft whose build thread it was returns to `draft`."""
    for d in list_all():
        if d.state == "teaching" and d.thread_id == thread_id:
            update(d.name, state="draft")


# ---- teach from example pages --------------------------------------------------------------------

def _input_kind(field: dict[str, Any]) -> str:
    if field.get("options"):
        return "select"
    fmt, typ = field.get("format"), field.get("type")
    if fmt == "email":
        return "email"
    if fmt in ("date", "date-time"):
        return "date"
    if typ in ("number", "integer"):
        return "number"
    if typ == "boolean":
        return "checkbox"
    return "text"


def write_teach_inputs(name: str, page_ids: list[str]) -> str:
    """Write the synthetic recording TEACH builds from: `recordings/draft-<name>/{recording.json,
    snapshots/<n>.html, candidates.json}`. Returns the recording id `draft-<name>`.

    candidates.json: `teach_mode: "examples"`, `source` = the first example page, `examples` = every page, `sink`
    = the draft's destination plus its fields as `labels` and `questions`, `mappings: []` (the agent
    proposes them) and `unexplained: []`. Raises KeyError (no such draft), DraftError (no destination, no
    example page, or a page that was not sent to this draft) and DestinationError (the destination no longer
    checks out)."""
    from control import destinations
    from control.api.recordings import recording_dir
    from control.pages import PageInbox

    draft = get(name)
    if draft is None:
        raise KeyError(name)
    if not draft.destination:
        raise DraftError("choose a destination first")
    if not page_ids:
        raise DraftError("send at least one example page first")
    inbox = PageInbox()
    pages = []
    for pid in dict.fromkeys(page_ids):
        page = inbox.get(pid)
        if page is None:
            raise DraftError(f"example page {pid} was not found")
        if page.get("integration") != name:
            raise DraftError(f"example page {pid} was not sent to {name}")
        pages.append(page)
    checked = destinations.check(draft.destination)

    rid = f"draft-{name}"
    root = recording_dir(rid)
    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    examples = []
    for n, page in enumerate(pages, 1):
        (root / "snapshots" / f"{n}.html").write_text(page["html"] or "")
        examples.append({"page_id": page["id"], "site": page["site"], "url": page["url"],
                         "canonical_url": page["canonical_url"], "title": page["title"],
                         "snapshot": f"snapshots/{n}.html"})
    first = examples[0]
    questions = [{"label": f["name"], "required": bool(f.get("required")), "input": _input_kind(f),
                  **({"options": f["options"]} if f.get("options") else {})} for f in checked["fields"]]
    sink = {**draft.destination, "title": checked["title"], "labels": [q["label"] for q in questions],
            "questions": questions}
    if draft.destination["kind"] == "form":
        sink.setdefault("submit", "Submit")
    candidates = {"recording_id": rid, "intent": draft.intent, "teach_mode": "examples",
                  "source": {"kind": "page", **{k: first[k] for k in ("site", "url", "canonical_url", "snapshot", "title")}},
                  "examples": examples, "sink": sink, "mappings": [], "unexplained": []}
    recording = {"id": rid, "intent": draft.intent, "events": [], "start_url": first["url"]}
    (root / "recording.json").write_text(json.dumps(recording, indent=2))
    (root / "candidates.json").write_text(json.dumps(candidates, indent=2))
    update(name, example_page_ids=[p["id"] for p in pages])
    return rid
