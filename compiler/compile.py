"""compile(spec, version) -> workspace/integrations/<name>/vN/

Deterministic: the same spec, examples, transforms and OpenAPI snapshot always produce
byte-identical code. Only the spec and transforms.py are ever written by AI; everything
here is regenerated from them.
"""

from __future__ import annotations

import ast
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from runner.contract import openapi_hash, validate_body
from spec import spec_hash, spec_to_dict
from spec.models import RECORD_PREFIX, Spec, ValueSource

COMPILER_VERSION = "0.1.0"
TEMPLATES = Path(__file__).parent / "templates"
QUARANTINE_EXPECTS = {"PARSE_NO_MATCH", "VALIDATION_FAILED", "NOT_FOUND", "SINK_CONTRACT", "AUTH",
                      "VERIFY_MISMATCH", "UNKNOWN"}


class CompileError(ValueError):
    pass


@dataclass
class Example:
    """A confirmed sample: the record, and either the expected sink body or a quarantine code."""

    name: str
    record: dict[str, Any]
    expect: dict[str, Any]
    missing: list[str]

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "Example":
        expect = d.get("expect") or {}
        if ("body" in expect) == ("quarantine" in expect):
            raise CompileError(f"example {d.get('name')!r}: expect needs exactly one of body/quarantine")
        if "quarantine" in expect and expect["quarantine"] not in QUARANTINE_EXPECTS:
            raise CompileError(f"example {d.get('name')!r}: unknown quarantine code {expect['quarantine']!r}")
        return cls(name=str(d["name"]), record=dict(d["record"]), expect=dict(expect),
                   missing=list(d.get("missing", [])))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "record": self.record, "expect": self.expect, "missing": self.missing}


def load_examples(path: str | Path | None) -> list[Example]:
    if path is None or not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text()) or []
    return [Example.parse(d) for d in data]


# ---- code generation helpers -------------------------------------------------------

def _ref_expr(ref: str) -> str:
    if ref.startswith(RECORD_PREFIX):
        return f"record[{ref.removeprefix(RECORD_PREFIX)!r}]"
    return f"fields[{ref!r}]"


def _kwargs(args: dict[str, Any]) -> str:
    return "".join(f", {k}={v!r}" for k, v in sorted(args.items()))


def value_expr(v: ValueSource) -> str:
    if v.template is not None:
        return f"render({v.template!r}, fields, record)"
    if v.transform is not None:
        src = _ref_expr(v.from_)  # type: ignore[arg-type]
        if v.transform.startswith("custom:"):
            return f"transforms.{v.transform.removeprefix('custom:')}({src}{_kwargs(v.args)})"
        return f"T.{v.transform}({src}{_kwargs(v.args)})"
    if v.from_ is not None:
        return _ref_expr(v.from_)
    return repr(v.const)


def rule_line(rule: Any) -> str:
    f = rule.field
    if rule.rule == "required":
        return f"check_required(fields, {f!r})"
    if rule.rule == "positive_int":
        return f"check_positive_int(fields, {f!r})"
    if rule.rule == "regex":
        return f"check_regex(fields, {f!r}, {rule.pattern!r})"
    if rule.rule == "one_of":
        return f"check_one_of(fields, {f!r}, {rule.values!r})"
    if rule.rule == "exists":
        return (f"check_exists(http, 'sink', {rule.check.path!r}, {dict(rule.check.params)!r}, "
                f"fields, record, where={f!r})")
    raise CompileError(f"unsupported rule {rule.rule}")


def _defined_functions(source: str) -> set[str]:
    tree = ast.parse(source)
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    env.filters["py"] = repr
    env.filters["json"] = lambda v: json.dumps(v, indent=2, sort_keys=True)
    return env


# ---- compile -------------------------------------------------------------------------

@dataclass
class CompileResult:
    name: str
    version: int
    path: Path
    spec_hash: str
    openapi_hash: str | None
    contract_errors: list[str]


