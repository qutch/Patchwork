# Patchwork-plus: Development Roadmap

Derived from *Patchwork-plus: Technical Plan* (Nov 13, 2026). Hours are on the 36-hour clock.
**D1** capture & demo apps · **D2** spec/compiler/runner (the spine) · **D3** agents · **D4** control plane, UI & pitch.

**Never cut:** running with AI off · Break 1 repaired with an approval · a visible agent plan.

| Phase | Hours | Milestone (demo-able proof) | Status |
|---|---|---|---|
| 0. Kickoff | 0–3 | Open decisions closed; fixtures committed | fixtures ✅, model ✅, team decisions ⏳ |
| **1. MVP: the spine** | 0–12 | **H12: 100 records, 0 LLM calls**; Break 1 *detected* | ✅ done |
| **2. BUILD loop** | 12–20 | **H20: record → spec → code → run, live** | ✅ done |
| **3. REPAIR loop** | 20–28 | **H28: Break 1 repaired with approval** | ✅ done (+ Break 2) |
| 4. Benchmarks & hardening | 28–32 | Benchmark page, Break 2 (stretch) | ✅ Break 2, `make smoke`, `make bench` + Benchmark tab; manual/computer-use still estimates until timed |
| 5. Freeze & rehearse | 32–36 | Code freeze H32, 5 rehearsals, backup video, Devpost | |

---

## Phase 0: Kickoff (H0–3)

- [x] `fixtures/spec.yaml`: hand-written spec covering all 3 supplier formats (D2 builds against it, D3 uses it as the template the agent edits)
- [x] `fixtures/recording.json`: hand-made recording in the recorder's event format, built from seeded message `msg-0007` (D3 builds the interpreter against it)
- [x] `fixtures/examples.yaml`: confirmed samples → golden tests
- [x] **Model and provider**: OpenRouter, `nvidia/nemotron-3-ultra-550b-a55b:free` with `particle-ai/particle-1:free` as fallback (free tier: 20 req/min, 1,000/day). Key lives only in the gitignored `.env`; the runner never sees it
- [x] **`deepagents` version pin**: `deepagents==0.7.38`, `langchain-openrouter==0.2.9` (full list and why interrupts are blocking tools in `agents/README.md`)
- [ ] **Spec v1 field names** signed off by all four people (see "Decisions the MVP made" below)
- [ ] Real business? Outreach owner and the Saturday-noon go/no-go
- [ ] Destination: Stockroom only, or Stockroom + Google Sheet
- [ ] Owners for D1–D4; final name (Patchwork vs GhostFlow)

## Phase 1: MVP, the deterministic spine ✅

Everything the plan says must work with the AI turned off. Each later phase plugs into this.

- [x] **Demo apps** (D1): Supplier Inbox `:8001` (HTML inbox + `GET /api/messages?since=`), Stockroom `:8002` live and `:8003` staging (product list, "Add incoming stock" form submitting via `fetch`, live `/openapi.json`, `POST /admin/schema {version: 2}`)
- [x] **Seed + ground truth** (D1): 150 messages, 3 supplier formats; 120 valid, 10 resends, 9 zero-qty, 9 unknown SKUs, 2 junk. Deterministic (`demo_apps/seed.py`)
- [x] **Spec models** (D2): Pydantic v2, closed vocabulary, agent-readable errors; `validate_spec(text) -> [errors]`
- [x] **Compiler** (D2): Jinja → `integration.py`, `parsers.py`, `adapters.py`, `transforms.py`, `config.yaml`, `manifest.json`, OpenAPI snapshot. Deterministic, byte-identical output; versions immutable
- [x] **Generated tests** (D2): golden, negative (one mutation per validation rule), dedupe, contract (against current OpenAPI via `$PATCHWORK_SINK_OPENAPI`), no-AI
- [x] **Runner** (D2): `python -m runner run <name> [--version] [--dry-run] [--limit]`; preflight, per-record pipeline, dedupe via `processed` table, cursor, quarantine with reason codes, circuit breaker (3× `SINK_CONTRACT`), quarantine threshold (3× `PARSE_NO_MATCH` per sender → incident), JSONL log, `llm_calls: 0`
- [x] **Sandboxed launch**: subprocess, timeout, scratch cwd, stripped env (no model keys)
- [x] **Versions + approval gate**: `deploy` refuses unless the full suite passed on that exact version; deploy and rollback only move the pointer
- [x] **Break 1 detection**: Stockroom v2 → preflight blocks the run before any write and opens a `CONTRACT_CHANGED` incident with old and new OpenAPI plus actionable errors
- [x] **Benchmark accuracy**: `python -m bench.accuracy --live po_to_stockroom` → exact accuracy vs ground truth (currently **150/150**)
- [x] **Test suite**: 29 tests, including a full e2e test against real servers

