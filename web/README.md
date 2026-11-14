# Patchwork web console

Vite + React + TypeScript + Tailwind. Seven tabs: **Tracker · Record · Review · Agent Activity · Integration · Runs · Benchmark**,
with the AI on/off switch in the header and the `LLM calls: 0` badge on Runs. It talks only to the control
plane API (`docs/contracts.md` section 4 and Part 2 V9) through `src/api.ts`.

## v2: jobs → tracker form

- **Tracker** (first tab, the default): every hotkey press (`GET /api/tracker`, polled every second) with site, title,
  company, reader + version, how it was routed, a label chip and AI calls. Labels: `code · 0 AI calls`,
  `learned now · N AI calls (checked by code)` (with the scripted agent: `scripted agent, 0 AI calls`), `held: unknown layout`,
  `held: form changed`, `already in tracker`. Click a row for why it's held, what a person can do, and the learn thread's
  evidence (each value, where code found it, ✓/✗). An open `CONTRACT_CHANGED` incident on `jobs_to_tracker` shows the
  Repair banner. Below: **Learned layouts** (origin, auto/person approval, learn policy switch, revert with confirm) and
  the extension setup values (`GET /api/extension`, copy buttons).
- **Benchmark** adds *AI calls per posting* from `GET /api/bench/v2` (Patchwork measured: solid; browser agent estimate:
  dotted, as on the v1 chart; badged `mock data` when the mock serves it).
- **Presenter mode**: `/?present=1` (or the button on Tracker). The six beats of the 2-minute script with what to say and
  do, a clock against each beat's slot, the live tracker and AI-calls counter, the incident banner and the yellow gate inline.
  `→`/Space next beat, `←` back, `1`–`6` jump, `T` restart the clock, `Esc` back to the console. `?present=1&beat=3` opens a beat.

## Commands

```bash
cd web && npm ci              # once

npm run dev:mock              # UI on :5181 + mock control plane on :5182 (no Python needed)
MOCK_AGENT=deep npm run dev:mock   # same, but the mock pretends the deep agent ran (learned rows show "5 AI calls")
npm run press -- lever        # stand-in for the hotkey: lever|greenhouse|branded|ashby|workable|dup [--n 1], rename, reset
npm run dev                   # UI on :5180, proxying /api to $PATCHWORK_API (default http://127.0.0.1:8000)
PATCHWORK_API=http://127.0.0.1:38080 npm run dev   # against `make api`

npm run typecheck             # tsc
npm test                      # vitest: API client, SSE resume, thread derivation, mapping table
npm run build                 # typecheck + production bundle in dist/
node mock/smoke.mjs           # the whole demo over HTTP against the mock (asserts the real counts)
```

From the repo root, `make up` starts the demo apps and the control plane on :8000 and `make web` starts this UI
against it. `make web-mock` runs it against the mock.

**Benchmark** reads `GET /api/bench/results` (`bench/results.json`, written by `make bench`): break-even chart, breaks caught
before any bad write (Patchwork vs a one-shot script), repair times and every number with its kind. Estimates and
extrapolations are dotted/dashed and badged. The mock serves the same file (404 until `make bench` has run).

## Demo click path (plan pages 14–15)

1. **Record**: Start recording, transfer one PO by hand in the browser that opens, Stop. With no one driving the
   browser, use **Use fixture recording** (plays back `fixtures/recording.json`). **Find mappings**, then
   **Build integration**.
2. **Agent Activity**: plan, subagent timeline and tool calls stream in. Answer the yellow question card, then
   **Approve & deploy v1** on the yellow deploy card. Review shows the same open question beside the mappings.
3. **Runs**: switch **AI OFF**, Limit 100, **Run batch**. The counters fill in and `LLM calls: 0` stays at 0.
4. **Switch to v2 (Break 1)**, Run batch again: preflight blocks it before any write and the incident banner opens.
5. Switch AI on in the banner, **Repair with AI**, answer "By supplier", approve v2. The agent replays quarantined records.
6. AI off, **Run batch**: the rest succeed with the v2 body. Integration → **Diff** shows v1 → v2.

Yellow means a human gate (agent questions, deploy approvals) and nothing else.

## Mock control plane

`mock/server.mjs` uses only the Node standard library. It replays `mock/data.json`, which `mock/collect.py` produced
from a real run of the compiler, runner and `ScriptedAgentService`, so the counts, contract errors, generated files
and agent events are real. It keeps state in memory: recordings stream the fixture's events, BUILD and REPAIR threads
pause at `ask_user` / `request_deploy` until resumed, and runs simulate the pipeline from the seed's ground truth
with live snapshot progress. Flags: `--port 5182`, `--pace 1` (0 = instant), `--agent scripted|deep`.

v2 lives in `mock/v2.mjs` (hand-written, not produced by `collect.py`). It starts **seeded**, mid-demo, with a row for
every label, an auto- and a person-approved layout, a finished learn thread with `evidence`/`learned` events and an open
form incident. `POST /api/demo/v2/reset` (or `npm run press -- reset`) goes to the baseline: three known layouts
(lever, greenhouse, ashby), empty tracker, default form. Then `press workable` learns a new layout, `press dup` is a
duplicate, `press rename` + any press is blocked before any write, and AI off + a new layout is held. `/api/bench/v2`
serves `bench/v2/results.json` when it exists, else illustrative numbers flagged `mock: true`.
`npx vitest run src/api.v2.test.ts` checks every v2 client call against it.

To regenerate the data: `.venv/bin/python web/mock/collect.py` from the repo root.

## Screenshots

`scripts/v2_screens.py` (with `npm run dev:mock` running) saves `screenshots/v2-*.png`: Tracker, evidence, held hints,
Benchmark, every presenter beat (light and dark) and `v2-flow-*` (reset → presses → learn → rename → repair → approve, in presenter mode).

`scripts/demo_screens.py` clicks through the demo with Playwright and saves every tab in light and dark themes to
`screenshots/`. `screenshots/real/` is the same walk against the real control plane
(`REAL=1 INBOX=localhost:38081 SCREENS_OUT=... python web/scripts/demo_screens.py`).
