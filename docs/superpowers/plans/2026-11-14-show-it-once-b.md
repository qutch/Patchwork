# Show It Once (Approach B, backup): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The New integration wizard has a second way to start, "Show it once". The person does the task once in a
recorded browser: opens a page, copies values into a form, and submits. Patchwork turns that into the same review
step as approach A. v1's app-to-app recording starts here too.

**Architecture:** Nothing new on the AI side. The existing pieces are already wired together:
- the recorder (`capture/`)
- the page → form matcher (`matcher/pages.py`, which produced `fixtures/recording_jobs`)
- the v1 API → API matcher (`matcher/core.py`)
- TEACH on recordings

This plan does four things:
- fixes the candidates endpoint for live page recordings;
- binds a recording to a draft integration;
- moves the Record UI into the wizard and feeds its candidates into Plan A's review table;
- proves one live recording on the demo Google Form before it is used on stage.

**Tech Stack:** Python 3.12, FastAPI, Playwright recorder (`capture/`), React + TypeScript (`web/`).

**Spec:** `docs/superpowers/specs/2026-11-14-new-integration-flow-design.md`, sections 3 and "Calls made by the
lead".

**Depends on:** Plan A Task 2 (drafts), Task 4 (`write_teach_inputs`, `POST /api/integrations/{name}/teach`),
Task 5 (`request_deploy` `review`) and Task 11 (`NewIntegration.tsx`, `ReviewTable.tsx`).

## Global Constraints

