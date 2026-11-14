# Patchwork v2: show it once, fill it in regardless of layout

Status: design for approval · 2026-11-14 · hackathon time left: ~24 h · demo slot: **2 minutes**
Tracks: **Practical AI** and **Business/Enterprise**.

## 1. Intent and why

The owner's goal: connect any two pieces of software, one with an output and one with an input, by showing the
transfer **once**, and have it automated from then on. What makes Patchwork special, in the owner's words:
**"being able to see it once and then fill it in regardless of layout."**

v1 (this branch) proves the engine on mock apps that both have JSON APIs. Judges will ask how it differs from a
browser agent, from Zapier, and from a script written by Claude. v2 answers with a product that:

- works on **real pages and real apps without APIs** (read the page you are on, fill the form you would fill);
- **learns each new layout once** (sometimes asking one question), then handles that layout with **no AI**;
- uses AI at runtime **only for what it hasn't learned yet**, every AI answer **checked by code and labelled**,
  and **compiles repeated patterns into code** so AI use falls toward zero over time;
- **stops rather than writes wrong data** when the destination changes, and proposes a tested fix for approval.

One line for the pitch: **"Code that learns. AI is the teacher, not the worker."**

How it differs (the slide): an AI agent pays for and risks AI on every record forever; a Claude script is code that
can't learn and fails silently; Patchwork is code that learns, holds anything it's unsure of for a person, and
shows AI calls per day trending to zero.

## 2. The demo (2 minutes) and the use case

Use case: **real job postings → the owner's job tracker** (a Google Sheet). Real data, no mock apps. The
enterprise framing of the same flow: a recruiting or sales team logging openings at target accounts, with
approvals and an audit trail.

| Time | Beat |
|---|---|
| 0:00–0:15 | Hook: "I track every job I apply to. Copying title, company and link is the boring part. An AI agent could do it, but it would read every page with AI forever. A Claude script breaks the first time a site looks different." |
| 0:15–0:35 | **Show it once** (pre-recorded teaching replayed fast, or done live): copy title, company and link from one posting into the tracker form. |
| 0:35–1:05 | **Regardless of layout:** press the hotkey on postings from three different platforms (e.g. Lever, Greenhouse, Ashby; layouts visibly different on screen). Rows appear in the real Google Sheet. Counter: **0 AI calls** for known layouts. One platform is new: Patchwork learns it in a few seconds (one AI pass, checked by code), the row appears labelled "learned now", and the next posting from that platform is instant with 0 AI calls. |
| 1:05–1:35 | **Break, live:** a judge renames a question in the tracker form ("Company" → "Employer"). Next hotkey press: blocked **before any write**, incident explains the change, AI proposes the remap, owner approves, the held posting goes in. |
| 1:35–1:55 | **Proof:** AI calls per posting over N real postings: browser agent flat line vs Patchwork dropping to zero; plus the v1 benchmark card (a one-shot Claude script silently lost records; Patchwork caught 2/2). |
| 1:55–2:00 | Close: "Code that learns. AI is the teacher, not the worker." |

## 3. Goals, non-goals, anti-goals

**Goals**
1. A user teaches a page → form transfer once from a recording; Patchwork produces a spec, compiles it, tests it.
2. A hotkey in the user's own browser sends the current page; the row is written within ~2–3 s for a known layout.
3. Layout-independent reading: a learned reader per layout (CSS selectors, embedded JSON-LD when present, label
   regexes on page text), routed by site; known platforms reused across companies.
4. Unknown layout → a **learn lane**: AI proposes a reader with quoted evidence; code checks it; it's saved as a new
   parser version (auto-used or held for approval, see §6); next time it's code.
5. Destination without an API: fill and submit a web form, verify it was accepted.
6. Destination contract check for forms: before writing, compare the live form's fields with the snapshot the
   version was compiled against; block + incident on change; AI-proposed remap with approval.
7. Visible proof: AI calls per posting over time, per-row "code" vs "learned now" labels, approvals history.
8. v1 demo (supplier POs → Stockroom) keeps working unchanged as a fallback.

**Non-goals (this hackathon)**
- Typing directly into the Google Sheets grid (canvas-rendered, fragile); see §6 decision D1.
- People-written free-form email (the hybrid design covers it in principle; not built or demoed).
- Sites behind logins or bot protection (LinkedIn etc.), scanned PDFs/OCR, mobile.
- Shared recipe library across users (mention as "what's next").
- Gmail/Notion/Airtable connectors (see `docs/real-app-integrations.md`, later).

