"""Two live runs of one integration never overlap (runner.run takes a per-integration lock), so a record can
never be written twice: the idempotency check and the mark happen inside the same exclusive section.

Found by tests/airtable/test_airtable_agents.py::test_teach_into_airtable_then_run_rename_and_repair, where the
control plane's after-deploy run (a subprocess) overlapped the test's in-process run and both wrote the page."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from runner import run as run_mod
from runner.store import Store
from tests.test_runner import ACME, RunnerFake, _msg, deployed  # noqa: F401 - fixture


class SlowSink(RunnerFake):
    """Every sink write takes a while, so two unlocked runs would both pass the idempotency check."""

    def request(self, target, method, path, *, params=None, json=None):
        if target == "sink" and method != "GET":
            time.sleep(0.4)
        return super().request(target, method, path, params=params, json=json)


def test_overlapping_runs_write_a_record_once(monkeypatch, deployed):  # noqa: F811
    _, openapi = deployed
    fake = SlowSink(openapi, records=[_msg(1, "acme.com", ACME.format(n=1001))])
    monkeypatch.setattr(run_mod, "Http", lambda config, staging=False: fake)
    summaries: list[dict] = []
    barrier = threading.Barrier(2)

    def one_run():
        barrier.wait()
        summaries.append(run_mod.run("po_to_stockroom", store=Store(), log=lambda s: None))

    threads = [threading.Thread(target=one_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(fake.sink_writes) == 1, fake.sink_writes
    assert sorted(s["counts"].get("success", 0) for s in summaries) == [0, 1]


def test_the_lock_is_held_across_processes(workspace):
    code = ("import time\nfrom runner.run import run_lock\n"
            "with run_lock('po_to_stockroom', 'live'):\n    print('held', flush=True)\n    time.sleep(1.0)\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    t0 = time.monotonic()
    with run_mod.run_lock("po_to_stockroom", "live"):
        waited = time.monotonic() - t0
    p.wait(timeout=10)
    assert waited > 0.5  # this process waited for the other one's run to finish
    with run_mod.run_lock("po_to_stockroom", "staging"):  # a dry run does not wait for a live one
        pass