Plan A's Global Constraints apply unchanged, and so do these:
- **Destinations:** B is limited to form destinations (Google Form, the demo form, and plain HTML forms since
  PR #1) and to v1 API → API. Airtable and SQLite are not recorded.
- **Demo:** B goes on stage only after Task 4 passes on the owner's demo Google Form. Otherwise the run sheet keeps
  approach A.
- **Recorder:** recordings never hold secret values; the existing redaction in `capture/` stays on.

## Review Focus

1. **A recording where the person typed a value instead of copying it** (an email, a note): expect an `ask_user`
   "You typed X into Y. Send that on every row?", as for `fixture-jobs` today, never a silent constant (tested in
   Task 2).
2. **A recording stopped before the form was submitted:** expect a clear "No submit seen: submit the form once, then
   stop" in the wizard, with no build started (tested in Task 1).
3. **A recording whose page and form are both on allowlisted hosts but in separate tabs:** the matcher's page
   snapshot is still found (tested in Task 1 with two pages).
4. **v1's "Use fixture recording":** still builds `po_to_stockroom` from the wizard, with the v1 bench catching
   2/2 breaks (tested in Task 3).
5. **Recording a destination Show it once can't record** (Airtable): the option is disabled with "Show it once
   works with forms; for Airtable use Describe and send an example" (tested in Task 3).

---

### Task 1: Live page recordings produce candidates through the API

**Files:**
- Modify: `control/api/recordings.py`. `candidates()` passes `base_dir=recording_dir(recording_id)` to
  `match()`, so page snapshots are found. A recording with no submit returns 422 with the message from Review
  Focus 2.
- Test: `tests/capture/test_api_page_recording.py`

**Interfaces:**
- Consumes: `tests/capture/jobs_demo.py`, which records headless through the real recorder, and the
  `capture.session` lifecycle.
- Produces: `POST /api/recordings/{id}/candidates` returns V11 page → form candidates for a live recording, the same
  shape as `fixtures/recording_jobs/candidates.json`.

- [ ] **Step 1: Write the failing tests**

```python
def test_live_page_recording_candidates_via_api(client, recorded_jobs_demo):
    rid = recorded_jobs_demo  # a recording dir written by jobs_demo under the test workspace
    c = client.post(f"/api/recordings/{rid}/candidates").json()
    assert c["source"]["kind"] == "page" and c["sink"]["kind"] == "form"
    assert {m["dest_field"] for m in c["mappings"]} >= {"Job Title", "Company", "Link"}

def test_recording_without_submit_is_422(client, recording_without_submit):
    r = client.post(f"/api/recordings/{recording_without_submit}/candidates")
    assert r.status_code == 422 and "No submit seen" in r.json()["detail"]
```

- [ ] **Step 2: Run them and confirm they fail.** Run `.venv/bin/python -m pytest tests/capture/test_api_page_recording.py -q`.
  Expected: the first FAILS on missing snapshots (or the source is None), the second on a 500.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/capture tests/matcher -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Recordings: live page recordings get candidates through the API`.

### Task 2: A recording becomes a draft's teach input

**Files:**
- Modify: `control/drafts.py` and `control/api/drafts.py` (from Plan A)
- Test: `tests/control/test_draft_show_once.py`

**Interfaces:**
- Consumes: Plan A `control.drafts.create`, `POST /api/integrations/{name}/teach`.
- Produces:
  - `POST /api/integrations/{name}/teach {recording_id}`, an alternative to `{page_ids}`. It refuses with 422 when
    the recording's candidates aren't page → form or API → API.
  - It saves the recording's form as the draft's destination (`{kind:"form", url}`) and starts the existing
    BUILD on that recording (`teach_mode` absent, so it uses today's TEACH path with mappings).
  - The resulting `request_deploy` carries the same `review` rows as Plan A Task 5. Each row has
    `ai_proposed: false` and `source` = what the person did ("copied from h1", "typed").
- Change: `request_deploy`'s `review` builder in `agents/deep_tools.py` also handles recorded mappings.

- [ ] **Step 1: Write the failing tests**

```python
def test_teach_from_recording_reaches_review(client, scripted_agent, recorded_jobs_demo):
    client.post("/api/integrations/new", json={"intent": "Track things I apply to", "name": "shown_once"})
    tid = client.post("/api/integrations/shown_once/teach", json={"recording_id": recorded_jobs_demo}).json()["thread_id"]
    ev = wait_for_interrupt(scripted_agent, tid, "ask_user")
    assert "You typed" in ev["question"]  # the typed email is confirmed, never a silent constant
    scripted_agent.resume(tid, ev["interrupt_id"], {"answer": ev["options"][0]})
    dep = wait_for_interrupt(scripted_agent, tid, "request_deploy")
    rows = {r["dest_field"]: r for r in dep["review"]}
    assert rows["Job Title"]["ai_proposed"] is False and rows["Job Title"]["status"] == "checked"

def test_airtable_recording_refused(client, recording_api_to_airtable):
    client.post("/api/integrations/new", json={"intent": "x y", "name": "nope"})
    r = client.post("/api/integrations/nope/teach", json={"recording_id": recording_api_to_airtable})
    assert r.status_code == 422
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control tests/agents -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Drafts: teach from a recording (show it once) into the same review`.

### Task 3: Web: "Show it once" in the wizard; v1 starts there

**Files:**
- Create: `web/src/pages/ShowOnce.tsx` (the Record tab's live view, reused), `web/src/pages/ShowOnce.test.tsx`
- Modify:
  - `web/src/pages/NewIntegration.tsx`: step 1 gets the choice "Describe and send an example" | "Show it once".
  - `web/src/routes.ts`: `#record` → `{page:"new", mode:"show"}`.
  - `web/src/tabs/Record.tsx`: becomes a thin wrapper, or is deleted once nothing imports it.
  - `web/mock/v3.mjs`
  - `web/README.md`: the v1 click path now starts at New integration → Show it once → "Use fixture recording".

**Interfaces:**
- Consumes: today's `api.startRecording`, `api.stopRecording`, `streams.recording`, `api.findCandidates`; Task 2's
  teach-from-recording.
- Produces: "Show it once" takes the intent, a start URL and the allowlist (prefilled from the form host), then
  Start, the live event list, and Stop.
  - **Page → form recording:** goes to the Review step (Plan A's `ReviewTable`).
  - **v1 API → API recording:** goes to today's Review view (mappings), then Build, as before.
  - **"Use fixture recording":** stays.
  - **Destination is Airtable or SQLite:** the option is disabled, with the Review Focus 5 message.

- [ ] **Step 1: Write the failing tests** with the mock:
  - `#record` opens Show it once.
  - "Use fixture recording" builds `po_to_stockroom` (the mock thread starts).
  - A page → form recording lands on the review table with "copied" sources.
  - The option is disabled for an Airtable draft.
- [ ] **Step 2: Run them and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the checks and confirm they pass.** Run
  `cd web && npm run -s typecheck && npx vitest run && npm run -s build && node mock/smoke.mjs`, then the v1 bench
  (2/2 breaks caught). Expected: PASS.
- [ ] **Step 5: Commit** with message `Web: Show it once in the New integration wizard; v1 starts there`.

### Task 4: A live rehearsal on the demo Google Form (decision gate)

**Files:**
- Modify: `docs/demo-v2.md` (a "Backup beat: Show it once" section, written only if this passes)

- [ ] **Step 1:** On a separate workspace and a free port, with the owner at the keyboard:
  1. New integration → Show it once.
  2. Start URL: a real page from the teammate's `fixtures/real_products/`.
  3. Form: the owner's demo Google Form (`DEMO_FORM_URL` in the Makefile).
  4. Copy two values, type one, submit, stop.

  Expected:
  - the review table shows the copied fields as `checked`;
  - the typed one is asked about;
  - Approve writes one row to the demo Sheet (`DEMO_SHEET_CSV`);
  - the whole beat takes under 60 s.
- [ ] **Step 2:** Do the beat 3 times in a row.
  - **Pass:** 3/3. Write the backup beat into `docs/demo-v2.md` with its timing.
  - **Fail:** any failure. Record the failure in `docs/demo-v2.md` under "Not on stage" and keep approach A
    only.
- [ ] **Step 3: Commit** with message `Demo: Show it once rehearsal result (backup beat)`.
