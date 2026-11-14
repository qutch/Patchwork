"""DeepAgentService: the BUILD and REPAIR loops on deepagents + OpenRouter (PATCHWORK_AGENT=deep).

One supervisor (`create_deep_agent`) with subagents interpreter, builder, tester and repairer. The tools
in agents/deep_tools.py wrap the existing spec/compiler/ops code and enforce every guardrail.

Event mapping (agents.interface.EVENT_TYPES) is done by `EventMiddleware`, installed on the supervisor and
on each subagent so every event knows which agent produced it:
  write_todos -> todos · task -> subagent_start/subagent_end · any other tool -> tool_call/tool_result ·
  short assistant text -> message.

Human-in-the-loop: `ask_user` and `request_deploy` are tools that call `ThreadedAgentService.interrupt`,
which emits the event and blocks this agent thread until the control plane calls `resume`. See
agents/README.md for why this beats LangGraph `interrupt()` + `interrupt_on` here.

Models: PATCHWORK_MODEL (primary) and PATCHWORK_MODEL_FALLBACK via ChatOpenRouter, with a shared rate
limiter, backoff on 429/timeouts/5xx, then a sticky switch to the fallback (`ResilientModelMiddleware`).
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

from deepagents import FilesystemMiddleware, FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents import prompts
from agents.deep_tools import FAIL_PREFIXES, ToolContext, build_tools, seed_repair_workdir
from agents.interface import ThreadCancelled, ThreadedAgentService, _Thread
from agents.scripted import NAME, work_dir
from runner.store import Store

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
DEFAULT_FALLBACK = "particle-ai/particle-1:free"

# The supervisor plans and delegates. It cannot ask the user or edit files: in a live run it asked its own
# (vaguer) warehouse question and patched the spec, then the repairer asked again and got a different answer.
SUPERVISOR_TOOLS = ["request_deploy", "get_incident", "validate_spec", "run_tests"]
SUBAGENTS: dict[str, tuple[str, str, list[str]]] = {
    "interpreter": ("Recording + candidates -> spec.yaml draft with parsers tested on real samples; asks the user to confirm assumptions.",
                    prompts.INTERPRETER,
                    ["get_recording_summary", "get_candidates", "read_openapi", "fetch_source_samples", "test_parser",
                     "draft_spec", "propose_spec_patch", "validate_spec", "add_test_case", "ask_user"]),
    "builder": ("Compiles spec.yaml into the next immutable version and runs its full generated test suite.",
                prompts.BUILDER, ["validate_spec", "compile_spec", "run_tests"]),
    "tester": ("Dry-runs the latest passing version against the staging sink; adds edge-case golden examples.",
               prompts.TESTER, ["run_tests", "dry_run", "add_test_case", "test_parser"]),
    "repairer": ("Incident -> diagnosis -> JSON Patch on spec.yaml (+ golden examples migrated); asks the user what it cannot infer.",
                 prompts.REPAIRER,
                 ["get_incident", "get_quarantined", "diff_openapi", "read_openapi", "test_parser", "propose_spec_patch",
                  "migrate_golden_examples", "validate_spec", "ask_user"]),
}
FS_TOOLS = ["ls", "read_file", "write_file", "edit_file"]
READ_ONLY_FS_TOOLS = ["ls", "read_file"]
WRITABLE = ["/spec.yaml", "/examples.yaml", "/transforms.py"]


# ---- models --------------------------------------------------------------------------------

_RATE_LIMITER = None


def _rate_limiter():
    global _RATE_LIMITER
    if _RATE_LIMITER is None:
        from langchain_core.rate_limiters import InMemoryRateLimiter
        rps = float(os.environ.get("PATCHWORK_MODEL_RPS", "0.3"))  # 18/min, under OpenRouter's 20/min free cap
        _RATE_LIMITER = InMemoryRateLimiter(requests_per_second=rps, check_every_n_seconds=0.1, max_bucket_size=2)
    return _RATE_LIMITER


def make_model(name: str) -> BaseChatModel:
    from langchain_openrouter import ChatOpenRouter
    return ChatOpenRouter(model=name, temperature=0, max_retries=0, rate_limiter=_rate_limiter(),
                          timeout=int(float(os.environ.get("PATCHWORK_MODEL_TIMEOUT_S", "120")) * 1000),
                          app_title="Patchwork")


def is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in (408, 409, 425, 429, 500, 502, 503, 504):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in ("429", "rate limit", "ratelimit", "timeout", "timed out", "temporarily",
                                   "overloaded", "502", "503", "504", "connection"))


class _Budget:
    def __init__(self, max_calls: int):
        self.max_calls, self.calls, self.lock = max_calls, 0, threading.Lock()
        self.on_fallback = False
        self.by_model: dict[str, int] = {}

    def take(self, model_name: str) -> None:
        with self.lock:
            if self.calls >= self.max_calls:
                raise RuntimeError(f"model call budget exhausted ({self.max_calls} calls); stopping this thread")
            self.calls += 1
            self.by_model[model_name] = self.by_model.get(model_name, 0) + 1


def _model_name(m: Any) -> str:
    return getattr(m, "model_name", None) or getattr(m, "model", None) or type(m).__name__


class ResilientModelMiddleware(AgentMiddleware):
    """Counts model calls against a per-thread budget, backs off on 429/timeouts/5xx, then switches
    (stickily, for the whole thread) to the fallback model."""

    def __init__(self, budget: _Budget, fallback: BaseChatModel | None, on_switch: Callable[[str], None],
                 check_cancelled: Callable[[], None], retries: int = 2, base_delay: float = 3.0):
        super().__init__()
        self.budget, self.fallback, self.on_switch = budget, fallback, on_switch
        self.check_cancelled = check_cancelled
        self.retries, self.base_delay = retries, base_delay

    def wrap_model_call(self, request, handler):
        if self.budget.on_fallback and self.fallback is not None:
            request = request.override(model=self.fallback)
        attempt = 0
        while True:
            self.check_cancelled()  # a reset stops the thread before it spends another model call
            self.budget.take(_model_name(request.model))
            try:
                return handler(request)
            except Exception as e:  # noqa: BLE001
                if not is_retryable(e):
                    raise
                attempt += 1
                if attempt <= self.retries:
                    time.sleep(self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1))
                    continue  # (check_cancelled runs again at the top of the loop)
                if self.fallback is None or self.budget.on_fallback:
                    raise
                self.budget.on_fallback = True
                self.on_switch(f"{_model_name(request.model)} failed ({type(e).__name__}); switching to {_model_name(self.fallback)}")
                request, attempt = request.override(model=self.fallback), 0


# ---- event mapping -------------------------------------------------------------------------

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _short(s: str, n: int) -> str:
    s = " ".join(s.split()) if "\n" not in s[:n] else s
    return s if len(s) <= n else s[: n - 1] + "…"


def _result_text(result: Any) -> str:
    if isinstance(result, ToolMessage):
        return _text(result.content)
    if isinstance(result, Command) and isinstance(result.update, dict):
        msgs = result.update.get("messages") or []
        return _text(msgs[-1].content) if msgs else ""
    return str(result)


def _args_for_event(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        s = json.dumps(v, default=str)
        out[k] = v if len(s) <= 400 else s[:400] + "…"
    return out


class EventMiddleware(AgentMiddleware):
    """Turns one agent's model turns and tool calls into interface events."""

    def __init__(self, svc: ThreadedAgentService, t: _Thread, agent: str):
        super().__init__()
        self.svc, self.t, self.agent = svc, t, agent

    @property
    def name(self) -> str:  # unique per instance so subagent stacks don't collide
        return f"PatchworkEvents_{self.agent}"

    def after_model(self, state, runtime):
        msgs = state.get("messages") or []
        if msgs and isinstance(msgs[-1], AIMessage):
            text = _text(msgs[-1].content).strip()
            if text:
                self.svc.emit(self.t, "message", agent=self.agent, text=_short(text, 600))
        return None

    def wrap_tool_call(self, request, handler):
        self.svc.check_cancelled(self.t)  # no tool runs (no files, no DB) after a reset
        call = request.tool_call
        tool, args = call.get("name"), call.get("args") or {}
        if tool == "task":
            name = args.get("subagent_type", "?")
            self.svc.emit(self.t, "subagent_start", name=name, task=_short(str(args.get("description", "")), 500))
            try:
                result = handler(request)
            except Exception as e:
                self.svc.emit(self.t, "subagent_end", name=name, summary=f"failed: {type(e).__name__}: {e}")
                raise
            self.svc.emit(self.t, "subagent_end", name=name, summary=_short(_result_text(result), 400))
            return result
        if tool == "write_todos":
            result = handler(request)
            todos = [{"content": td.get("content", ""), "status": td.get("status", "pending")}
                     for td in args.get("todos", []) if isinstance(td, dict)]
            self.svc.emit(self.t, "todos", todos=todos)
            return result
        self.svc.emit(self.t, "tool_call", agent=self.agent, tool=tool, args=_args_for_event(args))
        try:
            result = handler(request)
        except Exception as e:
            self.svc.emit(self.t, "tool_result", agent=self.agent, tool=tool, ok=False, summary=f"{type(e).__name__}: {e}")
            raise
        text = _result_text(result)
        ok = not (getattr(result, "status", None) == "error" or text.lstrip().upper().startswith(FAIL_PREFIXES)
                  or text.lstrip().startswith("Error"))
        self.svc.emit(self.t, "tool_result", agent=self.agent, tool=tool, ok=ok, summary=_short(text, 400))
        return result


