# One command to run Patchwork (`./patchwork`): design

**Owner's ask (2026-11-14):** "I want to be able to run a single command in the terminal and it spins up. I don't
want to have to deal with all the localhost and port stuff; that should be dealt with automatically." The owner then
approved this design and decided that `./patchwork` is also how the demo runs.

## Why

Today, running Patchwork takes three pieces of setup:
- a control plane (`python -m control.api --port N`) with a `PATCHWORK_WORKSPACE`;
- a separate Vite UI dev server on another port, pointed at the first with `PATCHWORK_API`;
- the extension's options page, where someone types the address and pastes a token.

The first live rehearsal stalled on exactly this.

## Goals

- **One command:** `./patchwork` at the repo root starts everything and opens the browser. Ctrl-C stops everything.
- **No ports or addresses for the person.** One process serves the API and the UI on one port. The extension finds
  it and its token by itself.
- **It is the demo's launcher.** It replaces `make stage-v2`'s role, and prefers port 38091 so the existing stage
  setup carries over.
- **First run sets itself up** if needed: the Python venv (uv) and the web packages (npm). It builds the UI.

## Non-goals and anti-goals

- **No Docker** (slower, and the extension would still need to find it).
- **No change to the runner** or to how integrations run.
- **Agents' and tests' port rules stay.** Agents and tests never bind 8000–8003, 5180 or 38091. The launcher's
  own tests use an injected port range.
- **Anti-goal:** an auto-chosen port that silently collides with the owner's other demo apps on 8000–8003 or 5180.

## Approaches considered

1. **(Chosen) One Python launcher, and the control plane serves the built UI.** One process, one port. The
   extension discovers it by probing a small fixed port range.
2. **A supervisor running both the API and the Vite dev server.** Two ports, hot reload. Rejected: still two ports,
   and Vite's proxy configuration is part of the problem.
3. **Docker Compose.** Rejected; see non-goals.

## Design

### The command

- `./patchwork` is an executable POSIX `sh` wrapper at the repo root.
  - It finds `.venv/bin/python`. If that is missing, it runs `make setup`, or a smaller uv venv plus deps if
    Playwright isn't needed. It says what it is doing.
  - It then runs `exec .venv/bin/python -m scripts.patchwork "$@"`, or the equivalent module path.
- The launcher logic is in `scripts/patchwork.py`, a small, testable Python module.

| Subcommand | What it does |
|---|---|
| `./patchwork` (`start`) | Starts in the foreground. Prints one status block. Opens the browser. Ctrl-C stops it cleanly. |
| `./patchwork --background` | Starts detached. Writes a pid file and a log file in the workspace. Prints the URL. |
| `./patchwork stop` | Stops the copy recorded in the pid file. |
| `./patchwork status` | Running or not, the URL, the workspace, whether AI is on. |
| `./patchwork --fresh` | Uses a new, empty temporary workspace (rehearsals). |
| `--workspace PATH` | Uses that workspace. |
| `--port N` | Uses exactly this port, or fails clearly if it is taken. |
| `--no-open` | Doesn't open the browser. |
| `--no-ai` | Starts with AI off. |

### Ports

- The preferred port is **38091** (the demo stage). If it is taken:
  - **by Patchwork** (`GET /api/health` answers as Patchwork): say "Patchwork is already running at …", open
    the browser there, and exit 0;
  - **by something else:** use the next free port in **38092–38110**.
- It never picks 8000–8003 or 5180.
- The chosen URL is printed and written to `<workspace>/patchwork.url`.

### Workspace and environment

- The default workspace is `workspace-v2/` in the repo: the demo stage's existing workspace (gitignored), with
  its baseline.
- It loads `.env` the way the control plane already does (`control.api.settings.load_dotenv`).
- It sets `PATCHWORK_AGENT=deep` and `PATCHWORK_AI_ENABLED=1` (unless `--no-ai`), and `PATCHWORK_MODEL` from
  `.env` or the shell (Haiku on stage). These are the same as `make stage-v2`.

### One port: the control plane serves the UI

- **Serving:** when `web/dist/index.html` exists, the FastAPI app serves `web/dist` at `/`:
  - static assets are served as files;
  - any non-`/api` path falls back to `index.html`, for the SPA;
  - `/api/*` is untouched.

  When it doesn't exist, nothing changes, so tests are unaffected.
- **Rebuilding:** the launcher runs `npm run build` in `web/`, running `npm ci` first if `node_modules` is
  missing. It rebuilds only when the newest file under `web/src`, `web/index.html` or `web/package.json` is newer
  than `web/dist/index.html`.
- **API calls:** the UI already calls same-origin `/api/...`, so no UI change is needed.

### The extension finds Patchwork by itself

- **Address:**
  - The default `controlUrl` becomes "auto". On use, the service worker probes `http://127.0.0.1:{38091..38110}`
    with `GET /api/extension`, a short timeout, and in parallel. It takes the first that answers with a token.
  - It caches the URL and token in `chrome.storage.local`.
  - On a network failure or a 401, it rediscovers once and retries.
- **Token:** fetched from `/api/extension`, as the options page's "Get token" button does today.
- **Options page:**
  - shows "Connected automatically to http://127.0.0.1:N" when connected that way;
  - keeps an "Advanced: set the address yourself" field, so a manual address still works and wins.
- **Manifest:** `host_permissions` already allows `http://127.0.0.1/*`.

### Make and docs

- `make patchwork` runs `./patchwork`.
- `make stage-v2` runs `./patchwork --port 38091 --no-open`.
- `make web-stage` is no longer needed for the demo. Keep it for UI development (hot reload).
- Update:
  - `README.md`: a Quick start of `./patchwork`;
  - `docs/demo-v2.md`: the run sheet's setup uses `./patchwork`, and the reset steps use the printed URL;
  - `CLAUDE.md` Commands.

## Tests

- **Port choice** (unit, injected ranges and a fake "is it Patchwork" probe):
  - the preferred port is free;
  - it is taken by Patchwork (reuse);
  - it is taken by something else (next free);
  - the reserved ports are never chosen;
  - `--port` taken gives a clear error.
- **Static serving** (in-process, temp `dist`):
  - `/` serves `index.html`;
  - `/assets/x.js` serves the file;
  - `/integrations/foo` (an SPA route) serves `index.html`;
  - `/api/health` is unchanged;
  - with no `dist`, `/` is 404 as today.
- **Rebuild check:** stale versus fresh, from mtimes.
- **Lifecycle:** `start --background` then `status`, then `stop`, on a free high port with a temp workspace and
  `--no-open`. This is the only test that starts a real process. It must use an injected port range far from the
  reserved ports.
- **Extension discovery:** the discovery function is pure (`pwDiscover(probe, ports)`), tested with node's built-in
  test runner if the extension has no test setup yet.

## Left to the builder

- **Exact output wording:** one short block, no stack traces on Ctrl-C.
- **Log location:** `<workspace>/patchwork.log` in background mode.
- **How the wrapper detects a missing uv or node:** a clear message saying what to install.
