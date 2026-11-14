# Patchwork-plus

Demonstrate a data-transfer task once in the browser and type one sentence of intent. Patchwork's
agents turn that into a versioned integration program that runs with **zero LLM calls**. When
the program breaks, the agents propose a tested repair for you to approve.

This branch contains the **MVP spine**: demo apps, spec, compiler, runner, preflight, versions
and benchmark, all with the AI off. See [ROADMAP.md](ROADMAP.md) for what comes next.

## Quick start

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r pyproject.toml --extra dev
make test          # 29 tests, including an e2e run against real servers
make apps          # terminal 1: Inbox :8001, Stockroom :8002, staging :8003
make demo          # terminal 2: compile -> approve -> run 150 POs -> Break 1 blocked
```

## How it fits together

```
fixtures/spec.yaml ──validate──> compile vN ──> workspace/integrations/<name>/vN/
  (agents write this later)        │              integration.py parsers.py adapters.py
                                   │              transforms.py config.yaml tests/
                                   └─ generated tests must pass ─> deploy (approval) ─> active pointer
                                                                                   │
python -m runner run <name>   (subprocess, stripped env, no LLM)  <────────────────┘
  preflight: sink /openapi.json hash changed? contract test -> block + incident before any write
  per record: route -> extract -> validate -> dedupe -> map -> sink -> verify -> mark processed
  outcome: success | duplicate | quarantined(PARSE_NO_MATCH | VALIDATION_FAILED | NOT_FOUND |
           SINK_CONTRACT | AUTH | VERIFY_MISMATCH | UNKNOWN)
```

| Dir | What |
|---|---|
| `spec/` | Pydantic models for the Integration Spec (closed vocabulary) + `validate_spec` |
| `compiler/` | `compile_spec()` and Jinja templates for code and tests |
| `runner/` | Runtime: pipeline, preflight, store (SQLite), contract checks. Never imports an LLM SDK |
| `control/` | Operations the API/agents will wrap: compile+test, deploy, sandboxed run, status; CLI |
| `demo_apps/` | Supplier Inbox (source), Stockroom (destination, v1/v2 contract), deterministic seed |
| `bench/` | Exact accuracy against seed ground truth; benchmark harness, one-shot baseline script, stopwatch |
| `fixtures/` | Hand-made spec, examples and recording, so teams don't block each other |

## CLI

```bash
python -m control validate fixtures/spec.yaml
python -m control compile  fixtures/spec.yaml          # -> vN + generated tests
python -m control deploy   po_to_stockroom 1           # human approval
python -m control run      po_to_stockroom --limit 100 [--dry-run]
python -m control status   po_to_stockroom
python -m control incident 1
python -m bench.accuracy --live po_to_stockroom
curl -X POST localhost:8002/admin/schema -H 'content-type: application/json' -d '{"version":2}'   # Break 1
```

`PATCHWORK_WORKSPACE` (default `./workspace`) holds the DB, generated packages and run logs.
`PATCHWORK_SOURCE_BASE_URL`, `PATCHWORK_SINK_BASE_URL` and `PATCHWORK_STAGING_BASE_URL` override spec URLs.

**Sandboxing is light, and we say so in the pitch:** the runner is a subprocess with a timeout, a scratch working
directory and an environment stripped of model keys and secrets.

## Recording

```bash
make apps                                          # Inbox :8001, Stockroom :8002/:8003
python -m capture record workspace/recordings/rec-manual \
  --intent "For every new purchase order message, record incoming stock" \
  --allow localhost:8001 --allow localhost:8002 --start-url http://localhost:8001/
