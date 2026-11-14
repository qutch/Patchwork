# agents/

`PATCHWORK_AGENT=scripted` (default) or `deep`. Both implement `agents.interface.AgentService`
(see `docs/contracts.md` section 3) and emit exactly the events in `EVENT_TYPES`.

| File | What |
|---|---|
| `interface.py` | the contract + `ThreadedAgentService` (event log, interrupts, resume, reset) |
| `scripted.py` | no-LLM storyline; tests, UI dev, live-demo fallback |
| `scripted_v2.py` | the v2 scripted storylines (LEARN, TEACH page→form, form REPAIR); call the real tool functions |
| `deep.py` | `DeepAgentService`: deepagents supervisor + 4 subagents (BUILD/REPAIR) or 1 `learner` (LEARN) on OpenRouter |
| `deep_tools.py` | the tools, and every guardrail (`tool_functions(ctx)` = plain functions, no langchain import) |
| `learn.py` | pure LEARN checks: page outline, evidence, corroboration, ambiguity, the D2 rule |
| `prompts.py` | supervisor/subagent prompts (steering only; nothing safety-relevant) |

## Configuration

| Env | Default | |
|---|---|---|
| `PATCHWORK_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | primary |
| `PATCHWORK_MODEL_FALLBACK` | `particle-ai/particle-1:free` | used after 429/timeout/5xx backoff (sticky per thread) |
| `OPENROUTER_API_KEY` | | required for `deep` |
| `PATCHWORK_MODEL_RPS` | `0.3` | shared client-side rate limit (18/min, under the free 20/min) |
| `PATCHWORK_MODEL_TIMEOUT_S` | `120` | per request |
| `PATCHWORK_AGENT_MAX_CALLS` | `80` | per-thread model-call budget; the thread fails rather than overspend |
| `PATCHWORK_INTEGRATION_NAME` | `po_to_stockroom` | BUILD target name (REPAIR uses the incident's integration; page→form recordings default to `jobs_to_tracker`) |
| `PATCHWORK_LEARN_MAX_CALLS` | `12` | LEARN's hard model-call cap (target ≤ 8) |

## Model choice (hour-0 spike, 2026-11-14)

Pinned: `deepagents 0.7.38`, `langchain 1.5.0`, `langchain-core 1.8.0`, `langgraph 1.2.22`,
`langchain-openrouter 0.2.9` (SDK `openrouter 0.11.46`), `langgraph-checkpoint-sqlite 3.1.3`, `jsonpatch 1.47`.

| Task | nemotron-3-ultra (primary) | particle-1 (fallback) |
|---|---|---|
| hello-world deep agent + `interrupt()`/resume | ok, 3 calls, 8.3 s | ok, 3 calls, 2.6 s |
| fix a broken spec from `validate_spec` errors | valid in 1 call, 9.8 s | valid in 2 calls, 5.3 s |
| Break 1 JSON Patch from an OpenAPI diff (x2) | valid 2/2; exact 1/2 (added a harmless `int`), 21–27 s | valid and exact 2/2, 2–3 s |
| **live BUILD (fixture recording)** | **pass: 35 calls, 130 s** | not run (budget) |
| **live REPAIR (Break 1)** | **pass: 19 calls, 67 s** | **pass: 24 calls, 83 s** |

**Default: nemotron.** It ran the whole scenario on the first try with clean delegation and the fewest calls.
The first live run of its v1 matched ground truth exactly (120 success, 10 duplicate, 9 NOT_FOUND,
9 VALIDATION_FAILED, 2 PARSE_NO_MATCH). particle-1 is 3–10x faster per call and also completes REPAIR, so it
is a proven fallback. To demo faster, set `PATCHWORK_MODEL=particle-ai/particle-1:free`.
Note: deepagents ships a Nemotron 3 Ultra harness profile keyed `openrouter:nvidia/nemotron-3-ultra-550b-a55b`.
The `:free` id doesn't match it, so it isn't applied. It wasn't needed.

## deepagents 0.7.38 vs the plan

- `create_deep_agent(model, tools, *, system_prompt, middleware, subagents, skills, memory, permissions, backend,
  interrupt_on, response_format, state_schema, context_schema, checkpointer, store, debug, name, cache)`.
  The plan's names (`system_prompt`, `subagents`, `backend`, `interrupt_on`, `checkpointer`) are right.
- **`write_todos` is no longer in the default stack.** Add `langchain.agents.middleware.TodoListMiddleware()`.
- Built-in tools are `ls, read_file, write_file, edit_file, delete, glob, grep, task` (`execute` only with a sandbox
  backend). We pass `FilesystemMiddleware(tools=["ls","read_file","write_file","edit_file"])` to replace the default.
- A `general-purpose` subagent is auto-added. Turning it off takes a `HarnessProfile(general_purpose_subagent=
  GeneralPurposeSubagentProfile(enabled=False))` registered for the model's `provider:model` key (`deep._no_general_purpose_subagent`).
- `FilesystemBackend(root_dir, virtual_mode=True)`: `virtual_mode` now defaults to True and paths are `/spec.yaml`.
  `permissions=[FilesystemPermission(operations=["write"], paths=[...], mode="allow"|"deny"|"interrupt")]`, first match wins.
- Subagent dict: `name`, `description` (required), plus `system_prompt, tools, model, middleware, interrupt_on, skills,
  permissions, response_format, mode ("isolated"|"fork")`. A subagent with no `tools` inherits the parent's.
- Subagents run inside the `task` tool. Rather than parse `stream(subgraphs=True)` namespaces, each agent
  (supervisor and every subagent) gets its own `EventMiddleware` (`wrap_tool_call` + `after_model`), so every event
  knows its agent: `write_todos`→`todos`, `task`→`subagent_start/end`, other tools→`tool_call/tool_result`,
  assistant text→`message`.
- `ChatOpenRouter(timeout=...)` is in **milliseconds**. `max_retries` uses SDK backoff of up to 150 s per retry, so we set it to 0
  and retry in `ResilientModelMiddleware` (backoff 3 s/6 s, then a sticky switch to the fallback).

## Interrupts: blocking tools, not LangGraph `interrupt()`

`interrupt()` + `Command(resume=…)` works (spike), but here `ask_user` and `request_deploy` are ordinary tools that call
`ThreadedAgentService.interrupt()`. That method emits the event and blocks the agent thread until `resume()`. Reasons:
- It is the interface's own mechanism. The control plane resumes deep and scripted threads the same way, and
  `reset()` already knows how to wake them.
- It works the same when a subagent asks (the repairer asks inside a `task` call). LangGraph would re-run the whole
  tool node (and the subagent) on resume, so any side-effecting tool batched with it would run twice.
- `request_deploy` has to check "tests passed on this exact version" anyway. Doing that, building the diffs, asking,
  deploying and replaying in one tool keeps the guardrail and the approval atomic.

Trade-off: a pending question doesn't survive a process restart (true of the whole `ThreadedAgentService` already).
The wait gives up on `reset()` (`ThreadCancelled`) and on interpreter shutdown. LangGraph runs tools on non-daemon
executor threads, so an unanswered question would otherwise keep the process alive.

## Guardrails (in `deep_tools.py`, tested in `tests/agents/test_deep_tools.py`)

- No live writes: no tool takes a URL. `dry_run` uses `run_sandboxed(dry_run=True)` and refuses when staging equals live.
  Spec patches can't change base URLs or set `staging_base_url` to the live URL.
- `compile_spec` refuses invalid specs (schema + lint, e.g. lookup tables must cover every routed `sender_domain`).
- `request_deploy` refuses unless that exact version passed its full suite. `control.ops.deploy` checks again.
- Only `/spec.yaml`, `/examples.yaml` and `/transforms.py` are writable (filesystem permissions). `propose_spec_patch` is
  JSON Patch on the spec and writes only if the result validates. Runtime code is always regenerated by the compiler.
- REPAIR: after 3 failed compile attempts, `compile_spec` escalates via `ask_user` ("Stop" or "Try 3 more times").
- Per-thread model-call budget. `reset()` is honoured before every model call and every tool.

Deterministic helpers keep the model's job small: `draft_spec` (candidates → acme-style spec, `exists` rule found by
probing the sink's `GET /api/products/{x}`, `verify` from the sink's query params), `test_parser` (runs regexes on
real samples with the runner's own extract/cast code), `diff_openapi` (renames by name, by feeding field, then by
unique type, plus the exact `suggested_questions`), and `migrate_golden_examples` (moves expected bodies to the new contract).

Candidates come from `workspace/recordings/<id>/candidates.json`, then `matcher.match`, then
`fixtures/agent_candidates.json` (fixture recording only).

## v2: LEARN, TEACH page→form, form REPAIR (contracts V10)

- **LEARN** (`start_learn(page_id, integration)`, thread `learn-…`): supervisor (todos + `task` + read-only fs, no other
  tools) → `learner` (`get_page`, `test_page_parser`, `ask_user`, `save_parser`, `add_test_case`; fs writes only
  `/spec.yaml`). The learner never sees HTML: `get_page` returns `learn.page_outline` (≤ 4,000 chars, every line an
  extractor and its value). `save_parser` re-runs every check in code, refuses unevidenced values before compiling, asks
  ambiguity questions itself, compiles, and applies D2: auto-deploy only if policy `auto` + tests passed + evidence +
  corroboration + no ambiguity + the patch only adds one parser; otherwise `request_deploy`. Neither agent can call
  `request_deploy` in LEARN.
- **TEACH page→form**: `draft_spec` turns V11 candidates into a V7/V5 spec with one taught parser + a golden from the snapshot.
- **REPAIR on a form**: `diff_openapi` returns `patch_hint` (`move` of the renamed `sink.fields` key) and asks only when
  the rename isn't unique; `migrate_golden_examples` carries values over; `request_deploy` always.
- **Scripted fallback** (`scripted_v2.py`): same event types, same tool functions; `propose_parser` picks extractors with a
  fixed preference order (JSON-LD JobPosting, a single-valued `h1`, meta, `<title>` refine), so a learn on stage works
  with no model and the checks still decide.
- W1/W4a seams (until merged): `ToolContext.validate_spec_text`, `compile_and_test`, `fetch_contract`, `get_page_record`.
  Offline tests replace them with `tests/agents/jobs.py::FakeEngine` (real Store versions, real `ops.deploy`).

## Tests

- Offline (default `pytest -q`): `tests/agents/test_deep_agent.py` drives the full BUILD→approve→run→Break 1→REPAIR→approve
  scenario with a scripted tool-calling model (`tests/agents/fake_model.py`). It checks event mapping, the tools each
  agent gets, filesystem write limits and `reset()`. `test_deep_tools.py` covers the guardrails. These modules import
  langchain lazily and run last, because the runner refuses to start in a process with AI modules loaded.
- Live (opt in): `pytest -m live -s tests/agents/test_deep_live.py` (needs `OPENROUTER_API_KEY`):
  `test_live_build_then_repair` (scripted-agent scenario on a real model) and `test_live_repair_only` (scripted v1, real REPAIR).

## Known limits / demo risks

- Free models are shared and rate-limited (1,000/day, 20/min for the whole team). One full scenario costs about 55 calls.
- Integration name defaults to `po_to_stockroom` for every BUILD (no slug from the intent yet).
- The model can still word things freely (summaries, todo text). The questions that matter come from tools.