def _no_general_purpose_subagent(model: BaseChatModel) -> None:
    """create_deep_agent auto-adds a `general-purpose` subagent unless the model's harness profile turns
    it off; the supervisor should only delegate to our four subagents."""
    from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
    from deepagents._models import get_model_identifier, get_model_provider
    provider, ident = get_model_provider(model), get_model_identifier(model)
    if provider and ident:
        register_harness_profile(f"{provider}:{ident}", HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))


# ---- the service ---------------------------------------------------------------------------

class DeepAgentService(ThreadedAgentService):
    def __init__(self, root: Path | None = None, *, model: BaseChatModel | None = None,
                 fallback_model: BaseChatModel | None = None, name: str | None = None,
                 max_model_calls: int | None = None, max_nudges: int = 2, retry_base_delay: float = 3.0):
        super().__init__(root)
        self._model, self._fallback = model, fallback_model
        self.name = name or os.environ.get("PATCHWORK_INTEGRATION_NAME", NAME)
        self.max_model_calls = max_model_calls or int(os.environ.get("PATCHWORK_AGENT_MAX_CALLS", "80"))
        self.max_nudges = max_nudges
        self.retry_base_delay = retry_base_delay
        self.model_calls: dict[str, int] = {}

    def interrupt(self, t: _Thread, type, **payload: Any) -> dict[str, Any]:
        """Same contract as the base class, but the wait gives up when the interpreter shuts down:
        LangGraph runs tools on non-daemon executor threads, so an unanswered question would otherwise
        keep the process alive forever."""
        import uuid
        interrupt_id = f"int-{uuid.uuid4().hex[:8]}"
        t.wake.clear()
        t.answer = None
        ev = self.emit(t, type, interrupt_id=interrupt_id, **payload)
        t.pending = ev
        self._set_state(t, "waiting")
        while not t.wake.wait(0.5):
            if getattr(threading, "_SHUTTING_DOWN", False) or not threading.main_thread().is_alive():
                raise ThreadCancelled(t.id)  # process shutting down while waiting for the user
        self.check_cancelled(t)  # reset() wakes us with t.cancelled set
        return t.answer or {}

    # model selection is lazy so importing this module never needs a key
    def models(self) -> tuple[BaseChatModel, BaseChatModel | None]:
        if self._model is not None:
            return self._model, self._fallback
        primary = os.environ.get("PATCHWORK_MODEL", DEFAULT_MODEL)
        fallback = os.environ.get("PATCHWORK_MODEL_FALLBACK", DEFAULT_FALLBACK)
        return make_model(primary), (make_model(fallback) if fallback and fallback != primary else None)

    def build_agent(self, t: _Thread, ctx: ToolContext, budget: _Budget):
        model, fallback = self.models()
        _no_general_purpose_subagent(model)
        tools = build_tools(ctx)
        backend = FilesystemBackend(root_dir=str(ctx.wd), virtual_mode=True)
        permissions = [FilesystemPermission(operations=["write"], paths=WRITABLE, mode="allow"),
                       FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]

        def resilient() -> ResilientModelMiddleware:
            return ResilientModelMiddleware(budget, fallback, lambda m: self.emit(t, "message", agent="supervisor", text=m),
                                            lambda: self.check_cancelled(t), base_delay=self.retry_base_delay)

        def fs() -> FilesystemMiddleware:
            return FilesystemMiddleware(backend=backend, tools=FS_TOOLS, permissions=permissions)

        subagents = []
        for sname, (desc, prompt, tool_names) in SUBAGENTS.items():
            subagents.append({
                "name": sname, "description": desc, "system_prompt": prompt,
                "tools": [tools[n] for n in tool_names],
                "middleware": [fs(), EventMiddleware(self, t, sname), resilient()],
            })
        return create_deep_agent(
            model=model,
            tools=[tools[n] for n in SUPERVISOR_TOOLS],
            system_prompt=prompts.SUPERVISOR,
            middleware=[FilesystemMiddleware(backend=backend, tools=READ_ONLY_FS_TOOLS, permissions=permissions),
                        TodoListMiddleware(), EventMiddleware(self, t, "supervisor"), resilient()],
            subagents=subagents,
            backend=backend,
            permissions=permissions,
            checkpointer=InMemorySaver(),
            name="supervisor",
        )

    def _drive(self, t: _Thread, ctx: ToolContext, first_message: str) -> dict[str, Any]:
        budget = _Budget(self.max_model_calls)
        agent = self.build_agent(t, ctx, budget)
        cfg = {"configurable": {"thread_id": t.id}, "recursion_limit": 200}
        t0 = time.monotonic()
        message = first_message
        try:
            for attempt in range(self.max_nudges + 1):
                agent.invoke({"messages": [HumanMessage(content=message)]}, cfg)
                if ctx.deploy_requested or ctx.stopped:
                    break
                if attempt < self.max_nudges:
                    self.emit(t, "message", agent="supervisor", text="(harness) No deploy requested yet; nudging the supervisor.")
                    message = prompts.NUDGE
        finally:
            self.model_calls[t.id] = budget.calls
        result: dict[str, Any] = {"model_calls": budget.calls, "calls_by_model": budget.by_model,
                                  "seconds": round(time.monotonic() - t0, 1),
                                  **(ctx.deploy_result or {"deployed": False})}
        if ctx.last_version:
            result.setdefault("version", ctx.last_version["version"])
        if not ctx.deploy_requested and not ctx.stopped:
            raise RuntimeError(f"agent finished without requesting a deploy ({json.dumps(result)})")
        return result

    def _run_build(self, t: _Thread, recording_id: str) -> Any:
        ctx = ToolContext(self, t, name=self.name, recording_id=recording_id)
        work_dir(self.name)
        return self._drive(t, ctx, prompts.BUILD_TASK.format(recording_id=recording_id, name=self.name))

    def _run_repair(self, t: _Thread, incident_id: int) -> Any:
        inc = Store().incident(incident_id)
        if inc is None:
            raise KeyError(f"incident {incident_id} not found")
        name = inc["integration"]
        seed_repair_workdir(name)
        ctx = ToolContext(self, t, name=name, incident_id=incident_id)
        msg = prompts.REPAIR_TASK.format(incident_id=incident_id, name=name, active=Store().active_version(name),
                                         incident_type=inc["type"], summary=inc["summary"])
        return self._drive(t, ctx, msg)