# headed Chromium opens: open a message, copy the PO, go to localhost:8002/incoming/new, fill the form, Save.
# Close the window (or Ctrl-C) to stop -> recording.json + candidates.json in that dir.
python -m matcher workspace/recordings/rec-manual/recording.json   # re-run stage A on any recording
```

Only allowlisted `host:port`s are recorded (DOM events, navigation, same-origin XHR/fetch with bodies ≤ 20 KB).
Password, `cc-*` and `one-time-code` fields, `Authorization`/`Cookie` headers and secret-looking body keys
are always redacted. `events.jsonl` grows live. The control plane uses `capture.session.start_recording / get_session /
list_recordings` (see `docs/contracts.md` §2). Demo controls on the Inbox: `POST /admin/deliver {count}`,
`POST /admin/new_supplier {count}` (Break 2), `POST /admin/reset`, `GET /admin/ground_truth`.

## Control plane

```bash
make up            # Inbox :8001, Stockroom :8002, staging :8003 + control plane :8000 (Ctrl-C stops all)
make api           # control plane alone on :38080 (what the web/ dev server proxies to)
python -m control.api --port 38080 [--host 127.0.0.1]
uvicorn control.api:app --port 38080
curl localhost:8000/api/health        # interactive API docs at /docs
```

FastAPI app `control.api:app`, implementing `docs/contracts.md` §4 (recordings, agent threads over SSE, integrations,
versions/diffs, background runs with live progress, incidents, demo controls, accuracy). It loads the repo `.env`
(never overriding set variables; `PATCHWORK_NO_DOTENV=1` skips it). Model keys never reach the runner.
`PATCHWORK_AGENT=scripted|deep` picks the agent; the AI on/off switch (`PUT /api/settings`) gates build and repair,
never runs. `PATCHWORK_RUN_PACE_MS` slows the runner per record so the counters are watchable (`make up` sets 30).
Demo app URLs: `PATCHWORK_INBOX_URL`, `PATCHWORK_STOCKROOM_URL`, `PATCHWORK_STAGING_URL`.
Tests: `tests/control/` (the whole demo through HTTP only in `test_api_flow.py`).

## Web UI

```bash
make up            # terminal 1: demo apps + control plane on :8000
make web           # terminal 2: UI on http://localhost:5180 (PATCHWORK_API=http://127.0.0.1:38080 make web for `make api`)
make web-mock      # UI against an in-memory mock control plane, no Python needed
```

Six tabs (Record, Review, Agent Activity, Integration, Runs, Benchmark). See [web/README.md](web/README.md) for the demo click path.

## Benchmark

```bash
make bench                                   # ~7 s, no model calls -> bench/results.json (served at GET /api/bench/results)
python -m bench.stopwatch manual             # time 5 manual transfers (Enter to start / stop); kept by later `make bench` runs
python -m bench.stopwatch computer_use -n 5  # same, for a computer-use agent
```

`make bench` starts its own Inbox, Stockroom live + staging and control plane in-process on free ports (never
8000–8003) with its own workspace and the scripted agent, then plays the demo scenario twice: once through Patchwork
(BUILD, run 100 + 50, Break 1 + REPAIR, Break 2 + REPAIR) and once with a **one-shot script**. That script
(`bench/baseline_script.py`) was written by a fresh Claude session given only `bench/baseline_prompt.md` (the recording,
the intent, the v1 APIs and six sample messages), and was not tuned afterwards. Both are scored against the seed's
ground truth by what actually landed in the Stockroom (`bench/scoring.py`).

Every number in `bench/results.json` has a `kind` and a `source`:

| kind | what |
|---|---|
| `measured` | this harness, this run: Patchwork BUILD/run/detect/repair times (scripted agent, approvals answered instantly), accuracy, bad writes; the baseline's writes, drops and exit codes |
| `measured (earlier run)` | deep agent (real model) BUILD 138 s, Break 1 repair 79 s, Break 2 repair 104 s, from a 2026-11-14 `make smoke` run |
| `estimate` | manual 1 min/record and computer-use 3 min/record (assumptions written next to the number), Patchwork's human setup minutes |
| `extrapolated` | the chart's lines past the measured records |
| `not measured` | e.g. the baseline's time to detect: it never opens an incident |

Result on the current run: the script copies the 150 seed messages perfectly, and the Stockroom's validation stops it
from writing bad rows at Break 1. It never adapts, though, and its "something failed" exit code is already 1 on every
normal run. At Break 2 it silently drops all 4 new-supplier POs and marks them done, so they are never retried.
Patchwork blocks Break 1 at preflight, quarantines Break 2 with an incident, and repairs both after approval.

## Before you present

```bash
make up            # terminal 1 (PATCHWORK_AGENT=deep make up for the real agent)
make smoke         # terminal 2: reset, BUILD, run 150, Break 1 + REPAIR, Break 2 + REPAIR, accuracy; exits 0 if all OK
```

`make smoke` resets the demo, so run it before going on stage, not during. With the scripted agent it takes ~20 s and
no model calls. With `PATCHWORK_AGENT=deep` it takes several minutes and roughly 90 OpenRouter requests (free tier: 1,000/day).
If the model is slow or down on stage, restart with `PATCHWORK_AGENT=scripted make up`: it tells the same story
(same events, same questions, real compiler and runner) with no LLM.
