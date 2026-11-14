"""Integration Spec v1: the IR every component builds against (plan section 4).

The vocabulary is closed. Anything the compiler can't express is rejected here with
an error message an agent can read and fix, instead of turning into a broken program.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
RECORD_PREFIX = "record."

QuarantineCode = Literal[
    "PARSE_NO_MATCH",
    "VALIDATION_FAILED",
    "NOT_FOUND",
    "SINK_CONTRACT",
    "AUTH",
    "VERIFY_MISMATCH",
    "UNKNOWN",
]
QUARANTINE_CODES: tuple[str, ...] = QuarantineCode.__args__  # type: ignore[attr-defined]

BUILTIN_TRANSFORMS = {"int", "float", "strip", "upper", "lower", "date", "lookup", "multiply"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_ident(value: str, what: str) -> str:
    if not IDENT.match(value):
        raise ValueError(f"{what} {value!r} must match {IDENT.pattern}")
    return value


class HttpCall(_Strict):
    method: Literal["GET", "POST", "PUT", "PATCH"]
    path: str
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"path {v!r} must start with '/'")
        return v


class Source(_Strict):
    kind: Literal["api", "ui"]
    base_url: str
    list: HttpCall
    record_id: str
    text_field: str
    cursor_field: str | None = None

    @model_validator(mode="after")
    def _api_only(self) -> "Source":
        if self.kind != "api":
            raise ValueError("source.kind 'ui' is not supported by compiler v1; use 'api'")
        return self


class FieldExtract(_Strict):
    """Exactly one extract method: regex | json_path | between."""

    regex: str | None = None
    json_path: str | None = None
    between: tuple[str, str] | None = None
    cast: Literal["int", "float", "str"] = "str"

    @model_validator(mode="after")
    def _one_method(self) -> "FieldExtract":
        chosen = [m for m in ("regex", "json_path", "between") if getattr(self, m) is not None]
        if len(chosen) != 1:
            raise ValueError(f"field extract needs exactly one of regex/json_path/between, got {chosen or 'none'}")
        if self.regex is not None:
            try:
                compiled = re.compile(self.regex)
            except re.error as e:
                raise ValueError(f"regex {self.regex!r} does not compile: {e}") from e
            if compiled.groups < 1:
                raise ValueError(f"regex {self.regex!r} needs one capture group for the value")
        if self.json_path is not None and not self.json_path.startswith("$"):
            raise ValueError(f"json_path {self.json_path!r} must start with '$'")
        return self


class Parser(_Strict):
    id: str
    match: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, FieldExtract]

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_ident(v, "parser id")

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: dict[str, FieldExtract]) -> dict[str, FieldExtract]:
        if not v:
            raise ValueError("parser needs at least one field")
        for name in v:
            _check_ident(name, "field name")
        return v


class Extract(_Strict):
    route_by: str
    parsers: list[Parser]

    @field_validator("parsers")
    @classmethod
    def _parsers(cls, v: list[Parser]) -> list[Parser]:
        if not v:
            raise ValueError("extract.parsers must not be empty")
        ids = [p.id for p in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate parser ids: {sorted(dupes)}")
        return v


class ValidationRule(_Strict):
    field: str
    rule: Literal["required", "positive_int", "regex", "one_of", "exists"]
    pattern: str | None = None
    values: list[Any] | None = None
    check: HttpCall | None = None

    @model_validator(mode="after")
    def _args(self) -> "ValidationRule":
        need = {"regex": "pattern", "one_of": "values", "exists": "check"}.get(self.rule)
        if need and getattr(self, need) is None:
            raise ValueError(f"rule {self.rule!r} on {self.field!r} needs '{need}'")
        if self.rule == "exists" and self.check and self.check.method != "GET":
            raise ValueError("exists.check must be a GET")
        return self


class Dedupe(_Strict):
    key: list[str]


class ValueSource(_Strict):
    """One of: {from} | {const} | {transform, from, args} | {template}."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    from_: str | None = Field(default=None, alias="from")
    const: Any = None
    transform: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    template: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ValueSource":
        if self.template is not None:
            if self.from_ is not None or self.transform is not None or self.const is not None:
                raise ValueError("template value cannot be combined with from/transform/const")
        elif self.transform is not None:
            if self.from_ is None:
                raise ValueError(f"transform {self.transform!r} needs 'from'")
            name = self.transform
            if name.startswith("custom:"):
                _check_ident(name.removeprefix("custom:"), "custom transform")
            elif name not in BUILTIN_TRANSFORMS:
                raise ValueError(
                    f"unknown transform {name!r}; use one of {sorted(BUILTIN_TRANSFORMS)} or custom:<fn>"
                )
        elif self.from_ is None and "const" not in self.model_fields_set:
            raise ValueError("value needs one of from/const/transform/template")
        elif self.from_ is not None and "const" in self.model_fields_set:
            raise ValueError("value cannot have both 'from' and 'const'")
        if self.args and self.transform is None:
            raise ValueError("'args' only applies to a transform")
        return self


