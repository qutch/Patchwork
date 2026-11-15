# Patchwork v2 demo: the 2-minute script

Design: `docs/superpowers/specs/2026-11-14-patchwork-v2-design.md` §1–§2. API: `docs/contracts.md` V9.
Automated version of this script: `make smoke-v2` (`scripts/smoke_v2.py`). Numbers: `bench/v2/results.json`
(`make bench-v2`). Tracks: Practical AI, Business/Enterprise.

One line: **"Code that learns. AI is the teacher, not the worker."**
The claim we make: Patchwork **learns each new layout once**, then reads it with code. We never say "any layout, no AI".

Button and tab names below follow the V9/W4b plan (Tracker tab, Learned layouts panel, incident banner with Repair,
Approve in the agent panel, Benchmark tab, presenter mode `?present=1`). Confirm each label at the first full
rehearsal and fix this file if the UI says something else.

---

## 0. Stage setup (before the slot, ~10 min)

Start Patchwork with one command, at the repo root:

```bash
./patchwork        # the API and the UI on one port, workspace-v2/ (the saved baseline), deep agent, AI on
```

It prints one block that starts `Patchwork is running at http://127.0.0.1:38091` (or the next free port up to
38110, if something else holds 38091) and opens that address. **Use the printed address everywhere below**
(`<URL>`); `cat workspace-v2/patchwork.url` shows it again. Ctrl-C stops it. `./patchwork --background` starts it
detached (`./patchwork status`, `./patchwork stop`). No `make up` and no separate UI server: the control plane serves
the UI itself.

Screen layout, 1280 px or wider, browser zoom 110 %:

| Window / tab | What | Why |
|---|---|---|
| Chrome window A, tab 1 | Patchwork UI `<URL>/?present=1` (Tracker tab) | rows, labels, AI-calls counter, beat hints |
| A, tab 2 | Patchwork UI Benchmark tab | proof chart |
| Chrome window B, tabs 1–3 | three **known** postings: one Lever, one Greenhouse (job-boards.greenhouse.io), one Ashby | layouts visibly different |
| B, tabs 4–5 | two postings from the **new** platform: careers.airbnb.com (branded Greenhouse) | learn once, then code |
| B, tab 6 | one more known posting (any of the three) | the held-then-written posting in the break beat |
| Chrome window C | the demo Google Form **editor** (Company question visible) | the judge renames it |
| Chrome window D | the linked Google Sheet, "Form Responses 1" | rows appearing |

Pick postings from `fixtures/postings/answer_key.json` (they are real, public, and their title/company are checked)
and open their `url` live; re-check they still load the morning of the demo (postings close).

Why careers.airbnb.com is the new platform: it routes to the Greenhouse reader by fingerprint, that reader fails on
the branded layout, so it is held as `PARSE_NO_MATCH` and learned (V4), which is the case W3's tests cover. If a
rehearsal shows the learn works as well on SmartRecruiters (jobs.smartrecruiters.com), that is a more striking
"never seen before" platform; switch only after two clean rehearsals on it.

Pre-flight (all must pass, in order):

1. `./patchwork` is running (above). It uses the deep agent; if step 4 is slow, restart it on the scripted agent
   (see §3).
2. Extension loaded, shortcut set (`chrome://extensions/shortcuts`, default `Alt+Shift+P`). Reload the extension
   (`chrome://extensions` → Patchwork → reload), then click **Find Patchwork** once in its options: the installed
   copy has `127.0.0.1:38091` saved as a manual address, and a saved address always wins. After that it finds
   Patchwork by itself, with nothing to paste (its options page says "Connected automatically to `<URL>`"). Press the
   shortcut once on a known posting off camera: badge `✓`.
3. Learned layouts panel shows exactly lever, greenhouse, ashby (learned earlier, off camera, with times) and **not**
   careers.airbnb.com. Settings: AI on, learn policy `auto`.
4. `make smoke-v2-stage` once, 60–30 minutes before the slot (`STAGE_PORT=<port> make smoke-v2-stage` if `<URL>` is
   not on 38091; it resets, uses the demo Form/Sheet and
   prints a timing per beat; it stops and waits for Enter when you must rename the Form question by hand).
   Note the learn beat's seconds. Then do the rehearsal reset (§4). It needs the saved baseline (I-2); on a fresh
   workspace add `--bootstrap` (`scripts/smoke_v2.py --bootstrap`: TEACH from `fixture-jobs`, learn Greenhouse and
   Ashby from one stored posting each, save the baseline). `make smoke-v2-offline` runs the same beats on its own
   in-process stack (fixture form, scripted agent) without touching `./patchwork`.
5. Model status: on Claude Haiku 5.5 the learn beat takes ~12 s and the repair ~22 s. If step 4's learn took > 20 s,
   present on the scripted agent (§3).

---

## 1. The script (2:00)

Times are targets; the presenter mode's `→` key advances the beat hint.

