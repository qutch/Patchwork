"""Agent layer. The control plane gets its AgentService from `get_agent_service()`.

PATCHWORK_AGENT selects the implementation:
  scripted (default until the deep agent lands) | deep
"""

from __future__ import annotations

import os
from functools import lru_cache

from agents.interface import AgentService


@lru_cache(maxsize=1)
def get_agent_service() -> AgentService:
    kind = os.environ.get("PATCHWORK_AGENT", "scripted")
    if kind == "deep":
        from agents.deep import DeepAgentService  # provided by the agents workstream
        return DeepAgentService()
    from agents.scripted import ScriptedAgentService
    return ScriptedAgentService()
