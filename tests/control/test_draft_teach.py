"""Teach inputs from a draft (wizard, Task 4): a synthetic recording with the example pages, the same BUILD,
one teach per draft, and the first run once the taught integration deploys."""

from __future__ import annotations

import json
import threading
import time

import pytest

from agents.scripted import ScriptedAgentService
from control import drafts, ops
from control.api.recordings import is_page_form, recording_dir
from demo_apps.tracker_form.app import create_app as form_app
from fixtures.products.pages import NAME as PRODUCT
from runner.store import workspace_dir
from tests.conftest import _Server
from tests.control.test_pages_routing import post_product, product_spec
from tests.control.v2_fakes import fake_runner, make_page_integration, records_by_page, v2api, wait_idle  # noqa: F401

QUESTIONS = [{"label": "Product", "required": True}, {"label": "Price"}, {"label": "Store"},
             {"label": "Link"}, {"label": "Date Added"}]


class FakeBuildAgent(ScriptedAgentService):
    """Records the builds it is asked for and runs a stand-in BUILD thread: it stays active until `gate` is
    set (then it ends without deploying), or fails, or asks for a deploy approval, per `mode`."""

    def __init__(self):
        super().__init__(pace_s=0)
        self.builds: list[tuple[str, str | None]] = []
        self.gate = threading.Event()
        self.mode = "wait"

    def start_build(self, recording_id, name=None):
        self.builds.append((recording_id, name))

        def body(t):
            if self.mode in ("deploy_request", "deploy_revise"):
                answer = self.interrupt(t, "request_deploy", name=name, version=1, summary="x", spec_diff="",
                                      code_diff="", tests={"passed": True, "summary": "ok"})
                if self.mode == "deploy_revise":  # like the real agent: revises after a rejection
                    self.gate.wait(15)
                return answer
            self.gate.wait(15)
            if self.mode == "fail":
                raise RuntimeError("model unavailable")
            return {"deployed": False}

        return self._start("build", recording_id, body, integration=name)


@pytest.fixture
def v2agent(workspace):  # overrides the v2_fakes fixture that v2api uses
    agent = FakeBuildAgent()
    yield agent
    agent.gate.set()


@pytest.fixture
def fake_agent(v2agent):
    return v2agent


@pytest.fixture
def client(v2api):
    return v2api


@pytest.fixture
def form():
    with _Server(form_app(questions=QUESTIONS)) as srv:
        yield srv.url + "/"


@pytest.fixture
def products_draft_with_pages(client, form):
    name = client.post("/api/integrations/new", json={"intent": "Track product prices"}).json()["name"]
    r = client.put(f"/api/integrations/{name}/destination", json={"kind": "form", "url": form})
    assert r.status_code == 200, r.text
    pids = [post_product(client, f, integration=name)["id"] for f in ("shop-a.html", "shop-b.html")]
    return name, pids


@pytest.fixture
def draft_with_pages_no_destination(client):
    name = client.post("/api/integrations/new", json={"intent": "Track product prices"}).json()["name"]
    return name, [post_product(client, integration=name)["id"]]


def test_teach_writes_examples_candidates(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    tid = client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).json()["thread_id"]
    c = json.loads((recording_dir(f"draft-{name}") / "candidates.json").read_text())
    assert c["teach_mode"] == "examples" and c["mappings"] == [] and len(c["examples"]) == len(pids)
    assert [f["label"] for f in c["sink"]["questions"]] == ["Product", "Price", "Store", "Link", "Date Added"]
    assert c["sink"]["questions"][0]["required"] is True and c["sink"]["labels"][0] == "Product"
    assert fake_agent.builds == [(f"draft-{name}", name)] and tid
    # the shape the agent reads
    assert c["recording_id"] == f"draft-{name}" and c["intent"] == "Track product prices"
    assert c["source"]["kind"] == "page" and c["source"]["snapshot"] == "snapshots/1.html"
    assert c["examples"][0]["page_id"] == pids[0] and c["examples"][1]["snapshot"] == "snapshots/2.html"
    assert c["destination"]["kind"] == "form" and c["unexplained"] == []
    rec = json.loads((recording_dir(f"draft-{name}") / "recording.json").read_text())
    assert rec["events"] == [] and rec["id"] == f"draft-{name}" and rec["start_url"] == c["source"]["url"]
    assert "Bolt Desk Lamp" in (recording_dir(f"draft-{name}") / "snapshots" / "2.html").read_text()
    assert is_page_form(f"draft-{name}")
    d = client.get(f"/api/integrations/{name}/draft").json()
    assert d["state"] == "teaching" and d["thread_id"] == tid and d["example_page_ids"] == pids
    assert [i["state"] for i in client.get("/api/integrations").json() if i["name"] == name] == ["teaching"]


