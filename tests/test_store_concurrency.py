"""Opening a fresh DB from many threads/processes at once must not fail on the column migrations."""

import subprocess
import sys
import threading

from runner.store import Store


def test_concurrent_store_opens_on_a_fresh_db(workspace):
    errors: list[BaseException] = []
    barrier = threading.Barrier(16)

    def open_store():
        try:
            barrier.wait()
            s = Store()
            s.close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_store) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    cols = {r["name"] for r in Store().db.execute("PRAGMA table_info(runs)")}
    assert {"kind", "pid"} <= cols


def test_concurrent_processes_on_a_fresh_db(workspace):
    code = "from runner.store import Store; Store().close()"
    procs = [subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True) for _ in range(8)]
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
