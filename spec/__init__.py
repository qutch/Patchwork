"""Spec loading and validation. `validate_spec` is the function the agents' tool wraps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from spec.models import QUARANTINE_CODES, Spec

__all__ = ["Spec", "SpecError", "load_spec", "parse_spec", "validate_spec", "spec_hash", "QUARANTINE_CODES"]


class SpecError(ValueError):
    """Raised with agent-readable messages, one per problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid spec:\n" + "\n".join(f"  - {e}" for e in errors))


def _format(err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        msg = e["msg"].removeprefix("Value error, ")
        out.append(f"{loc}: {msg}")
    return out


def parse_spec(data: dict[str, Any]) -> Spec:
    try:
        return Spec.model_validate(data)
    except ValidationError as e:
        raise SpecError(_format(e)) from None


def load_spec(path: str | Path) -> Spec:
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise SpecError(["<root>: spec must be a YAML mapping"])
    return parse_spec(data)


def validate_spec(text: str) -> list[str]:
    """Return a list of errors (empty means valid). Never raises on bad input."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"<yaml>: {e}"]
    if not isinstance(data, dict):
        return ["<root>: spec must be a YAML mapping"]
    try:
        parse_spec(data)
    except SpecError as e:
        return e.errors
    return []


def spec_to_dict(spec: Spec) -> dict[str, Any]:
    return spec.model_dump(mode="json", by_alias=True, exclude_none=True)


def spec_hash(spec: Spec) -> str:
    canon = json.dumps(spec_to_dict(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()