### 0:00–0:15 Hook (talk only, Tracker tab on screen, empty)

> "I track every job I apply to. Copying title, company and link is the boring part. An AI agent could do it, but it
> would read every page with AI, forever. A Claude-written script breaks the first time a site looks different.
> Patchwork learns each layout once, then it's just code."

### 0:15–0:35 Show it once

Click: **Agent** (or Integration) tab → the `jobs_to_tracker` v1 teaching thread (pre-recorded, `fixture-jobs` or the
owner's own recording). Scroll the events fast: copy title, copy company, paste into the form, submit → spec → tests
passed → approved v1.

> "I showed it once: copied the title and company from a posting into my tracker form. It wrote a spec, compiled it to
> code and tested it. That's the only demonstration."

### 0:35–1:05 Regardless of layout

1. Window B tab 1 (Lever) → **`Alt+Shift+P`**. Switch to the Tracker: row appears, label **code · 0 AI calls**.
2. Tab 2 (Greenhouse) → `Alt+Shift+P`. Tab 3 (Ashby) → `Alt+Shift+P`. Two more rows, both **code · 0 AI calls**.
   Point at window D: three rows in the real Google Sheet.

> "Three platforms, three different layouts, zero AI calls. Those layouts were learned once, earlier; here's when."
> (Point at the Learned layouts panel: site, learned at, approved by.)

3. Tab 4 (careers.airbnb.com) → `Alt+Shift+P`. Row shows **learning**, then **learned now · N AI calls (checked by
   code)**. Expand the row: evidence (value, where found, ✓).

> "This site is new. One AI pass proposes a reader; code checks every value is really on the page, and that nothing
> was ambiguous, before it's used."

4. Tab 5 (second Airbnb posting) → `Alt+Shift+P`. Row: **code · 0 AI calls**.

> "Next posting from that site: code. The AI-calls counter didn't move."

### 1:05–1:35 Break, live

1. Hand the laptop (window C, Form editor) to a judge: "Rename 'Company' to 'Employer'." They click the question
   title, type `Employer`, click away (Google autosaves).
2. Window B tab 6 → `Alt+Shift+P`. Tracker row: **held: form changed**. Banner: `CONTRACT_CHANGED`, "Company: not
   accepted by destination". Sheet: no new row.

> "It stopped before writing anything. A script would have filled the wrong box or silently dropped the company."

3. Banner → **Repair**. Agent panel: proposes `Company → Employer` (JSON Patch `move`), tests pass → **Approve**.
4. The held posting is written; row label turns to **code**, Sheet shows the new row with the company under Employer.

> "AI proposed the remap, code tested it, I approved it. The held posting went in."

### 1:35–1:55 Proof

Window A tab 2 (Benchmark). Chart: cumulative AI calls over the real postings, browser agent (dashed, estimate) vs
Patchwork (solid) flattening after each platform is learned. Then the v1 card.

> "Sixty real postings across five platforms. A browser agent pays AI on every posting (that line is our estimate, the
> assumption is written on the chart). Patchwork pays once per layout and then nothing. And from v1: a one-shot Claude
> script silently lost records when the input changed; Patchwork caught both breaks before a bad write."

Say only the numbers on the chart and their labels (§5). If the chart shows "scripted", say so.

### 1:55–2:00 Close

> "Code that learns. AI is the teacher, not the worker."

---

## 2. "How is this different" slide (text)

Title: **AI is the teacher, not the worker**

| | Per record, forever | When a site looks different | When the destination changes | Over time |
|---|---|---|---|---|
| **AI browser agent** | pays for and risks AI on every record | adapts, unchecked | adapts, unchecked | AI calls flat, forever |
| **Script written by Claude** | no AI | breaks or silently writes wrong data | fails silently | can't learn |
| **Patchwork** | code, 0 AI calls for known layouts | learns the layout once; AI's answer checked by code and labelled | blocks before any write, proposes a tested fix for approval | AI calls per day trend to zero |

Footer: "Learns each new layout once. Holds anything it isn't sure of for a person."
(Never "any layout, no AI".)

---

## 3. Fallbacks

| Situation | Do | Say |
|---|---|---|
| Learn has not finished after ~15 s (slow model) | keep talking over the streaming events (todos, evidence); at 25 s switch to the backup video at the learn beat | "The model is slow today; here's the same step from our rehearsal." |
| Model API down before the slot | Ctrl-C (or `./patchwork stop`), then `PATCHWORK_AGENT=scripted ./patchwork` (restart before going on stage, not during) | "This is the scripted agent: same checks, same compiler and runner, no model. The AI-calls column says 'scripted'." |
| Presenting on the scripted agent | nothing to hide: the Tracker labels say `scripted`; the benchmark chart's per-learn calls come from a deep run (`measured (earlier run)`) | "The learn you just saw ran the scripted agent. The chart's AI calls per learn are from a real-model run earlier today." |
| Extension badge `!` or nothing happens | press once more; then use the UI's paste-a-URL fallback if W4b ships one, else the backup video | "Let me send that again." |
| Google Form submit slow (> 5 s) | wait; rows land in the Sheet < 1 s after submit (spike a) | "It's filling the real Google Form in a headless browser." |
| Judge renames the wrong question | let it run: any rename blocks the same way | "Any change to the form blocks it the same way." |
| Repair proposes a question instead of the remap | answer it with the obvious mapping (Company → Employer) | "When it isn't sure, it asks rather than guessing." |
| Everything is on fire | v1 demo (`make up`, supplier POs → Stockroom) still works unchanged | "Same engine on the API demo." |