**Anti-goals (failure even if it technically works)**
- Writing a wrong row silently. Any doubt → hold for a person.
- Claiming "any layout, no AI": the honest claim is "learns each new layout once".
- AI at runtime that isn't labelled and checked, or AI keys reaching the runner.
- A 2-minute demo with dead air (long model waits) or that only works with the scripted agent without saying so.
- Breaking the v1 demo or its tests.

## 4. Constraints

- 24 hours, a team of parallel agents, then integration; code freeze leaves time for rehearsal and a backup video.
- Free OpenRouter models are slow (BUILD ~2 min on nemotron); 20 req/min and 1,000/day shared.
- Runner rules from v1 stand: subprocess, stripped env, refuses to start with AI modules loaded, no LLM calls.
- Privacy: page HTML from the user's browser goes to the model provider only in the learn lane and only for
  public job pages in the demo.
- The user's live demo on ports 8000–8003 and 5180 must not be disturbed by builders.

## 5. Recon findings (2026-11-14)

- Public job listings exist without auth on all three platforms checked: Greenhouse (Airbnb, 214 jobs),
  Lever (Palantir, 302), Ashby (Ramp, 180).
- Lever and Greenhouse posting pages are **server-rendered**: title and location are in the raw HTML
  (`<h2>` on Lever, `<h1>` on Greenhouse). Plain HTTP + HTML parsing works; no headless browser needed to read.
- Only **Ashby** embeds schema.org `JobPosting` JSON-LD in raw HTML; Lever and Greenhouse did not. JSON-LD is a
  shortcut when present, not the plan.
- Greenhouse also serves company-branded domains (e.g. careers.airbnb.com) with the same page, so routing purely by
  host misses platform reuse; see §7.2.
- Not yet verified: Google Forms fill-and-submit from a headless browser at low volume (spike first, §9).

## 6. Decisions

Decided by the owner in conversation:
- The special thing is "see it once, fill it in regardless of layout".
- Real data, not mock apps; the job-tracker use case.
- Trigger: on a posting, press a macro/hotkey and the row appears.
- Connecting without APIs is preferred.
- AI may handle what isn't learned yet, if checked and labelled (owner accepted the hybrid framing).
- 2-minute demo; tracks Practical AI and Business/Enterprise.

