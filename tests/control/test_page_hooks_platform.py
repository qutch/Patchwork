"""One learn at a time per multi-tenant platform: two Workday companies held together start one learn, and the
platform parser it saves reads the other company on replay (control/page_hooks.py)."""

from control.page_hooks import PageHooks


class _Inbox:
    def __init__(self, pages):
        self.pages, self._learns = pages, {}

    def learns(self, name):
        return dict(self._learns)

    def get(self, pid):
        return self.pages.get(pid)

    def add_learn(self, pid, tid, site, name):
        self._learns[pid] = {"page_id": pid, "thread_id": tid, "site": site, "integration": name}


class _Agent:
    def __init__(self):
        self.started = []

    def start_learn(self, pid, name):
        self.started.append(pid)
        return f"learn-{pid}"

    def status(self, tid):
        return {"state": "running"}


def test_one_learn_per_platform_at_a_time():
    pages = {"pg-1": {"site": "tmobile.wd1.myworkdayjobs.com"}, "pg-2": {"site": "visa.wd5.myworkdayjobs.com"},
             "pg-3": {"site": "jobs.smartrecruiters.com"}}
    agent = _Agent()
    hooks = PageHooks(_Inbox(pages), auto=None, ai_enabled=lambda: True, get_agent=lambda: agent)
    assert hooks.maybe_learn("jobs_to_tracker", ["pg-1", "pg-2", "pg-3"]) == ["learn-pg-1", "learn-pg-3"]
    assert agent.started == ["pg-1", "pg-3"]  # visa waits: the Workday learn may cover it
