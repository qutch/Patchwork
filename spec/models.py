"""Integration Spec v1: the IR every component builds against (plan section 4).

The vocabulary is closed. Anything the compiler can't express is rejected here with
an error message an agent can read and fix, instead of turning into a broken program.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

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


class Auth(_Strict):
    """contracts V13: how a sink (or API source) authenticates. Names a credential; never holds a value."""

    scheme: Literal["bearer", "header"] = "bearer"
    credential: str
    header: str | None = None  # scheme: header, e.g. X-Api-Key

    @field_validator("credential")
    @classmethod
    def _credential(cls, v: str) -> str:
        from runner.credentials import check_name
        return check_name(v)

    @model_validator(mode="after")
    def _header(self) -> "Auth":
        if self.scheme == "header" and not (self.header and re.fullmatch(r"[A-Za-z0-9-]{1,64}", self.header)):
            raise ValueError("auth.scheme 'header' needs auth.header (an HTTP header name, e.g. X-Api-Key)")
        return self


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
    html_field: str | None = None  # v2: the record field holding page HTML (css/jsonld/fingerprint read it)
    cursor_field: str | None = None
    auth: Auth | None = None  # contracts V13: sent on every source request (names a credential, never a value)

    @model_validator(mode="after")
    def _api_only(self) -> "Source":
        if self.kind != "api":
            raise ValueError("source.kind 'ui' is not supported by compiler v1; use 'api'")
        return self


class FieldExtract(_Strict):
    """Exactly one primary extract method: regex | json_path | between | css | jsonld.

    `combine: product` (primary regex only) casts every capture group and multiplies them, e.g.
    '(\\d+) cases \\((\\d+)/case\\)' -> 3 cases x 12 = 36 units.

    v2 (contracts V3): `css` (+ optional `attr`) and `jsonld` (+ optional `jsonld_type`, default
    JobPosting) read the record's HTML (`source.html_field`). A `regex` next to `css`/`jsonld` is a
    refine: applied to the normalised result, group 1 is the value."""

    regex: str | None = None
    json_path: str | None = None
    between: tuple[str, str] | None = None
    css: str | None = None
    attr: str | None = None
    jsonld: str | None = None
    jsonld_type: str | None = None
    cast: Literal["int", "float", "str"] = "str"
    combine: Literal["product"] | None = None

    @property
    def reads_html(self) -> bool:
        return self.css is not None or self.jsonld is not None

    @property
    def refine(self) -> str | None:
        """The refine regex (a regex next to css/jsonld), else None."""
        return self.regex if self.reads_html else None

    @model_validator(mode="after")
    def _one_method(self) -> "FieldExtract":
        if not self.reads_html:
            chosen = [m for m in ("regex", "json_path", "between") if getattr(self, m) is not None]
            if len(chosen) != 1:
                raise ValueError(f"field extract needs exactly one of regex/json_path/between, got {chosen or 'none'}")
        else:
            chosen = [m for m in ("json_path", "between", "css", "jsonld") if getattr(self, m) is not None]
            if len(chosen) != 1:
                raise ValueError("field extract needs exactly one of regex/json_path/between/css/jsonld "
                                 f"(regex may refine css or jsonld), got {chosen}")
        if self.attr is not None and self.css is None:
            raise ValueError("attr only applies to css fields")
        if self.jsonld_type is not None and self.jsonld is None:
            raise ValueError("jsonld_type only applies to jsonld fields")
        if self.css is not None and not self.css.strip():
            raise ValueError("css selector must not be empty")
        if self.jsonld is not None and not self.jsonld.strip():
            raise ValueError("jsonld path must not be empty")
        if self.regex is not None:
            try:
                compiled = re.compile(self.regex)
            except re.error as e:
                raise ValueError(f"regex {self.regex!r} does not compile: {e}") from e
            if compiled.groups < 1:
                what = "refine regex" if self.reads_html else "regex"
                raise ValueError(f"{what} {self.regex!r} needs one capture group for the value")
        if self.combine is not None:
            if self.regex is None or self.reads_html:
                raise ValueError("combine only applies to regex fields")
            if re.compile(self.regex).groups < 2:
                raise ValueError(f"combine: {self.combine} needs a regex with at least two capture groups")
            if self.cast not in ("int", "float"):
                raise ValueError(f"combine: {self.combine} needs cast: int or float")
        if self.json_path is not None and not self.json_path.startswith("$"):
            raise ValueError(f"json_path {self.json_path!r} must start with '$'")
        return self


class Origin(_Strict):
    """Layout provenance (contracts V4). Metadata only: copied into manifest.json, never used to route."""

    kind: Literal["taught", "learned", "fixture"]
    at: str | None = None
    thread_id: str | None = None
    approved_by: Literal["auto", "human"] | None = None


class Parser(_Strict):
    id: str
    match: dict[str, str] = Field(default_factory=dict)
    fingerprint: str | None = None  # v2: CSS selector identifying the platform in the record's HTML
    origin: Origin | None = None
    fields: dict[str, FieldExtract]

    @field_validator("match")
    @classmethod
    def _match(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if "*" in val and not re.fullmatch(r"\*(\.[A-Za-z0-9-]+){2,}", val):
                raise ValueError(f"match {k}: a wildcard must be '*.<domain>' (e.g. '*.myworkdayjobs.com'), got {val!r}")
        return v

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("fingerprint must be a non-empty CSS selector")
        return v

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
    rule: Literal["required", "positive_int", "regex", "one_of", "exists", "number", "date", "url"]
    pattern: str | None = None
    values: list[Any] | None = None
    check: HttpCall | None = None
    # `number` only (contracts V15): inclusive bounds on the value cast to float
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _args(self) -> "ValidationRule":
        need = {"regex": "pattern", "one_of": "values", "exists": "check"}.get(self.rule)
        if need and getattr(self, need) is None:
            raise ValueError(f"rule {self.rule!r} on {self.field!r} needs '{need}'")
        if self.rule != "number" and (self.min is not None or self.max is not None):
            raise ValueError(f"min/max only apply to rule 'number' (on {self.field!r})")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"rule 'number' on {self.field!r}: min {self.min} is greater than max {self.max}")
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


class ApiSink(_Strict):
    kind: Literal["api"]
    base_url: str
    staging_base_url: str | None = None
    call: HttpCall
    body: dict[str, ValueSource]
    verify: Verify | None = None
    auth: Auth | None = None  # contracts V13: sent on every sink request (names a credential, never a value)

    @field_validator("call")
    @classmethod
    def _mutating(cls, v: HttpCall) -> HttpCall:
        if v.method == "GET":
            raise ValueError("sink.call must be a mutating request (POST/PUT/PATCH)")
        return v


class FormVerify(_Strict):
    confirmation_text: str


class FormSink(_Strict):
    """v2 (contracts V5): a web form filled by label in headless Chromium. Keys of `fields` are the
    form's question labels; values use the same vocabulary and reference checks as `body`."""

    kind: Literal["form"]
    url: str
    fields: dict[str, ValueSource]
    submit: str = "Submit"
    verify: FormVerify | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"form url {v!r} must be http(s)")
        return v

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: dict[str, ValueSource]) -> dict[str, ValueSource]:
        if not v:
            raise ValueError("form sink needs at least one field")
        if any(not label.strip() for label in v):
            raise ValueError("form field labels must not be empty")
        return v