AI off on stage (Settings → AI off) is also a fine thing to show if asked: an unknown layout is then held with
`PARSE_NO_MATCH`, nothing written, no learn thread; switching AI on and replaying learns it (Review Focus 5).

---

## 4. Rehearsal reset (between runs, ~2 min)

1. Google Form editor: rename `Employer` back to `Company` (exact text, no trailing space).
2. Google Sheet: select rows 2..N of "Form Responses 1" → right-click → Delete rows. Keep the header row.
   (A person does this; nothing in Patchwork deletes Sheet rows.)
3. `curl -X POST <URL>/api/demo/v2/reset`, with the address `./patchwork` printed (restores the saved baseline of
   `jobs_to_tracker`: lever, greenhouse, ashby learned; clears pages, runs, records, processed keys, incidents and learn threads. Never touches
   `po_to_stockroom`). The baseline is saved once after the off-camera learns with `POST /api/demo/v2/save-baseline`.
4. Check: Tracker empty; AI-calls counter 0; Learned layouts = lever, greenhouse, ashby; no open incident;
   `GET /api/settings` → `ai_enabled: true`, `learn_policy: auto`.
5. Close and reopen the posting tabs (fresh DOM), keep the Form editor and Sheet open.

`make smoke-v2-stage` does steps 3–4 itself; with the Google Form it prints steps 1–2 and waits for Enter.

---

## 5. What's measured and what's estimated (say only these)

Fill the values from `bench/v2/results.json` after the final `make bench-v2`; the `kind` column must match the file.

| Number on screen | kind | source |
|---|---|---|
| AI calls per known-layout posting (0) | measured | `bench/v2/results.json` postings with label `code`; the runner cannot import a model SDK |
| Postings, platforms, accuracy vs the answer key, wrong rows written | measured | `make bench-v2` (this run), `fixtures/postings/answer_key.json` |
| Layouts learned and which postings triggered them | measured | `make bench-v2` (this run) |
| Seconds per known posting (inbox → row) | measured | `make bench-v2`, fixture form (the Google Form adds ~1.3–2 s per submit, spike a) |
| AI calls per learn | measured (earlier run) | deep-agent LEARN runs, `agents/README.md` (W3-7); `estimate` (the ≤ 8 design budget) until those exist |
| Learn seconds on stage | measured (earlier run) | the pre-flight `make smoke-v2-stage` timing of the learn beat |
| Browser-agent AI calls per posting | estimate | assumption on the chart and in `bench/v2/results.json` (≥ 1 model call per posting is a floor; we use a stated per-step count) |
| Patchwork cumulative line past the 60 postings | extrapolated | flat unless a new layout arrives; only if the chart extends it |
| v1 card: script silently dropped records, Patchwork caught 2/2 breaks | measured | `bench/results.json` (v1 `make bench`) |
| v1 deep-agent BUILD / repair times | measured (earlier run) | `bench/results.json` `deep_agent` |

Things we do **not** claim: that the browser-agent number was run; that learning never needs a person (ambiguous or
uncorroborated readers wait for approval); that every layout is learnable (pages behind logins and bot walls are out of
scope).

---

## 6. Backup video checklist

Record after the first clean full rehearsal on the deep agent, re-record if the UI changes.

- [ ] Screen recorder at 1080p+, 30 fps, system audio off, mic on (or silent with captions); cursor highlight on.
- [ ] Same window layout as §0; notifications off (Do Not Disturb), bookmarks bar hidden, no personal tabs, no `.env`
      or terminal with secrets visible; demo Form/Sheet only (never the owner's real tracker).
- [ ] One continuous take of §1 end to end (≤ 2:15), plus separate clips: (a) the learn beat, (b) the break + repair
      beat, (c) the benchmark chart. The clips are what you cut to on stage.
- [ ] The AI-calls counter and the row labels are readable in the video (zoom in post if needed).
- [ ] The agent kind is visible once (deep vs scripted) so the video doesn't over-claim.
- [ ] Export MP4 (H.264), keep a copy on the laptop desktop **and** a USB stick; test it plays offline full screen.
- [ ] Note the video's date and agent in its file name, e.g. `patchwork-v2-2026-11-15-deep.mp4`.
- [ ] Reset (§4) after recording.
