# Patchwork web console

Vite + React + TypeScript + Tailwind. Five tabs: **Record · Review · Agent Activity · Integration · Runs**,
with the AI on/off switch in the header and the `LLM calls: 0` badge on Runs. It talks only to the control
plane API (`docs/contracts.md` section 4) through `src/api.ts`.

## Commands

```bash
cd web && npm ci              # once

npm run dev:mock              # UI on :5180 + mock control plane on :5181 (no Python needed)
npm run dev                   # UI on :5180, proxying /api to $PATCHWORK_API (default http://127.0.0.1:8000)
PATCHWORK_API=http://127.0.0.1:38080 npm run dev   # against `make api`

npm run typecheck             # tsc
npm test                      # vitest: API client, SSE resume, thread derivation, mapping table
npm run build                 # typecheck + production bundle in dist/
node mock/smoke.mjs           # the whole demo over HTTP against the mock (asserts the real counts)
```

From the repo root, `make up` starts the demo apps and the control plane on :8000 and `make web` starts this UI
against it. `make web-mock` runs it against the mock.

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
with live snapshot progress. Flags: `--port 5181`, `--pace 1` (0 = instant).

To regenerate the data: `.venv/bin/python web/mock/collect.py` from the repo root.

## Screenshots

`scripts/demo_screens.py` clicks through the demo with Playwright and saves every tab in light and dark themes to
`screenshots/`. `screenshots/real/` is the same walk against the real control plane
(`REAL=1 INBOX=localhost:38081 SCREENS_OUT=... python web/scripts/demo_screens.py`).
