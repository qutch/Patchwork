"""Run on arrival (docs/contracts.md V9): at most one run per integration at a time, plus at most
one queued follow-up of each kind.

`AutoRunner.request(name)` starts a run now if the integration is idle, else queues one follow-up
(three hotkey presses during a run → exactly one more run, which reads all three pages from the
cursor). `request(name, ("replay", "run"))` queues a replay of quarantined records and then a run
(after a learned or repaired version is deployed). A run someone else started (`POST /api/runs`)
counts as busy: the follow-up starts as soon as it has finished.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import yaml

from control import ops
from runner.store import Store

log = logging.getLogger("patchwork.autorun")

PAGE_SOURCE = "patchwork://control"
BUSY_RETRY_S = 0.1
BUSY_GIVE_UP_S = ops.RUN_TIMEOUT_S + 60


class Busy(RuntimeError):
    """The integration has a run in progress that this AutoRunner didn't start."""


@dataclass
class _Lane:
    busy: bool = False
    pending: list[str] = field(default_factory=list)  # ordered, each kind at most once
    current: int | None = None
    worker: threading.Thread | None = None


class AutoRunner:
    def __init__(self, start: Callable[[str, str], int], wait: Callable[[int], None]):
        """`start(name, kind)` launches a run ("run" | "replay") and returns its id; it raises
        `Busy` if a run of `name` is already in progress. `wait(run_id)` blocks until it finished."""
        self._start = start
        self._wait = wait
        self._lock = threading.Lock()
        self._lanes: dict[str, _Lane] = {}
        self._generation = 0

    def request(self, name: str, kinds: Iterable[str] = ("run",)) -> dict[str, Any]:
        """Returns {run_id, queued}: the run started now, or queued=True (run_id None) if the
        integration was busy and a follow-up is queued (or was already queued)."""
        kinds = list(kinds)
        with self._lock:
            lane = self._lanes.setdefault(name, _Lane())
            if lane.busy:
                for k in kinds:
                    if k not in lane.pending:
                        lane.pending.append(k)
                return {"run_id": None, "queued": True}
            lane.busy, lane.pending = True, kinds[1:]
            gen = self._generation
        first = kinds[0]
        try:
            run_id: int | None = self._start(name, first)
        except Busy:
            with self._lock:  # someone else's run: go after it
                if first not in lane.pending:
                    lane.pending.insert(0, first)
            run_id = None
        except BaseException:
            with self._lock:
                lane.busy, lane.pending = False, []
            raise
        with self._lock:
            lane.current = run_id
            lane.worker = threading.Thread(target=self._drain, args=(name, lane, run_id, gen), daemon=True,
                                           name=f"autorun-{name}")
            lane.worker.start()
        return {"run_id": run_id, "queued": run_id is None}

    def _drain(self, name: str, lane: _Lane, run_id: int | None, gen: int) -> None:
        try:
            while True:
                if run_id is not None:
                    self._wait(run_id)
                with self._lock:
                    if gen != self._generation or not lane.pending:
                        lane.busy, lane.current = False, None
                        return
                    kind = lane.pending.pop(0)
                run_id = self._start_when_free(name, kind, gen)
                with self._lock:
                    lane.current = run_id
        except Exception:  # noqa: BLE001
            log.exception("auto-run of %s failed", name)
            with self._lock:
                lane.busy, lane.current, lane.pending = False, None, []

    def _start_when_free(self, name: str, kind: str, gen: int) -> int | None:
        deadline = time.monotonic() + BUSY_GIVE_UP_S
        while time.monotonic() < deadline and gen == self._generation:
            try:
                return self._start(name, kind)
            except Busy:
                time.sleep(BUSY_RETRY_S)
            except Exception as e:  # noqa: BLE001 - e.g. the runner failed before printing `started`
                log.warning("auto-run %s of %s could not start: %s", kind, name, e)
                return None
        return None

    def state(self, name: str) -> dict[str, Any]:
        with self._lock:
            lane = self._lanes.get(name) or _Lane()
            return {"busy": lane.busy, "pending": list(lane.pending), "current": lane.current}

    def forget(self, name: str | None = None) -> None:
        """Drop queued follow-ups (demo reset). Runs in progress are the RunManager's to stop."""
        with self._lock:
            self._generation += 1
            for n, lane in self._lanes.items():
                if name is None or n == name:
                    lane.pending = []
                    lane.busy, lane.current = False, None

    def idle(self, name: str, timeout: float = 30) -> bool:
        """Test/ops helper: wait until nothing runs or is queued for `name`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.state(name)["busy"]:
                return True
            time.sleep(0.05)
        return False


# ---- which integrations read the page inbox ---------------------------------------------------

_spec_cache: dict[tuple[str, int, float], dict] = {}


def version_spec(name: str, version: int) -> dict[str, Any] | None:
    """The compiled spec.yaml of a version as plain data (no spec models: works before and after W1)."""
    path = ops.version_dir(name, version) / "spec.yaml"
    try:
        key = (str(path), version, path.stat().st_mtime)
    except OSError:
        return None
    if key not in _spec_cache:
        try:
            _spec_cache[key] = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return None
    return _spec_cache[key]


def version_manifest(name: str, version: int) -> dict[str, Any]:
    import json
    try:
        return json.loads((ops.version_dir(name, version) / "manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def is_page_spec(spec: dict[str, Any] | None) -> bool:
    return bool(spec) and ((spec.get("source") or {}).get("base_url") == PAGE_SOURCE)


def is_page_integration(name: str, store: Store | None = None) -> bool:
    store = store or Store()
    v = store.active_version(name)
    return v is not None and is_page_spec(version_spec(name, v))


def page_integrations(store: Store | None = None) -> list[str]:
    """Integrations whose **active** spec reads `patchwork://control` (jobs_to_tracker first)."""
    store = store or Store()
    names = [i["name"] for i in store.integrations()
             if i["active_version"] is not None and is_page_spec(version_spec(i["name"], i["active_version"]))]
    return sorted(names, key=lambda n: (n != "jobs_to_tracker", n))
