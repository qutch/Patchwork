"""Destination and source credentials (contracts V13). AI-free, like the rest of the runner.

A spec names a credential (`auth: {scheme: bearer, credential: AIRTABLE_TOKEN}`); it never holds a value.
Values live in the gitignored `.env`, are loaded into the control plane's environment, and reach a runner
subprocess only as `PATCHWORK_CRED__<NAME>` for the names its spec references (control/ops.runner_env). The
agents, the logs, the UI and the model never see a value: anything that may show text derived from a request
passes it through `redact()`.

Model keys can never be credentials (`MODEL_KEY`), so naming one in a spec cannot smuggle it into the runner.
"""

from __future__ import annotations

import os
import re
from typing import Any

CRED_PREFIX = "PATCHWORK_CRED__"
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MODEL_KEY = re.compile(r"(ANTHROPIC|OPENROUTER|OPENAI|LANGCHAIN|LANGSMITH|GOOGLE_API|GEMINI|PATCHWORK_MODEL)", re.I)


class CredentialError(RuntimeError):
    """A credential is missing or not allowed (the message names it, never shows a value)."""


def check_name(name: str) -> str:
    """The credential name, or ValueError if it is malformed or names a model key."""
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(f"credential name {name!r} must be UPPER_SNAKE_CASE (e.g. AIRTABLE_TOKEN)")
    if MODEL_KEY.search(name) or name.startswith(CRED_PREFIX):
        raise ValueError(f"credential {name!r} is not allowed: model keys never reach the runner")
    return name


def get(name: str) -> str:
    """The value of credential `name`. In a runner: from PATCHWORK_CRED__<name> (set by the control plane);
    in the control plane (contract fetches for the agents' tools): from the environment (.env)."""
    check_name(name)
    value = os.environ.get(CRED_PREFIX + name) or os.environ.get(name)
    if not value:
        raise CredentialError(f"credential {name} is not set: add {name}=... to .env and restart the control plane")
    return value


def is_set(name: str) -> bool:
    try:
        return bool(get(name))
    except (ValueError, CredentialError):
        return False


def headers(auth: Any) -> dict[str, str]:
    """HTTP headers for a spec `auth` block (a spec model or a plain dict); {} when there is none.
    bearer -> Authorization: Bearer <value>; header -> <auth.header>: <value>."""
    if not auth:
        return {}
    get_ = (lambda k: auth.get(k)) if isinstance(auth, dict) else (lambda k: getattr(auth, k, None))
    value = get(get_("credential"))
    if get_("scheme") == "header":
        return {get_("header"): value}
    return {"Authorization": f"Bearer {value}"}


def _known_values() -> list[str]:
    vals = [v for k, v in os.environ.items() if k.startswith(CRED_PREFIX) and v]
    return sorted({v for v in vals if len(v) >= 6}, key=len, reverse=True)


def redact(text: Any, extra: list[str] | None = None, names: list[str] | None = None) -> str:
    """`text` with every credential value this process holds as PATCHWORK_CRED__*, the values of the named
    credentials (`names`, e.g. every credential a spec references) and `extra`, replaced by ***."""
    s = str(text)
    named = [os.environ.get(n, "") for n in (names or []) if NAME.match(n or "")]
    for v in sorted({*(extra or []), *named, *_known_values()}, key=len, reverse=True):
        if v and len(v) >= 6:
            s = s.replace(v, "***")
    return s
