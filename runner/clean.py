"""Cleaners (contracts V18): a closed set of value cleaners a spec names in `clean`.

Shared by compiled parsers, the rows engine (runner/rows.py) and the agents' checks, so every reader cleans a value
the same way. Pure stdlib, no network, no AI. A value that does not fit is VALIDATION_FAILED with what was read,
never a guess: a date that two formats read differently, or an amount written with the other decimal mark, is held.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from runner.lib import Quarantine, cast

CLEANERS = ("money", "number", "date", "split", "strip", "upper", "lower")  # contracts V18

_CODE_HEAD = re.compile(r"^[A-Z]{3}(?![A-Za-z])")
_CODE_TAIL = re.compile(r"(?<![A-Za-z])[A-Z]{3}$")
_NUMBER = {".": re.compile(r"^([1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.(\d+))?$"),  # a group never starts with 0
           ",": re.compile(r"^([1-9]\d{0,2}(?:\.\d{3})+|\d+)(?:,(\d+))?$")}
_MINUS = ("-", "−")


def _fail(where: str, problem: str) -> Quarantine:
    return Quarantine("VALIDATION_FAILED", f"{where}: {problem}")


def _spec_parts(spec: str | dict) -> tuple[str, Any]:
    if isinstance(spec, str):
        return spec, None
    if isinstance(spec, dict) and len(spec) == 1:
        return next(iter(spec.items()))
    return repr(spec), None


def _strip_currency(s: str) -> str:
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Sc").strip()
    s = _CODE_HEAD.sub("", s).strip()
    return _CODE_TAIL.sub("", s).strip()


def _read_number(text: str, decimal: str, currency: bool) -> Decimal | None:
    """The amount in `text` with `decimal` as the decimal mark (the other mark groups thousands, in groups of
    3 only), or None when it is not exactly one amount. `currency` also strips symbols and ISO codes and reads
    (x) as negative. A sign may lead or trail: -5, 5-, (5)."""
    got = _read_parts(text, decimal, currency)
    return got[0] if got else None


def _read_parts(text: str, decimal: str, currency: bool) -> tuple[Decimal, bool, int] | None:
    """`_read_number`, plus whether thousands were grouped and how many fraction digits were written."""
    s = text.strip()
    if currency:
        s = _strip_currency(s)
    signs = 0
    if currency and s.startswith("(") and s.endswith(")"):
        s, signs = _strip_currency(s[1:-1]), signs + 1
    if s.startswith(_MINUS):
        s, signs = s[1:].strip(), signs + 1
    elif s.startswith("+"):
        s = s[1:].strip()
    if s.endswith(_MINUS):
        s, signs = s[:-1].strip(), signs + 1
    if currency:
        s = _strip_currency(s)
    m = _NUMBER[decimal].match(s)
    if signs > 1 or m is None:
        return None
    group_mark = "," if decimal == "." else "."
    whole = m.group(1).replace(group_mark, "")
    value = Decimal(whole + ("." + m.group(2) if m.group(2) else ""))
    return (-value if signs else value), group_mark in m.group(1), len(m.group(2) or "")


def parse_amount(value: Any) -> Decimal | None:
    """Lenient reading of a numeric cell (currency, codes, parentheses; decimal '.'), None when not numeric.
    Never raises. Used to recognise aggregate rows."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    try:
        return _read_number(str(value), ".", currency=True)
    except (InvalidOperation, ValueError):
        return None


def _amount(value: Any, args: Any, where: str, currency: bool) -> float | int:
    name = "money" if currency else "number"
    decimal = (args or {}).get("decimal", ".")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if currency else value
    text = str(value if value is not None else "")
    parts = _read_parts(text, decimal, currency)
    other = "," if decimal == "." else "."
    alt = _read_number(text, other, currency)
    if parts is None:
        hint = f"; with decimal '{other}' it would read {alt:,.2f}" if alt is not None else ""
        what = "a money amount" if currency else "a number"
        raise _fail(where, f"{text!r} is not {what} with decimal '{decimal}'{hint}")
    got, grouped, fraction = parts
    # One mark followed by exactly 3 digits, with no grouping: 1.299 is 1.299 or 1,299 depending on the sender's
    # mark. An amount is never guessed: hold it and name both readings.
    if currency and not grouped and fraction == 3 and alt is not None and alt != got:
        raise _fail(where, f"{text!r} is ambiguous: {got.normalize():f} with decimal '{decimal}' or {alt:,.2f} "
                           f"with decimal '{other}'")
    if currency or got.as_tuple().exponent < 0:  # money is always a float; a number without a fraction is an int
        return float(got)
    return int(got)


def _date(value: Any, formats: list[str], where: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = " ".join(str(value if value is not None else "").split())
    readings: list[str] = []
    for fmt in formats:
        try:
            iso = datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
        if iso not in readings:
            readings.append(iso)
    if not readings:
        raise _fail(where, f"{text!r} does not match any of the date formats {formats}")
    if len(readings) > 1:
        raise _fail(where, f"{text!r} is ambiguous: {' or '.join(readings)}")
    return readings[0]


def clean_value(value: Any, spec: str | dict, where: str) -> Any:
    """Run one cleaner (contracts V18) on a raw value. Raises Quarantine("VALIDATION_FAILED", "<where>: ...")."""
    name, args = _spec_parts(spec)
    if name == "money":
        return _amount(value, args, where, currency=True)
    if name == "number":
        return _amount(value, args, where, currency=False)
    if name == "date":
        if not isinstance(args, list) or not args:
            raise _fail(where, "clean date needs a list of formats")
        return _date(value, args, where)
    if name == "split":
        sep, part = (args or {}).get("sep"), (args or {}).get("part")
        if not sep or not isinstance(part, int):
            raise _fail(where, "clean split needs {sep, part}")
        parts = str(value if value is not None else "").split(sep)
        if not -len(parts) <= part < len(parts):
            raise _fail(where, f"{value!r} has no part {part} when split on {sep!r}")
        return parts[part].strip()
    if name in ("strip", "upper", "lower"):
        text = str(value if value is not None else "").strip()
        return text.upper() if name == "upper" else text.lower() if name == "lower" else text
    raise _fail(where, f"unknown cleaner {name!r}; use one of {list(CLEANERS)}")


def clean_cast(value: Any, spec: str | dict | None, to: str, where: str) -> Any:
    """`clean` then `cast` (contracts V18). With a cleaner, `cast: str` (the default) keeps the cleaned value as
    the cleaner returns it (a number stays a number); `int` needs a whole number."""
    if spec is None:
        return cast(value, to, where)
    value = clean_value(value, spec, where)
    if to == "str":
        return value
    if isinstance(value, float) and to == "int":
        if not value.is_integer():
            raise _fail(where, f"{value!r} is not a whole number")
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and to == "float":
        return float(value)
    return cast(value, to, where)
