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


def _check_clean(v: str | dict | None) -> str | dict | None:
    """contracts V18: `clean` is a closed set of cleaners (runner.clean.CLEANERS), by name or {name: args}."""
    from runner.clean import CLEANERS

    if v is None:
        return v
    examples = ("money | number | strip | upper | lower | {money: {decimal: ','}} | {date: ['%d %b %Y']} | "
                "{split: {sep: ',', part: 1}}")
    if isinstance(v, str):
        if v not in CLEANERS:
            raise ValueError(f"unknown cleaner {v!r}; use one of {examples}")
        if v in ("date", "split"):
            raise ValueError(f"cleaner {v!r} needs arguments, e.g. " +
                             ("{date: ['%m/%d/%Y']}" if v == "date" else "{split: {sep: ',', part: 1}}"))
        return v
    if not isinstance(v, dict) or len(v) != 1:
        raise ValueError(f"clean must be one cleaner: {examples}")
    name, args = next(iter(v.items()))
    if name not in CLEANERS:
        raise ValueError(f"unknown cleaner {name!r}; use one of {examples}")
    if name in ("money", "number"):
        if not isinstance(args, dict) or set(args) - {"decimal"} or args.get("decimal", ".") not in (".", ","):
            raise ValueError(f"clean {name} takes {{decimal: '.' | ','}}, got {args!r}")
    elif name == "date":
        if not isinstance(args, list) or not args or not all(isinstance(f, str) and "%" in f for f in args):
            raise ValueError(f"clean date needs a non-empty list of strptime formats (e.g. ['%m/%d/%Y']), got {args!r}")
    elif name == "split":
        if (not isinstance(args, dict) or set(args) - {"sep", "part"} or not isinstance(args.get("sep"), str)
                or not args.get("sep") or not isinstance(args.get("part"), int) or isinstance(args.get("part"), bool)):
            raise ValueError(f"clean split needs {{sep: <non-empty text>, part: <index from 0>}}, got {args!r}")
    else:
        raise ValueError(f"cleaner {name!r} takes no arguments; write clean: {name}")
    return v


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
    refine: applied to the normalised result, group 1 is the value.

    V18: a `regex` next to `json_path` is a refine too. `clean` (runner.clean.CLEANERS) runs on the raw value
    before `cast`."""

    regex: str | None = None
    json_path: str | None = None
    between: tuple[str, str] | None = None
    css: str | None = None
    attr: str | None = None
    jsonld: str | None = None
    jsonld_type: str | None = None
    cast: Literal["int", "float", "str"] = "str"
    combine: Literal["product"] | None = None
    clean: str | dict | None = None  # contracts V18

    @property
    def reads_html(self) -> bool:
        return self.css is not None or self.jsonld is not None

    @property
    def refine(self) -> str | None:
        """The refine regex (a regex next to css/jsonld/json_path), else None."""
        return self.regex if (self.reads_html or self.json_path is not None) else None

    @field_validator("clean")
    @classmethod
    def _clean(cls, v: str | dict | None) -> str | dict | None:
        return _check_clean(v)

    @model_validator(mode="after")
    def _one_method(self) -> "FieldExtract":
        if not self.reads_html and self.json_path is not None and self.regex is not None:
            if self.between is not None:
                raise ValueError("field extract needs exactly one of regex/json_path/between "
                                 "(regex may refine json_path), got ['json_path', 'between']")
        elif not self.reads_html:
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
                what = "refine regex" if self.refine is not None else "regex"
                raise ValueError(f"{what} {self.regex!r} needs one capture group for the value")
        if self.combine is not None:
            if self.regex is None or self.refine is not None:
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


def _check_regex(pattern: str, what: str, groups: int = 0) -> str:
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"{what} {pattern!r} does not compile: {e}") from e
    if compiled.groups < groups:
        raise ValueError(f"{what} {pattern!r} needs one capture group for the value")
    return pattern


class RowExtract(_Strict):
    """contracts V18: one row field, read relative to its row. Exactly one of `column` (a table header or a
    named group of `select.lines.match`, case-insensitive), `index` (a cell or group position, from 0), `css`
    (inside the row element, optional `attr`) or `regex` (on the row's text, group 1). A `regex` next to
    `column`/`index`/`css` is a refine. `clean` runs before `cast`. An empty value is None."""

    column: str | None = None
    index: int | None = None
    css: str | None = None
    regex: str | None = None
    attr: str | None = None
    cast: Literal["int", "float", "str"] = "str"
    clean: str | dict | None = None

    @property
    def refine(self) -> str | None:
        """The refine regex (a regex next to column/index/css), else None."""
        primary = self.column is not None or self.index is not None or self.css is not None
        return self.regex if primary else None

    @field_validator("clean")
    @classmethod
    def _clean(cls, v: str | dict | None) -> str | dict | None:
        return _check_clean(v)

    @model_validator(mode="after")
    def _one_method(self) -> "RowExtract":
        chosen = [m for m in ("column", "index", "css") if getattr(self, m) is not None]
        if len(chosen) > 1 or (not chosen and self.regex is None):
            raise ValueError("row field needs exactly one of column/index/css/regex "
                             f"(regex may refine column, index or css), got {chosen or 'none'}")
        if self.column is not None and not self.column.strip():
            raise ValueError("column must name a column")
        if self.css is not None and not self.css.strip():
            raise ValueError("css selector must not be empty")
        if self.index is not None and self.index < 0:
            raise ValueError(f"index {self.index} must be 0 or more")
        if self.attr is not None and self.css is None:
            raise ValueError("attr only applies to css row fields")
        if self.regex is not None:
            _check_regex(self.regex, "refine regex" if self.refine else "regex", groups=1)
        return self


class TableSelect(_Strict):
    columns: list[str]

    @field_validator("columns")
    @classmethod
    def _columns(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("select.table.columns must not be empty (the header names that find the table)")
        if any(not c.strip() for c in v):
            raise ValueError("select.table.columns must not contain an empty name")
        return v


class LineSelect(_Strict):
    """Text lines between `after` and `before` (both exclusive). Each line `match`es (a row; its named groups
    are the row's cells) or is explained: blank, a repeat of a line from before the first row, or `skip`."""

    after: str | None = None
    before: str | None = None
    match: str
    skip: str | None = None

    @model_validator(mode="after")
    def _regexes(self) -> "LineSelect":
        for name in ("after", "before", "match", "skip"):
            value = getattr(self, name)
            if value is not None:
                _check_regex(value, f"select.lines.{name}")
        return self


class RowSelect(_Strict):
    table: TableSelect | None = None
    css: str | None = None
    lines: LineSelect | None = None

    @property
    def kind(self) -> str:
        return "table" if self.table is not None else "css" if self.css is not None else "lines"

    @model_validator(mode="after")
    def _one(self) -> "RowSelect":
        chosen = [m for m in ("table", "css", "lines") if getattr(self, m) is not None]
        if len(chosen) != 1:
            raise ValueError(f"rows.select needs exactly one of table/css/lines, got {chosen or 'none'}")
        if self.css is not None and not self.css.strip():
            raise ValueError("rows.select.css must not be empty")
        return self


class Rows(_Strict):
    """contracts V18: a parser with `rows` emits one record per row (document fields + row fields)."""

    select: RowSelect
    fields: dict[str, RowExtract]
    require: list[str] | None = None  # default: [the first row field]
    hold: Literal["document", "row"] = "document"

    @model_validator(mode="before")
    @classmethod
    def _default_require(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("require") is None and isinstance(data.get("fields"), dict) \
                and data["fields"]:
            data = {**data, "require": [next(iter(data["fields"]))]}
        return data

    @model_validator(mode="after")
    def _shape(self) -> "Rows":
        if not self.fields:
            raise ValueError("rows needs at least one field")
        for name in self.fields:
            _check_ident(name, "row field name")
        if not self.require:
            raise ValueError("rows.require must name at least one row field")
        for name in self.require:
            if name not in self.fields:
                raise ValueError(f"rows.require names {name!r}, which is not a row field ({sorted(self.fields)})")
        kind = self.select.kind
        groups = {g.lower() for g in re.compile(self.select.lines.match).groupindex} if self.select.lines else set()
        for name, f in self.fields.items():
            if f.css is not None and kind != "css":
                raise ValueError(f"rows.fields.{name}: css row fields need select.css (this select is {kind})")
            if (f.column is not None or f.index is not None) and kind == "css":
                raise ValueError(f"rows.fields.{name}: column/index need select.table or select.lines, "
                                 "not select.css (use css or regex)")
            if f.column is not None and kind == "lines" and f.column.strip().lower() not in groups:
                raise ValueError(f"rows.fields.{name}: column {f.column!r} is not a named group of "
                                 f"select.lines.match (groups: {sorted(groups)})")
        return self


class Parser(_Strict):
    id: str
    match: dict[str, str] = Field(default_factory=dict)
    fingerprint: str | None = None  # v2: CSS selector identifying the platform in the record's HTML
    origin: Origin | None = None
    fields: dict[str, FieldExtract]
    rows: Rows | None = None  # contracts V18: one record per row

    def all_fields(self) -> set[str]:
        """The document field names plus the row field names (what one emitted record carries)."""
        return set(self.fields) | (set(self.rows.fields) if self.rows else set())

    @model_validator(mode="after")
    def _no_collision(self) -> "Parser":
        if self.rows:
            both = sorted(set(self.fields) & set(self.rows.fields))
            if both:
                raise ValueError(f"{', '.join(repr(b) for b in both)} is both a document field and a row field; "
                                 "rename one")
        return self

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


CROSS_RULES = ("sum_equals", "product_equals", "count_equals")  # contracts V18: rules over a document's rows


class ValidationRule(_Strict):
    field: str
    rule: Literal["required", "positive_int", "regex", "one_of", "exists", "number", "date", "url",
                  "sum_equals", "product_equals", "count_equals"]
    pattern: str | None = None
    values: list[Any] | None = None
    check: HttpCall | None = None
    # `number` only (contracts V15): inclusive bounds on the value cast to float
    min: float | None = None
    max: float | None = None
    # cross-check rules only (contracts V18). `tolerance` is 0.01 when a cross-check rule omits it, and absent
    # (None) on every other rule, so specs without cross-checks dump and hash exactly as before.
    rows_field: str | None = None
    fields: list[str] | None = None
    tolerance: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _default_tolerance(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("rule") in CROSS_RULES and data.get("tolerance") is None:
            data = {**data, "tolerance": 0.01}
        return data

    @model_validator(mode="after")
    def _args(self) -> "ValidationRule":
        if self.rule in CROSS_RULES:
            if self.rule == "sum_equals" and not self.rows_field:
                raise ValueError(f"rule 'sum_equals' on {self.field!r} needs 'rows_field' (the row field to add up)")
            if self.rule == "product_equals" and (self.fields is None or len(self.fields) != 2):
                raise ValueError(f"rule 'product_equals' on {self.field!r} needs two fields to multiply "
                                 "(fields: [a, b])")
            if self.rule != "sum_equals" and self.rows_field is not None:
                raise ValueError(f"rows_field only applies to rule 'sum_equals' (on {self.field!r})")
            if self.rule != "product_equals" and self.fields is not None:
                raise ValueError(f"fields only applies to rule 'product_equals' (on {self.field!r})")
            if self.tolerance is not None and self.tolerance < 0:
                raise ValueError(f"tolerance on {self.field!r} must be 0 or more")
        elif self.rows_field is not None or self.fields is not None or self.tolerance is not None:
            raise ValueError(f"rows_field/fields/tolerance only apply to the rules {list(CROSS_RULES)} "
                             f"(on {self.field!r})")
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


class SqliteVerify(_Strict):
    read_back: bool = True


class SqliteSink(_Strict):
    """v4 (contracts V16): rows inserted into a table of a local SQLite file. Keys of `fields` are COLUMN
    NAMES (label-keyed, like a form's or an Airtable table's); values use the same vocabulary and reference
    checks as `body`. `path` is relative to the workspace and must stay inside it (runner/sqlite_sink.py
    enforces that again on the resolved path). A local file needs no credential, so there is no `auth`."""

    kind: Literal["sqlite"]
    path: str
    table: str
    fields: dict[str, ValueSource]
    verify: SqliteVerify | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        from pathlib import PurePosixPath, PureWindowsPath

        if not v.strip():
            raise ValueError("sqlite path must name a database file, relative to the workspace")
        if PurePosixPath(v).is_absolute() or PureWindowsPath(v).is_absolute() or v.startswith(("~", "\\")):
            raise ValueError(f"sqlite path {v!r} must be relative to the workspace, not absolute")
        if ".." in PurePosixPath(v.replace("\\", "/")).parts:
            raise ValueError(f"sqlite path {v!r} must not contain '..'")
        return v

    @field_validator("table")
    @classmethod
    def _table(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("sqlite table must be a table name")
        return v

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: dict[str, ValueSource]) -> dict[str, ValueSource]:
        if not v:
            raise ValueError("sqlite sink needs at least one column")
        if any(not name.strip() for name in v):
            raise ValueError("sqlite column names must not be empty")
        return v


LABEL_KEYED_SINKS = ("form", "airtable", "sqlite")  # sinks whose `fields` are keyed by the destination's own labels

Sink = Annotated[ApiSink | FormSink | AirtableSink | SqliteSink, Field(discriminator="kind")]


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

    def has_rows(self) -> bool:
        """contracts V18: every parser has `rows` (one record per row). Mixed specs are rejected."""
        return all(p.rows is not None for p in self.extract.parsers)

    @model_validator(mode="after")
    def _references(self) -> "Spec":
        """Every field the pipeline uses must be extracted by every parser."""
        parsers = self.extract.parsers
        common = set.intersection(*(p.all_fields() for p in parsers))
        errors: list[str] = []
        with_rows = [p.id for p in parsers if p.rows is not None]
        if with_rows and len(with_rows) != len(parsers):
            errors.append(f"extract.parsers: every parser has rows or none does (with rows: {with_rows}; "
                          f"without: {[p.id for p in parsers if p.rows is None]})")
        has_rows = len(with_rows) == len(parsers)
        doc_common = set.intersection(*(set(p.fields) for p in parsers))
        row_common = set.intersection(*(set(p.rows.fields) for p in parsers)) if has_rows else set()

        def check(ref: str, where: str) -> None:
            if ref.startswith(RECORD_PREFIX):
                return
            if ref not in common:
                errors.append(
                    f"{where} references field {ref!r}, which not every parser extracts "
                    f"(fields common to all parsers: {sorted(common)})"
                )

        for rule in self.validate_:
            where = f"validate[{rule.field}:{rule.rule}]"
            if rule.field.startswith(RECORD_PREFIX):
                errors.append(f"validate[{rule.field}]: rules apply to extracted fields, not record.* fields")
            elif rule.rule in CROSS_RULES:
                if not has_rows:
                    errors.append(f"{where}: cross-check rules only apply when every parser has rows")
                    continue
                if rule.rule == "product_equals" and rule.field not in row_common:
                    errors.append(f"{where}: field {rule.field!r} is not a row field "
                                  f"(row fields common to all parsers: {sorted(row_common)})")
                if rule.rule != "product_equals" and rule.field not in doc_common:
                    errors.append(f"{where}: field {rule.field!r} is not a document field "
                                  f"(document fields common to all parsers: {sorted(doc_common)})")
                if rule.rows_field is not None and rule.rows_field not in row_common:
                    errors.append(f"{where}: rows_field {rule.rows_field!r} is not a row field "
                                  f"(row fields common to all parsers: {sorted(row_common)})")
                for name in rule.fields or []:
                    if name not in row_common:
                        errors.append(f"{where}: fields: {name!r} is not a row field "
                                      f"(row fields common to all parsers: {sorted(row_common)})")
            else:
                check(rule.field, where)
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
            if p.rows is not None and p.rows.select.css is not None:
                users.append(f"extract.parsers[{p.id}].rows.select (css)")
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