Open, needed from the owner (defaults in brackets are the builder's to use if unanswered):
- **D1 Destination.** [Google Form linked to the tracker Sheet, filled by a headless browser.] Alternatives: Sheets
  API (an API, but reliable), or typing into the Sheets grid (no API, fragile).
- **D2 New layouts.** [Auto-use a learned reader when every code check passes and nothing was ambiguous; ask when
  ambiguous; every learned reader shown in an approvals history with one-click revert.] Alternative: always wait
  for approval (stronger enterprise story, slower demo).
- **D3 Stage model.** [Free fallback model `particle-ai/particle-1:free` for the live learn and repair: measured
  2–3 s per call in v1.] Alternative: a paid fast model (cents per demo).

Calls made by Claude (builder may revisit with reason):
- C1 Hotkey via a small Chrome extension (Manifest V3, `commands` shortcut) that posts the page's URL, title and
  HTML to the control plane. Teaching keeps the existing Playwright recorder window.
- C2 The extension's snapshots land in a **page inbox** on the control plane exposed as a JSON list API, so the
  integration's source stays `kind: api` and the v1 runner, cursor, dedupe and pipeline are reused.
- C3 HTML parsing in the runner with a small deterministic library (e.g. `selectolax` or `lxml`); no browser to read.
- C4 Form writing in the runner with Playwright (already a dependency for capture), headless.
- C5 Dedupe key: the posting's canonical URL.
- C6 Keep v1 untouched: v2 is a second integration (`jobs_to_tracker`), new spec features are additive.
- C7 Tracker columns are the four the owner named: **job title, company, link, date applied**. Date applied is the
  date of the hotkey press (`record.captured_at`, formatted as a date), not read from the page. The learn lane's
  evidence check applies only to fields extracted from the page; values from `record.*` are exempt.
- C8 Every platform reader in the demo comes from teaching or the learn lane, never hand-written. The "already known"
  platforms on stage were learned earlier, off camera, and the Learned layouts panel shows when. The benchmark
  starts from an empty layout library.
- C9 Builders develop and rehearse against a **separate demo Form and Sheet**, never the owner's real tracker. Resetting
  between rehearsals: clear the Sheet's rows (a person, or the API if D1 is the Sheets API) and clear Patchwork's
  processed keys (the existing demo reset).

## 7. Design

### 7.1 Flow

```
TEACH (once)   recorder window: copy title/company/link from a posting -> paste into tracker form -> submit
               -> recording (copy/paste/input/submit events + page snapshot) -> matcher candidates
               -> interpreter agent -> spec.yaml (page parser + form sink) -> compile -> tests -> approve v1

RUN (every hotkey)
  extension --POST /api/pages {url,title,html}--> page inbox --> run jobs_to_tracker for the new snapshot
  route layout -> known parser?  yes -> extract (code) -> validate -> dedupe (URL) -> preflight form contract
                                       -> fill form -> submit -> verify -> row in Sheet        [0 AI calls]
                                 no  -> quarantine PARSE_NO_MATCH -> LEARN lane (agent, AI) -> new parser
                                       checked by code (evidence + tests) -> new version (D2) -> replay
  form changed? -> block before write -> CONTRACT_CHANGED incident -> REPAIR (agent) -> approve -> replay
```

### 7.2 Spec additions (additive to `spec/models.py`, compiled by `compiler/`)

- **Record shape from the page inbox:** `{id, url, site, title, html, text, captured_at}` (`text` = visible text).
- **New field extractors** (closed vocabulary, deterministic), next to existing `regex | json_path | between`:
  - `css: "<selector>"` with optional `attr: "href"` and `text: true` (whitespace-normalised);
  - `jsonld: "<path>"` reading `@type: JobPosting` (e.g. `hiringOrganization.name`);
  - `const_from: record.url` already covered by `from: record.<field>` in sink bodies.
- **Routing:** `route_by: site` with exact hosts, plus an ordered **fallback** that tries platform parsers whose
  `match.fingerprint` (a CSS selector or meta tag that identifies the platform, e.g. a Greenhouse marker) is present.
  This gives platform reuse on branded domains. The builder decides the exact fingerprint mechanism.
- **Sink `kind: form`:** `url`, `fields: {<form label>: <value source>}`, `submit` (button label), `verify`
  (`confirmation_text` on the next page). Label-based targeting; the compiled code resolves labels to inputs at run
  time and fails closed if a label is missing or ambiguous.
- **Form contract:** compile time snapshots the form's fields (label, type, required, options) into an
  OpenAPI-shaped document so `runner/contract.py`, `CONTRACT_CHANGED`, `diff_openapi` and the incident UI keep
  working (same adapter idea as `docs/real-app-integrations.md` §3.1).

### 7.3 Runner

- Reuses `runner/pipeline.py` order: route → extract → validate → dedupe → map → send → verify → mark.
- `send` for `kind: form` drives headless Playwright: open, fill by label, submit, check confirmation; maps failures
  to existing reason codes (`SINK_CONTRACT` for missing/renamed fields, `VERIFY_MISMATCH` for no confirmation).
- Preflight for forms: load the form, snapshot fields, hash, compare; block before any write on change.
- Still zero AI; no keys in env. Single-record runs must be fast enough for a ~2–3 s hotkey round trip (keep a warm
  browser if needed; builder's call).

### 7.4 Learn lane (new agent mode `LEARN`, lighter than REPAIR)

**All AI work in v2 runs on LangChain Deep Agents** (`deepagents==0.7.38`, `agents/deep.py` `DeepAgentService`):
TEACH (interpreter), LEARN and REPAIR are modes of the same `create_deep_agent` supervisor, with planning
(`write_todos` via `TodoListMiddleware`), subagents through the `task` tool, the virtual filesystem with write
permissions limited to `/spec.yaml`, `/examples.yaml`, `/transforms.py`, and the existing `EventMiddleware` and
`ResilientModelMiddleware`. LEARN adds a `learner` subagent (tools: `get_page`, `test_parser` on HTML, evidence
check, ambiguity check → `ask_user`, `propose_spec_patch`, `add_test_case`); the builder subagent compiles and tests
as in v1. Guardrails stay in tools, not prompts (`agents/deep_tools.py`). `ScriptedAgentService` gets matching
LEARN/REPAIR scripts only as the stage fallback, emitting the same events. See `agents/README.md` for the deepagents
API notes already learned in v1.

- Trigger: a `PARSE_NO_MATCH` from the page inbox for a site with no parser (no 3-record threshold; one page is
  enough because a page layout is a template).
- Steps: fetch the snapshot → propose a parser (css/jsonld/regex) → `test_parser` on the snapshot → **evidence
  check in code**: every extracted value must occur verbatim in the page text (or JSON-LD) and pass validation →
  ambiguity check (more than one plausible candidate for a field → `ask_user`) → compile → tests → D2 policy →
  replay the held page.
- Every row written after a learn shows `parser: <site>_v1 (learned 14:02)`; every learn is in the approvals history.
- Model-call budget per learn small (target ≤ 8 calls); scripted equivalent for the stage fallback.

### 7.5 Teach from a recording on real pages

- Recorder already emits `copy` (text + element description), `paste`, `input`, `submit`, `framenavigated`
  (`capture/recorder.js`), and the matcher already falls back to typed form values when no API request was seen
  (`matcher/core.py` `_destination`). Add: a snapshot of the source page HTML at copy time and a stable element path
  for the copied element, so candidates can say "title = `h2` on jobs.lever.co".
- Destination fields come from the form's labels at submit time.

### 7.6 Control plane and UI

- `POST /api/pages` (from the extension; localhost only, token from the extension options page) → page inbox;
  `GET /api/pages?since=` (the integration's source); trigger a run of the integration on arrival.
- UI: a live "Tracker" view: each hotkey press as a row with its layout, parser, AI calls (0 or learn), outcome;
  a **Learned layouts** panel (site, version, learned when, approved by, revert); AI-calls-over-time chart on the
  Benchmark tab; presenter mode that sequences the 2-minute script.

### 7.7 Proof (benchmark additions)

- Replay N real postings (e.g. 60 across 3–5 platforms, fetched once and stored as fixtures with source URLs) through
  Patchwork from an empty layout library: count AI calls per posting (learns only), accuracy against a hand-checked
  answer key, time per posting. Compare with a browser-agent estimate (labelled `estimate`, as in v1) where every
  posting costs AI calls. Chart: cumulative AI calls vs postings.

## 8. Workstreams for the agent team

Each workstream owns its files, works in its own worktree, keeps all tests green, and commits on its own branch.
Contracts between them go in `docs/contracts.md` first (hour 0–1).

1. **Engine (spec/compiler/runner):** page record shape, `css`/`jsonld` extractors, routing fallback, `kind: form`
   sink with label targeting, form contract snapshot + preflight, reason-code mapping, generated tests; a local
   fixture form app for offline tests (`demo_apps/`), plus fixture pages saved from the three platforms.
2. **Capture + extension:** Chrome MV3 extension (hotkey → `POST /api/pages`), page snapshot + element path on copy
   in the recorder, matcher candidates for page → form.
3. **Agents:** `LEARN` mode and tools (`get_page`, `test_parser` on HTML, evidence check, ambiguity check), interpreter
   update for page → form teaching, repairer for form contract changes, scripted equivalents for all three.
4. **Control plane + UI:** page inbox endpoints and run-on-arrival, Tracker view, Learned layouts panel + approvals
   history + revert, presenter mode, mock server updates.
5. **Proof + pitch:** real-posting fixture set with answer key, AI-calls-over-time benchmark and chart, `make smoke`
   for v2, 2-minute script, backup video checklist.

## 9. Order and risk

- **Hour 0–2 spikes (go/no-go):** (a) fill and submit a Google Form from headless Playwright five times and see the
  rows in the linked Sheet; (b) extract title/company/link from saved Lever/Greenhouse/Ashby pages with css/jsonld.
  If (a) fails: fall back to the Sheets API sink (D1 alternative) and keep everything else.
- Biggest risks: Google Forms automation (captcha, markup), the learn lane's speed on free models (D3), routing on
  branded domains, 24 h scope. Mitigations: spike first, scripted learn fallback, fixture pages for offline tests,
  v1 untouched.
- Done means: the 2-minute script runs end to end three times in a row on the real model and once on the scripted
  agent; all v1 and v2 tests pass; benchmark numbers labelled measured/estimate.

## 10. Left to the builders

Library choices (HTML parser), exact extension UX, warm-browser strategy, fingerprint mechanism, presenter-mode
details, chart styling (load the `dataviz` skill), file layout inside each workstream.