**Not yet in the MVP:** agents, live recorder, matcher, FastAPI control plane/SSE, React UI, replay of quarantined records, version diffs.

## Phase 2: BUILD loop (H12–20) → *record → spec → code → run, live* ✅

Built by four parallel workstreams and merged: **Capture** (`capture/`, `matcher/`, Inbox admin endpoints),
**Agents** (`agents/deep.py`: deepagents + OpenRouter; `agents/scripted.py`: the no-LLM fallback),
**Control plane** (`control/api/`: FastAPI + SSE, runs, incidents, versions/diffs, demo controls),
**UI** (`web/`: five tabs). `make up` + `make web` runs it; `make smoke` drives the whole demo over HTTP.
The table below is the original plan, kept for reference.

| Owner | Work | Plugs into |
|---|---|---|
| D1 | `capture/recorder.js` + Playwright launcher (`add_init_script`, `expose_binding('pwRecord')`, `page.on('request')`), redaction, allowlist. Output must match `fixtures/recording.json` | writes `workspace/recordings/<id>/recording.json` |
| D1 | `matcher/` (~200 lines, no LLM): destination values from mutating requests → find them in source GET bodies → provenance + candidate regex → `candidates.json` | interpreter input |
| D3 | Supervisor (`create_deep_agent`) + **interpreter** subagent: `get_recording_summary`, `get_candidates`, `read_openapi`, `fetch_source_samples`, `test_parser`, `validate_spec` | `spec.validate_spec`, `spec.parse_spec` |
| D3 | **builder** + **tester** subagents: `compile_spec`, `run_tests`, `dry_run(limit)`, `add_test_case` | `compiler.compile.compile_spec`, `control.ops.run_tests`, `control.ops.run_sandboxed(dry_run=True)` |
| D3 | `ask_user` (LangGraph `interrupt()`), `request_deploy` in `interrupt_on`; checkpointer | `control.ops.deploy` (already refuses untested versions) |
| D4 | FastAPI control plane: `POST /recordings`, `/recordings/{id}/stop`, `/integrations/build`, `GET /agent/{thread}/stream` (SSE), `/agent/{thread}/resume`, `POST /runs`, `GET /runs/{id}` | wraps `control/ops.py` |
| D4 | React (Vite/TS/Tailwind) **Runs** tab (AI on/off toggle, live counters, `LLM calls: 0` badge, incident banner), then **Review** + **Agent Activity** tabs | |

Tool → MVP function mapping (already built, so agents get guardrails for free):

| Agent tool | Function | Guardrail already enforced |
|---|---|---|
| `validate_spec` | `spec.validate_spec(text)` | closed vocabulary; every error is a readable string |
| `compile_spec` | `compiler.compile.compile_spec` | refuses invalid specs, missing `custom:` fns, existing versions |
| `run_tests` | `control.ops.run_tests` | records pass/fail per exact version |
| `dry_run` | `control.ops.run_sandboxed(dry_run=True)` | staging sink only; errors if no `staging_base_url` |
| `request_deploy` | `control.ops.deploy` | refuses unless tests passed on that version |