class AirtableVerify(_Strict):
    read_back: bool = True


AIRTABLE_BASE_ID = re.compile(r"^app[A-Za-z0-9]{14}$")


class AirtableSink(_Strict):
    """v3 (contracts V14): records created in an Airtable table through its REST API. Keys of `fields`
    are Airtable field NAMES (label-keyed, like a form's); values use the same vocabulary and reference
    checks as `body`. `auth` names the credential (V13); a value never appears in a spec."""

    kind: Literal["airtable"]
    base_id: str
    table: str
    auth: Auth
    fields: dict[str, ValueSource]
    typecast: bool = False
    verify: AirtableVerify | None = None

    @field_validator("base_id")
    @classmethod
    def _base_id(cls, v: str) -> str:
        if not AIRTABLE_BASE_ID.match(v):
            raise ValueError(f"airtable base_id {v!r} must look like app + 14 letters/digits "
                             "(from the base URL airtable.com/appXXXXXXXXXXXXXX/...)")
        return v

    @field_validator("table")
    @classmethod
    def _table(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("airtable table must be a table name or id (tbl...)")
        return v

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: dict[str, ValueSource]) -> dict[str, ValueSource]:
        if not v:
            raise ValueError("airtable sink needs at least one field")
        if any(not name.strip() for name in v):
            raise ValueError("airtable field names must not be empty")
        return v


LABEL_KEYED_SINKS = ("form", "airtable")  # sinks whose `fields` are keyed by the destination's own labels

Sink = Annotated[ApiSink | FormSink | AirtableSink, Field(discriminator="kind")]


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
        where = self.sink_values_key()
        for name, value in self.sink_values().items():
            if value.from_ is not None:
                check(value.from_, f"sink.{where}.{name}")
            for ref in template_refs(value.template or ""):
                check(ref, f"sink.{where}.{name}.template")
        errors += self._html_field_errors()
        for call_name, call in self._templated_calls():
            for ref in template_refs(call):
                check(ref, call_name)
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _html_field_errors(self) -> list[str]:
        if self.source.html_field is not None:
            return []
        users = []
        for p in self.extract.parsers:
            if p.fingerprint is not None:
                users.append(f"extract.parsers[{p.id}].fingerprint")
            for fname, f in p.fields.items():
                if f.reads_html:
                    users.append(f"extract.parsers[{p.id}].fields.{fname} ({'css' if f.css is not None else 'jsonld'})")
        if not users:
            return []
        return [f"source.html_field (the record field holding the page HTML) is required by {', '.join(users)}"]

    def sink_values_key(self) -> str:
        return "body" if isinstance(self.sink, ApiSink) else "fields"

    def sink_values(self) -> dict[str, ValueSource]:
        """The sink's value mapping: `body` for API sinks, `fields` for form and Airtable sinks."""
        return self.sink.body if isinstance(self.sink, ApiSink) else self.sink.fields

    def _templated_calls(self) -> list[tuple[str, str]]:
        out = []
        for rule in self.validate_:
            if rule.check:
                out.append((f"validate[{rule.field}].check.path", rule.check.path))
        if isinstance(self.sink, ApiSink) and self.sink.verify:
            out.append(("sink.verify.path", self.sink.verify.path))
            for k, v in self.sink.verify.params.items():
                out.append((f"sink.verify.params.{k}", v))
        return out

    def custom_transforms(self) -> set[str]:
        return {
            v.transform.removeprefix("custom:")
            for v in self.sink_values().values()
            if v.transform and v.transform.startswith("custom:")
        }


_TEMPLATE_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def template_refs(template: str) -> list[str]:
    return _TEMPLATE_REF.findall(template)