def compile_spec(spec: Spec, *, version: int, out_root: Path, examples: list[Example] | None = None,
                 transforms_src: str | None = None, sink_openapi: dict | None = None) -> CompileResult:
    examples = examples or []
    transforms_src = transforms_src or ""
    needed = spec.custom_transforms()
    missing_fns = needed - _defined_functions(transforms_src) if transforms_src else needed
    if missing_fns:
        raise CompileError(f"spec uses custom transforms not defined in transforms.py: {sorted(missing_fns)}")

    out = out_root / spec.name / f"v{version}"
    if out.exists():
        raise CompileError(f"{out} already exists; versions are immutable, compile v{version + 1} instead")

    contract_errors: list[str] = []
    if sink_openapi is not None:
        for ex in examples:
            if "body" in ex.expect:
                contract_errors += [f"[{ex.name}] {e}" for e in validate_body(
                    sink_openapi, spec.sink.call.method, spec.sink.call.path, ex.expect["body"])]

    env = _env()
    ctx = {
        "spec": spec,
        "version": version,
        "spec_hash": spec_hash(spec),
        "compiler_version": COMPILER_VERSION,
        "body_exprs": {k: value_expr(v) for k, v in spec.sink.body.items()},
        "rule_lines": [rule_line(r) for r in spec.validate_],
        "negative_cases": _negative_cases(spec),
        "uses_transforms": bool(needed),
    }

    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "tests").mkdir(parents=True)
    (tmp / "contract").mkdir()
    files = {
        "__init__.py": env.get_template("package_init.py.j2").render(**ctx),
        "integration.py": env.get_template("integration.py.j2").render(**ctx),
        "parsers.py": env.get_template("parsers.py.j2").render(**ctx),
        "adapters.py": env.get_template("adapters.py.j2").render(**ctx),
        "transforms.py": transforms_src or env.get_template("transforms.py.j2").render(**ctx),
        "tests/__init__.py": "",
        "tests/conftest.py": env.get_template("tests/conftest.py.j2").render(**ctx),
        "tests/test_golden.py": env.get_template("tests/test_golden.py.j2").render(**ctx),
        "tests/test_negative.py": env.get_template("tests/test_negative.py.j2").render(**ctx),
        "tests/test_dedupe.py": env.get_template("tests/test_dedupe.py.j2").render(**ctx),
        "tests/test_contract.py": env.get_template("tests/test_contract.py.j2").render(**ctx),
        "tests/test_no_ai.py": env.get_template("tests/test_no_ai.py.j2").render(**ctx),
    }
    for rel, text in files.items():
        if rel.endswith(".py") and text:
            try:
                ast.parse(text, filename=rel)
            except SyntaxError as e:  # a compiler bug, never an agent's fault
                shutil.rmtree(tmp)
                raise CompileError(f"internal: generated {rel} is not valid Python: {e}") from e
        (tmp / rel).write_text(text)

    (tmp / "spec.yaml").write_text(yaml.safe_dump(spec_to_dict(spec), sort_keys=False))
    (tmp / "golden.json").write_text(json.dumps([e.to_dict() for e in examples], indent=2))
    config = {
        "source": {"base_url": spec.source.base_url},
        "sink": {"base_url": spec.sink.base_url, "staging_base_url": spec.sink.staging_base_url},
        "credentials": {},  # env var names only, never values
    }
    (tmp / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    oa_hash = None
    if sink_openapi is not None:
        (tmp / "contract" / "openapi.json").write_text(json.dumps(sink_openapi, indent=2, sort_keys=True))
        oa_hash = openapi_hash(sink_openapi)
    manifest = {
        "name": spec.name, "version": version, "spec_hash": ctx["spec_hash"], "openapi_hash": oa_hash,
        "sink_call": {"method": spec.sink.call.method, "path": spec.sink.call.path},
        "compiler_version": COMPILER_VERSION,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "examples": len(examples),
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
    tmp.rename(out)
    return CompileResult(spec.name, version, out, ctx["spec_hash"], oa_hash, contract_errors)


def _negative_cases(spec: Spec) -> list[dict[str, Any]]:
    """One mutation per validation rule, each expected to quarantine with the right code."""
    cases = []
    for r in spec.validate_:
        if r.rule == "required":
            cases.append({"id": f"{r.field}_missing", "field": r.field, "op": "delete", "code": "VALIDATION_FAILED"})
        elif r.rule == "positive_int":
            cases.append({"id": f"{r.field}_zero", "field": r.field, "op": "set", "value": 0, "code": "VALIDATION_FAILED"})
            cases.append({"id": f"{r.field}_negative", "field": r.field, "op": "set", "value": -5, "code": "VALIDATION_FAILED"})
        elif r.rule == "regex":
            cases.append({"id": f"{r.field}_bad_format", "field": r.field, "op": "set", "value": "\u0000",
                          "code": "VALIDATION_FAILED"})
        elif r.rule == "one_of":
            cases.append({"id": f"{r.field}_not_allowed", "field": r.field, "op": "set",
                          "value": "__patchwork_not_allowed__", "code": "VALIDATION_FAILED"})
        elif r.rule == "exists":
            cases.append({"id": f"{r.field}_unknown", "field": r.field, "op": "set",
                          "value": "PATCHWORK-UNKNOWN", "missing": "PATCHWORK-UNKNOWN", "code": "NOT_FOUND"})
    return cases