def test_teach_twice_is_409(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids})
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).status_code == 409
    assert len(fake_agent.builds) == 1


def test_teach_without_destination_is_422(client, draft_with_pages_no_destination):
    name, pids = draft_with_pages_no_destination
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).status_code == 422


def test_teach_without_pages_or_with_foreign_pages_is_422(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": []}).status_code == 422
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": ["pg-999999"]}).status_code == 422
    assert fake_agent.builds == [] and client.get(f"/api/integrations/{name}/draft").json()["state"] == "draft"


def test_teach_with_ai_off_is_409_and_leaves_the_draft(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    client.put("/api/settings", json={"ai_enabled": False})
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).status_code == 409
    assert client.get(f"/api/integrations/{name}/draft").json()["state"] == "draft"


def test_unknown_draft_is_404(client):
    assert client.post("/api/integrations/nope/teach", json={"page_ids": ["pg-000001"]}).status_code == 404


def test_deploying_the_taught_integration_makes_it_live_and_runs_once(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    tid = client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).json()["thread_id"]
    fake_agent.gate.set()
    assert records_by_page(name) == {}  # waiting: nothing ran for the example pages
    make_page_integration(name=name, spec=product_spec(), deploy=False)
    ops.deploy(name, 1)
    assert drafts.get(name).state == "live"
    client.app.state.hooks.on_agent_event({"type": "deployed", "thread_id": tid, "name": name})
    wait_idle(client, name)
    assert set(records_by_page(name)) == set(pids)
    assert [i["state"] for i in client.get("/api/integrations").json() if i["name"] == name] == ["live"]


def teach(client, name, pids):
    r = client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids})
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


def wait_ended(agent, tid):
    deadline = time.time() + 10
    while time.time() < deadline and agent.status(tid)["state"] in ("running", "waiting"):
        time.sleep(0.02)
    assert agent.status(tid)["state"] not in ("running", "waiting")


@pytest.mark.parametrize("mode", ["wait", "fail"])
def test_ended_build_returns_the_draft_to_draft_and_can_be_taught_again(client, products_draft_with_pages,
                                                                         fake_agent, mode):
    name, pids = products_draft_with_pages
    fake_agent.mode = mode
    tid = teach(client, name, pids)
    d = client.get(f"/api/integrations/{name}/draft").json()
    assert d["state"] == "teaching"  # still active: stays teaching, and a second teach is refused
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).status_code == 409
    fake_agent.gate.set()
    wait_ended(fake_agent, tid)
    d = client.get(f"/api/integrations/{name}/draft").json()
    assert d["state"] == "draft" and d["destination"]["kind"] == "form" and d["example_page_ids"] == pids
    fake_agent.gate.clear()
    teach(client, name, pids)  # /teach works again
    assert len(fake_agent.builds) == 2


def test_teach_itself_recovers_a_stale_teaching_draft(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    tid = teach(client, name, pids)
    fake_agent.gate.set()
    wait_ended(fake_agent, tid)
    fake_agent.gate.clear()
    teach(client, name, pids)  # no GET in between


def test_rejected_deploy_request_returns_the_draft_to_draft(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    fake_agent.mode = "deploy_request"
    tid = teach(client, name, pids)
    deadline = time.time() + 10
    while time.time() < deadline and fake_agent.status(tid)["state"] != "waiting":
        time.sleep(0.02)
    pending = fake_agent.status(tid)["pending"]
    assert drafts.get(name).state == "teaching"
    r = client.post(f"/api/agent/{tid}/resume", json={"interrupt_id": pending["interrupt_id"],
                                                       "decision": "reject", "comment": "no"})
    assert r.status_code == 200, r.text
    assert drafts.get(name).state == "draft"


def test_teach_is_refused_while_the_thread_revises_after_a_rejection(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    fake_agent.mode = "deploy_revise"
    tid = teach(client, name, pids)
    deadline = time.time() + 10
    while time.time() < deadline and fake_agent.status(tid)["state"] != "waiting":
        time.sleep(0.02)
    pending = fake_agent.status(tid)["pending"]
    client.post(f"/api/agent/{tid}/resume", json={"interrupt_id": pending["interrupt_id"], "decision": "reject"})
    assert drafts.get(name).state == "draft" and fake_agent.status(tid)["state"] == "running"
    r = client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids})
    assert r.status_code == 409 and "is still being taught" in r.json()["detail"]
    assert len(fake_agent.builds) == 1
    fake_agent.gate.set()