## Phase 3: REPAIR loop (H20–28) → *Break 1 repaired with approval* ✅

- [x] D3: REPAIR mode (same supervisor, first message = incident) + **repairer** subagent: `get_incident`, `get_quarantined(limit)`, `diff_openapi(old,new)`, `read_openapi`, `test_parser`, `propose_spec_patch` (JSON Patch on the spec only). Max 3 attempts, then escalate
- [x] D3: Break 1 script: auto-map `product_id→sku`, `quantity→quantity_delta`; `ask_user` "Always BOS, or by supplier?" → `lookup` on `record.sender_domain` (already supported and tested in `test_compiler.py`)
- [x] D2: version diff endpoint (spec JSON Patch + unified code diff); **replay quarantined records** after deploy (raw records are already stored in `records.raw`; idempotency keys make the replay safe)
- [x] D2: v(N+1) golden tests: update expected bodies for the new contract and add tests made from quarantined records
- [x] D4: **Integration** tab (versions, code viewer, test results, diffs) + Approve/Reject cards for `ask_user` and `request_deploy`
- [x] D1: Break 2 data (4th supplier, `Item code: … / 2 cases (12/case)`). Sheets adapter: not built (only if a real business commits)

## Phase 4: Benchmarks & hardening (H28–32)

- [x] D1: benchmark runs: Patchwork measured on 100 (`make bench` -> `bench/results.json`, every number labelled `measured | measured (earlier run) | extrapolated | estimate | not measured` with a source)
- [x] Baseline: a one-shot script written by a fresh Claude session from one prompt (`bench/baseline_prompt.md` -> `bench/baseline_script.py`), run through the same scenario and scored against ground truth
- [ ] D1: manual (time 5, extrapolate): `python -m bench.stopwatch manual` is ready; until someone runs it, results.json uses a labelled 1 min/record estimate
- [ ] D1: computer-use agent (run 5, extrapolate): no API key, so a labelled 3 min/record estimate citing OSWorld-Human (arXiv:2506.16042); `python -m bench.stopwatch computer_use` records real runs
- [x] D4: benchmark page (web console tab 6, `GET /api/bench/results`): break-even chart, breaks caught before any bad write, repair time, all numbers with their kind
- [ ] D4: slides
- [x] D2: hardening (store migration/lock races, run-busy race, reset cancels agent threads, stale-run recovery on restart). Live-demo fallback: `PATCHWORK_AGENT=scripted` runs both breaks with no model
- [x] D3: Break 2: new parser with `combine: product` (`multiply` only takes a constant, and units per case vary), case math confirmed with the user, warehouse asked when routing by supplier

## Phase 5: Freeze (H32–36)

- [ ] Code freeze at H32 · 5 full rehearsals of the 4-minute script · backup video · Devpost

## Cut order (first to go)

1. Break 2 · 2. Sheets / real business · 3. Playwright UI fallback sink (mention, don't build) · 4. Tester's extra test generation · 5. Polished diffs (raw unified diffs) · 6. Live recorder (play back `fixtures/recording.json` and say so)

## Decisions the MVP made (confirm at the spec sign-off)

- **Examples live beside the spec** (`examples.yaml`), not inside it. They feed golden tests without bloating spec diffs. The compiler copies them into `golden.json`.
- **`from: record.<field>`** reads raw source fields (e.g. `record.sender_domain`). Break 1's per-supplier `warehouse_id` lookup needs this.
- **`sink.staging_base_url`** added to the spec, so `dry_run` has a staging target to use.
- **Outcomes are `success | duplicate | quarantined(reason)`**. The plan's separate `invalid` is folded into `quarantined(VALIDATION_FAILED)`.
- **Storage uses stdlib `sqlite3`**, not SQLModel. It's one file (`runner/store.py`) and easy to swap.
- **Supplier formats are fixed:** Acme is label-style (`PO Number:/SKU:/Qty:`), Globex is prose (`receive N units of item X`), Initech is ERP key=value (`REF=/PART=/UNITS=`).
