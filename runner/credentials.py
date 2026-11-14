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


def redact_obj(obj: Any, names: list[str] | None = None) -> Any:
    """`redact` applied to every string (keys and values) inside a JSON-like structure; other values kept.
    Returns `obj` itself when it holds no credential value (the common case: no copy of large records)."""
    named = [os.environ.get(n, "") for n in (names or []) if NAME.match(n or "")]
    vals = sorted({v for v in (*named, *_known_values()) if v and len(v) >= 6}, key=len, reverse=True)
    if not vals:
        return obj

    def has(o: Any) -> bool:
        if isinstance(o, str):
            return any(v in o for v in vals)
        if isinstance(o, dict):
            return any(has(k) or has(v) for k, v in o.items())
        if isinstance(o, (list, tuple)):
            return any(has(v) for v in o)
        return False

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            for v in vals:
                o = o.replace(v, "***")
            return o
        if isinstance(o, dict):
            return {walk(k): walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o

    return walk(obj) if has(obj) else obj


def auth_blocks(spec: Any) -> list[dict[str, Any]]:
    """Every `auth` block a spec carries (`source.auth`, `sink.auth`, and any other `auth:` mapping with a
    `credential`, so new sink kinds such as `airtable` are covered without a change here). `spec` is a spec
    model, a spec dict (spec.yaml) or a compiled config dict (config.yaml): they share the shape."""
    if spec is None:
        return []
    if hasattr(spec, "model_dump"):
        spec = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    out: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "auth" and isinstance(v, dict) and v.get("credential"):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(spec)
    return out


def credential_names(spec: Any) -> list[str]:
    """The credential names a spec (model, spec dict or config dict) references: sorted, unique, unchecked
    (callers that launch a runner pass each through `check_name`)."""
    return sorted({str(b["credential"]) for b in auth_blocks(spec)})
