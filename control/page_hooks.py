"""What the control plane does around page-integration runs (docs/contracts.md V9):

- a run of a page integration finished → for each `PARSE_NO_MATCH` page: if AI is on, the agent
  has `start_learn`, no learn thread is active for that page's `site` and this page never had one,
  `agent.start_learn(page_id, integration)`; otherwise the row stays `held`.
- a learn or repair thread emitted `deployed` for a page integration → once that thread has
  finished (a REPAIR thread replays by itself first), replay quarantined records, then run once
  more (pages blocked by preflight were never fetched past the cursor).

`CONTRACT_CHANGED` repairs still start from a person's click, as in v1.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from control.autorun import AutoRunner, default_page_integration, is_page_integration, is_page_spec, version_spec
from runner.html import platform_of
from runner.store import Store

log = logging.getLogger("patchwork.pages")

ACTIVE = ("running", "waiting")
DEPLOY_WAIT_S = 120


class PageHooks:
    def __init__(self, inbox: Any, auto: AutoRunner, ai_enabled: Callable[[], bool],
                 get_agent: Callable[[], Any]):
        self.inbox, self.auto = inbox, auto
        self.ai_enabled, self.get_agent = ai_enabled, get_agent
        self._lock = threading.Lock()
        self._attached: set[int] = set()

    # ---- agent events --------------------------------------------------------------------------
    def attach(self, agent: Any) -> None:
        """Subscribe to the agent's events (ThreadedAgentService.subscribe). Idempotent."""
        if agent is None or id(agent) in self._attached or not hasattr(agent, "subscribe"):
            return
        self._attached.add(id(agent))
        agent.subscribe(self.on_agent_event)

    def on_agent_event(self, ev: dict[str, Any]) -> None:
        if ev.get("type") != "deployed":
            return
        tid, name = str(ev.get("thread_id") or ""), ev.get("name")
        if not name or not (tid.startswith(("learn-", "repair-")) or self._is_draft_build(name, tid)):
            return
        threading.Thread(target=self._after_deploy, args=(name, tid), daemon=True,
                         name=f"after-deploy-{name}").start()

    @staticmethod
    def _is_draft_build(name: str, tid: str) -> bool:
        """The BUILD thread the wizard started for a draft (`drafts.thread_id`): once it deploys, the example
        pages get their first run."""
        from control import drafts

        d = drafts.get(name) if drafts.NAME.match(str(name)) else None
        return tid.startswith("build-") and d is not None and d.thread_id == tid

    def _after_deploy(self, name: str, thread_id: str) -> None:
        try:
            if not is_page_integration(name):
                return
            agent = self.get_agent()
            deadline = time.monotonic() + DEPLOY_WAIT_S
            while agent is not None and time.monotonic() < deadline:  # let the thread finish its own work
                try:
                    if agent.status(thread_id).get("state") not in ACTIVE:
                        break
                except KeyError:
                    break
                time.sleep(0.05)
            self.auto.request(name, ("run",) if thread_id.startswith("build-") else ("replay", "run"))
        except Exception:  # noqa: BLE001
            log.exception("replay after deploy of %s failed", name)

    # ---- runs ------------------------------------------------------------------------------------
    def on_run_finished(self, run: dict[str, Any]) -> list[str]:
        """RunManager listener. Returns the learn thread ids it started."""
        if run.get("dry_run"):
            return []
        name = run["integration"]
        if not is_page_spec(version_spec(name, run["version"])):
            return []
        store = Store()
        try:
            page_ids = [r["record_id"] for r in store.db.execute(
                "SELECT record_id FROM records WHERE run_id = ? AND status = 'quarantined' AND reason = 'PARSE_NO_MATCH' "
                "ORDER BY record_id", (run["id"],))]
        finally:
            store.close()
        return self.maybe_learn(name, page_ids)

    def _site_active(self, agent: Any, learns: dict[str, dict], site: str) -> bool:
        for learn in learns.values():
            if learn["site"] != site:
                continue
            try:
                if agent.status(learn["thread_id"]).get("state") in ACTIVE:
                    return True
            except KeyError:
                continue
        return False

    def maybe_learn(self, name: str, page_ids: list[str]) -> list[str]:
        from control import drafts

        if drafts.is_waiting(name):
            return []  # a wizard draft is not live: its pages wait until it is taught and deployed
        if not page_ids or not self.ai_enabled():
            return []  # AI off: the rows stay held (PARSE_NO_MATCH); turning AI on and replaying learns them
        agent = self.get_agent()
        if agent is None or not hasattr(agent, "start_learn"):
            return []
        started = []
        default = default_page_integration()
        with self._lock:  # one decision at a time, so two finishing runs can't both start a site's learn
            learns = self.inbox.learns(name)
            for pid in page_ids:
                if pid in learns:
                    continue  # a page is learned from at most once
                page = self.inbox.get(pid)
                if page is None:
                    continue
                if (page.get("integration") or default or name) != name:
                    continue  # V15: a page is learned only for the integration it was sent to
                site = page["site"]
                if self._site_active(agent, learns, site) or any(  # one learn per platform at a time
                        self._site_active(agent, learns, s) for s in {x["site"] for x in learns.values()}
                        if platform_of(site) and platform_of(s) == platform_of(site)):
                    continue
                try:
                    tid = agent.start_learn(pid, name)
                except Exception:  # noqa: BLE001 - the model being down must not break the run watcher
                    log.exception("start_learn(%s) failed", pid)
                    continue
                self.inbox.add_learn(pid, tid, site, name)
                learns[pid] = {"page_id": pid, "thread_id": tid, "site": site, "integration": name}
                started.append(tid)
        return started
