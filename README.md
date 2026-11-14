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
| `bench/` | Exact accuracy against seed ground truth |
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