class Verify(_Strict):
    method: Literal["GET"] = "GET"
    path: str
    params: dict[str, str] = Field(default_factory=dict)
    expect_count: int = 1


class Sink(_Strict):
    kind: Literal["api"]
    base_url: str
    staging_base_url: str | None = None
    call: HttpCall
    body: dict[str, ValueSource]
    verify: Verify | None = None

    @field_validator("call")
    @classmethod
    def _mutating(cls, v: HttpCall) -> HttpCall:
        if v.method == "GET":
            raise ValueError("sink.call must be a mutating request (POST/PUT/PATCH)")
        return v


class Assumption(_Strict):
    id: str
    text: str
    status: Literal["confirmed", "unconfirmed", "observed", "inferred"]


class Spec(_Strict):
    spec_version: Literal[1]
    name: str
    intent: str
    source: Source
    extract: Extract
    validate_: list[ValidationRule] = Field(default_factory=list, alias="validate")
    dedupe: Dedupe
    sink: Sink
    on_error: Literal["quarantine"] = "quarantine"
    assumptions: list[Assumption] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_ident(v, "spec name")

    @model_validator(mode="after")
    def _references(self) -> "Spec":
        """Every field the pipeline uses must be extracted by every parser."""
        common = set.intersection(*(set(p.fields) for p in self.extract.parsers))
        errors: list[str] = []

        def check(ref: str, where: str) -> None:
            if ref.startswith(RECORD_PREFIX):
                return
            if ref not in common:
                errors.append(
                    f"{where} references field {ref!r}, which not every parser extracts "
                    f"(fields common to all parsers: {sorted(common)})"
                )

        for rule in self.validate_:
            if rule.field.startswith(RECORD_PREFIX):
                errors.append(f"validate[{rule.field}]: rules apply to extracted fields, not record.* fields")
            else:
                check(rule.field, f"validate[{rule.field}:{rule.rule}]")
        for key in self.dedupe.key:
            check(key, "dedupe.key")
        for name, value in self.sink.body.items():
            if value.from_ is not None:
                check(value.from_, f"sink.body.{name}")
            for ref in template_refs(value.template or ""):
                check(ref, f"sink.body.{name}.template")
        for call_name, call in self._templated_calls():
            for ref in template_refs(call):
                check(ref, call_name)
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _templated_calls(self) -> list[tuple[str, str]]:
        out = []
        for rule in self.validate_:
            if rule.check:
                out.append((f"validate[{rule.field}].check.path", rule.check.path))
        if self.sink.verify:
            out.append(("sink.verify.path", self.sink.verify.path))
            for k, v in self.sink.verify.params.items():
                out.append((f"sink.verify.params.{k}", v))
        return out

    def custom_transforms(self) -> set[str]:
        return {
            v.transform.removeprefix("custom:")
            for v in self.sink.body.values()
            if v.transform and v.transform.startswith("custom:")
        }


_TEMPLATE_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def template_refs(template: str) -> list[str]:
    return _TEMPLATE_REF.findall(template)
