"""A scripted tool-calling chat model. The supervisor and every subagent share one instance, so the
script is simply the model turns in execution order."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


def call(name: str, **args: Any) -> dict:
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}


def turn(*calls: dict, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))


class ScriptedChatModel(BaseChatModel):
    """Returns the next scripted AIMessage on every call; records the tool names it was bound with."""

    model_name: str = "scripted-fake"
    _script: list[AIMessage] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _bound: list[list[str]] = PrivateAttr(default_factory=list)
    _seen: list[list[Any]] = PrivateAttr(default_factory=list)
    _descriptions: dict[str, str] = PrivateAttr(default_factory=dict)

    def __init__(self, script: list[AIMessage], **kw: Any):
        super().__init__(**kw)
        self._script = list(script)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    @property
    def remaining(self) -> int:
        return len(self._script)

    @property
    def seen(self) -> list[list[Any]]:
        return self._seen

    def bind_tools(self, tools: Any, **kw: Any) -> "ScriptedChatModel":
        names = []
        for t in tools:
            if isinstance(t, dict):
                names.append(t.get("name") or t.get("function", {}).get("name"))
                self._descriptions[names[-1]] = t.get("description") or t.get("function", {}).get("description", "")
            else:
                names.append(getattr(t, "name", str(t)))
                self._descriptions[names[-1]] = getattr(t, "description", "")
        self._bound.append(sorted(names))
        return self

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kw: Any) -> ChatResult:
        with self._lock:
            self._seen.append(list(messages))
            if not self._script:
                raise AssertionError("scripted model ran out of turns")
            msg = self._script.pop(0)
        return ChatResult(generations=[ChatGeneration(message=msg)])
