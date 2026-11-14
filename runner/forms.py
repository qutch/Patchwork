"""Form sinks (contracts V5, V6): snapshot a web form, describe it as an OpenAPI-shaped contract, and
fill it by label in headless Chromium. No AI; Playwright only.

Safety rules, enforced here and not by callers:
  * a label resolves to exactly one control, or the record is held (SINK_CONTRACT); every label is
    resolved before anything is typed, so a failure never leaves a half-filled neighbour;
  * the submit button is matched by its exact accessible name ("Clear form" is never a match);
  * a dry run fills but never clicks submit.

Label resolution (spike a, scripts/spike_google_form.py): first by accessible name, else the single
control inside the role=listitem whose role=heading text equals the label. Both sides are
whitespace-normalised and compared case-insensitively, with required markers ("*", "(required)",
[aria-label="Required question"]) ignored. A name match that disagrees with a heading match is
ambiguous and fails closed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from runner.lib import Quarantine

LOAD_TIMEOUT_MS = 15_000
CONFIRM_TIMEOUT_MS = 10_000
ACTION_TIMEOUT_MS = 5_000  # one fill or click; a stuck control must not hang the record for 30 s
X_INPUTS = ("text", "textarea", "email", "url", "date", "number", "radio", "checkbox", "select")

# Anything a person can type into or choose in. Used to keep accessible-name matches to controls.
_CONTROL = ("input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select, [role=textbox], "
            "[role=radiogroup], [role=listbox], [role=combobox], [role=checkbox], [role=radio]")
_QUESTION = "[role=listitem]:has([role=heading])"
# A Google question, or any plain control: whichever shows first means the form has rendered.
_FIRST_FIELD = (f"{_QUESTION}, input:not([type=hidden]):not([type=submit]):not([type=button])"
                ":not([type=reset]), select, textarea")
_REQUIRED_SUFFIX = re.compile(r"\s*(?:\*|\(required\)|required question)\s*$", re.I)

# Helpers shared by the snapshot and the alert reader. A control's label follows its accessible name:
# aria-labelledby, then aria-label, then <label for> / a wrapping <label>. Radio and checkbox groups are
# labelled by their fieldset's <legend>.
_HELPERS_JS = r"""
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const stripReq = s => s.replace(/\s*(?:\*|\(required\))\s*$/i, '');
  const cleanLabel = el => {
    const c = el.cloneNode(true);
    c.querySelectorAll('input, select, textarea, button').forEach(x => x.remove());
    return stripReq(norm(c.textContent));
  };
  const ctlLabel = e => {
    const by = e.getAttribute('aria-labelledby');
    if (by) {
      const t = norm(by.split(/\s+/).map(id => (document.getElementById(id) || {}).textContent || '').join(' '));
      if (t) return stripReq(t);
    }
    const al = norm(e.getAttribute('aria-label'));
    if (al) return al;
    for (const l of (e.labels || [])) { const t = cleanLabel(l); if (t) return t; }
    return '';
  };
  const legendOf = e => {
    const fs = e.closest('fieldset');
    const lg = fs && fs.querySelector(':scope > legend');
    return lg ? cleanLabel(lg) : '';
  };
  const fieldLabel = e => (e.type === 'radio' || e.type === 'checkbox') ? (legendOf(e) || ctlLabel(e)) : ctlLabel(e);
"""

# One pass over the page: every question as {label, input, required, options}. Google Forms questions
# (role=listitem with a role=heading) are read first and drop the [aria-label="Required question"]
# marker; a page with none of those is read as plain HTML.
_SNAPSHOT_JS = "() => {" + _HELPERS_JS + r"""
  const out = [];
  for (const li of document.querySelectorAll('[role=listitem]')) {
    const h = li.querySelector('[role=heading]');
    if (!h) continue;
    const c = h.cloneNode(true);
    c.querySelectorAll('[aria-label="Required question"]').forEach(e => e.remove());
    const label = norm(c.textContent).replace(/\s*\*$/, '');
    const radios = li.querySelectorAll('[role=radio], input[type=radio]');
    const boxes = li.querySelectorAll('[role=checkbox], input[type=checkbox]');
    const lists = li.querySelectorAll('[role=listbox], select');
    const area = li.querySelector('textarea');
    const inp = li.querySelector('input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=submit]):not([type=button])');
    const optName = e => norm(e.getAttribute('aria-label') || e.getAttribute('data-value') || e.value || e.textContent);
    let input = null, options = [];
    if (radios.length) { input = 'radio'; options = [...radios].map(optName); }
    else if (boxes.length) { input = 'checkbox'; options = [...boxes].map(optName); }
    else if (lists.length) {
      input = 'select';
      options = [...li.querySelectorAll('[role=option], option')]
        .map(o => norm(o.getAttribute('data-value') ?? o.textContent)).filter(o => o && o !== 'Choose');
    }
    else if (area) input = 'textarea';
    else if (inp) {
      const t = (inp.getAttribute('type') || 'text').toLowerCase();
      input = ['email', 'url', 'date', 'number'].includes(t) ? t : 'text';
    }
    if (!input) continue;  // a heading with nothing to answer (section title, image)
    const required = !!(li.querySelector('[aria-label="Required question"]')
                        || li.querySelector('[required], [aria-required="true"]'));
    out.push({label, input, required, options: [...new Set(options.filter(Boolean))]});
  }

  if (out.length) return out;
  const seen = new Set();
  const CTL = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), textarea, select';
  for (const e of document.querySelectorAll(CTL)) {
    if (e.disabled || seen.has(e)) continue;
    const grouped = e.type === 'radio' || e.type === 'checkbox';
    if (grouped) {
      const fs = e.closest('fieldset');
      const label = legendOf(e);
      if (!fs || !label) continue;  // nothing to address a loose radio or checkbox by
      const mates = [...fs.querySelectorAll(CTL)].filter(x => x.type === e.type && x.closest('fieldset') === fs);
      mates.forEach(x => seen.add(x));
      const options = mates.map(x => ctlLabel(x) || norm(x.value));
      const required = mates.some(x => x.required || x.getAttribute('aria-required') === 'true')
                       || fs.getAttribute('aria-required') === 'true';
      out.push({label, input: e.type, required, options: [...new Set(options.filter(Boolean))]});
      continue;
    }
    const label = ctlLabel(e);
    if (!label) continue;
    let input, options = [];
    if (e.tagName === 'SELECT') {
      input = 'select';
      options = [...e.options].filter(o => o.value !== '').map(o => norm(o.textContent)).filter(Boolean);
    } else if (e.tagName === 'TEXTAREA') input = 'textarea';
    else {
      const t = (e.getAttribute('type') || 'text').toLowerCase();
      input = ['email', 'url', 'date', 'number'].includes(t) ? t : 'text';
    }
    const star = [...(e.labels || [])].some(l => /\*\s*$/.test(norm(l.textContent)));
    const required = e.required || e.getAttribute('aria-required') === 'true' || star;
    out.push({label, input, required, options: [...new Set(options)]});
  }
  return out;
}
"""

_FIELDSET = "fieldset:has(> legend)"
_LEGENDS_JS = r"""
fs => fs.map(f => { const c = f.querySelector(':scope > legend').cloneNode(true);
  return (c.textContent || '').replace(/\s+/g, ' ').trim(); })
"""

_ITEM_LABELS_JS = r"""
items => items.map(li => {
  const h = li.querySelector('[role=heading]');
  const c = h.cloneNode(true);
  c.querySelectorAll('[aria-label="Required question"]').forEach(e => e.remove());
  return (c.textContent || '').replace(/\s+/g, ' ').trim();
})
"""


def _norm(label: str) -> str:
    return _REQUIRED_SUFFIX.sub("", " ".join((label or "").split())).casefold()


@dataclass
class FormResult:
    """What a form sink's send() returns: shaped like an HTTP response for runner/pipeline.py."""

    status_code: int = 200
    text: str = ""
    detail: str | None = None
    seconds: float = 0.0


# ---- page-level API (contracts V5) ---------------------------------------------------------------

def open_form(page: Any, url: str, *, timeout_ms: int = LOAD_TIMEOUT_MS) -> None:
    """Load the form and wait for its first question or field. Not loading in time -> UNKNOWN."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.locator(_FIRST_FIELD).first.wait_for(state="attached", timeout=timeout_ms)
    except Exception as e:  # noqa: BLE001 - playwright raises its own Error/TimeoutError types
        raise Quarantine("UNKNOWN", f"form {url} did not load in {timeout_ms // 1000} s: "
                                    f"{str(e).splitlines()[0][:200]}") from None


def snapshot_form(page: Any) -> list[dict]:
    """[{label, input, required, options}] for every question, in page order."""
    return page.evaluate(_SNAPSHOT_JS)


def form_contract(snapshot: list[dict], *, title: str, url: str) -> dict:
    """The V6 document. Only stable facts: labels, input kinds, required flags and options."""
    props: dict[str, dict] = {}
    counts: dict[str, int] = {}
    required: list[str] = []
    for q in snapshot:
        label = q["label"]
        counts[label] = counts.get(label, 0) + 1
        prop: dict[str, Any] = {"type": "string", "x-input": q["input"] if q["input"] in X_INPUTS else "text"}
        if q["input"] in ("radio", "select") and q.get("options"):
            prop["enum"] = list(q["options"])
        elif q["input"] == "checkbox" and q.get("options"):
            prop["x-options"] = list(q["options"])  # several may be ticked: not an enum
        props.setdefault(label, prop)
        if q["required"] and label not in required:
            required.append(label)
    for label, n in counts.items():
        if n > 1:
            props[label]["x-count"] = n  # two questions share a label: filling it fails closed
    schema = {"type": "object", "additionalProperties": False, "required": required, "properties": props}
    return {"openapi": "3.0.3",
            "info": {"title": title, "x-patchwork-kind": "form", "x-form-url": url},
            "paths": {"/form": {"post": {"requestBody": {"content": {"application/json": {"schema": schema}}}}}}}


def _kind_of(loc: Any) -> str:
    return loc.evaluate("""e => {
      const role = e.getAttribute('role');
      if (role === 'radiogroup' || role === 'radio' || e.type === 'radio') return 'radio';
      if (role === 'checkbox' || e.type === 'checkbox') return 'checkbox';
      if (role === 'listbox' || role === 'combobox' || e.tagName === 'SELECT') return 'select';
      if (e.tagName === 'TEXTAREA') return 'textarea';
      const t = (e.getAttribute('type') || 'text').toLowerCase();
      return ['email', 'url', 'date', 'number'].includes(t) ? t : 'text';
    }""")


def _item_control(item: Any, label: str) -> tuple[str, Any]:
    for kind, sel in (("radio", "[role=radio], input[type=radio]"), ("checkbox", "[role=checkbox], input[type=checkbox]")):
        if item.locator(sel).count():
            return kind, item  # a group: the option is picked by name when filling
    ctl = item.locator(_CONTROL)
    n = ctl.count()
    if n != 1:
        raise Quarantine("SINK_CONTRACT", f"form question {label!r} holds {n} controls; expected exactly 1")
    return _kind_of(ctl), ctl


def _labels(page: Any) -> list[str]:
    return page.locator(_QUESTION).evaluate_all(_ITEM_LABELS_JS)


def resolve_control(page: Any, label: str) -> tuple[str, Any]:
    """-> (kind, locator) for exactly one control, else Quarantine(SINK_CONTRACT)."""
    target = _norm(label)
    if not target:
        raise Quarantine("SINK_CONTRACT", "empty form label")
    plain = _REQUIRED_SUFFIX.sub("", " ".join(label.split()))
    words = r"\s+".join(re.escape(w) for w in plain.split())
    by_name = page.get_by_label(re.compile(rf"^\s*{words}(?:\s*(?:\*|\(required\)|required question))?\s*$", re.I))
    by_name = by_name.and_(page.locator(_CONTROL))
    n_name = by_name.count()
    item_labels = _labels(page)
    hits = [i for i, text in enumerate(item_labels) if _norm(text) == target]
    if n_name > 1:
        raise Quarantine("SINK_CONTRACT", f"form label {label!r} matches {n_name} controls by accessible name")
    if n_name == 1:
        el = by_name.element_handle()
        for i in hits:
            if not page.locator(_QUESTION).nth(i).evaluate("(li, el) => li.contains(el)", el):
                raise Quarantine("SINK_CONTRACT", f"form label {label!r} is ambiguous: a control is named "
                                                  "that way and a different question has that title")
        return _kind_of(by_name), by_name
    if len(hits) > 1:
        raise Quarantine("SINK_CONTRACT", f"form label {label!r} matches {len(hits)} questions "
                                          f"(form has: {item_labels})")
    if not hits:  # plain HTML: a fieldset's <legend> names its radio or checkbox group
        fieldsets = page.locator(_FIELDSET)
        legends = fieldsets.evaluate_all(_LEGENDS_JS)
        lhits = [i for i, text in enumerate(legends) if _norm(text) == target]
        if len(lhits) == 1:
            return _item_control(fieldsets.nth(lhits[0]), label)
        what = "no question" if not lhits else f"{len(lhits)} fieldsets"
        raise Quarantine("SINK_CONTRACT", f"form label {label!r} matches {what} "
                                          f"(form has: {item_labels + legends})")
    return _item_control(page.locator(_QUESTION).nth(hits[0]), label)


def _fill_one(page: Any, label: str, kind: str, loc: Any, value: str) -> None:
    try:
        _fill_control(page, label, kind, loc, value)
    except Quarantine:
        raise
    except Exception as e:  # noqa: BLE001 - e.g. the control is hidden or disabled
        raise Quarantine("SINK_CONTRACT", f"form question {label!r} could not be filled: "
                                          f"{str(e).splitlines()[0][:200]}") from None


def _fill_control(page: Any, label: str, kind: str, loc: Any, value: str) -> None:
    t = ACTION_TIMEOUT_MS
    if kind == "radio":
        opt = loc.get_by_role("radio", name=value, exact=True)
        if opt.count() != 1:
            raise Quarantine("SINK_CONTRACT", f"form question {label!r} has no option {value!r}")
        opt.click(timeout=t)
    elif kind == "checkbox":
        for v in [v.strip() for v in value.split(",") if v.strip()]:
            opt = loc.get_by_role("checkbox", name=v, exact=True)
            if opt.count() != 1:
                raise Quarantine("SINK_CONTRACT", f"form question {label!r} has no option {v!r}")
            opt.click(timeout=t)
    elif kind == "select":
        if loc.evaluate("e => e.tagName") == "SELECT":
            if value not in loc.locator("option").all_inner_texts():
                raise Quarantine("SINK_CONTRACT", f"form question {label!r} has no option {value!r}")
            loc.select_option(label=value, timeout=t)
        else:
            loc.click(timeout=t)
            opt = page.get_by_role("option", name=value, exact=True)
            if opt.count() < 1:
                raise Quarantine("SINK_CONTRACT", f"form question {label!r} has no option {value!r}")
            opt.last.click(timeout=t)
    else:
        loc.fill(value, timeout=t)
        got = loc.input_value(timeout=t)
        if got != value:
            raise Quarantine("SINK_CONTRACT", f"form question {label!r} did not accept {value!r} (shows {got!r})")


_ALERTS_JS = "() => {" + _HELPERS_JS + r"""
  const out = [];
  const add = t => { t = (t || '').trim(); if (t && !out.includes(t)) out.push(t); };
  document.querySelectorAll('[role=alert]').forEach(a => add(a.innerText || a.textContent));
  const ids = (e, attr) => (e.getAttribute(attr) || '').split(/\s+/).filter(Boolean)
    .map(id => norm((document.getElementById(id) || {}).textContent)).filter(Boolean).join(' ');
  document.querySelectorAll('input, select, textarea').forEach(e => {
    const name = fieldLabel(e);
    const bad = e.matches(':invalid') ? e.validationMessage : '';
    const flagged = e.getAttribute('aria-invalid') === 'true'
      ? (ids(e, 'aria-errormessage') || ids(e, 'aria-describedby') || 'invalid') : '';
    const msg = bad || flagged;
    if (msg) add(name ? name + ': ' + msg : msg);
  });
  return out;
}
"""


def _alerts(page: Any) -> list[str]:
    """Errors the page is showing: role=alert text, HTML5 validation messages and aria-invalid fields."""
    try:
        return [a for a in page.evaluate(_ALERTS_JS) if a]
    except Exception:  # noqa: BLE001
        return []


def fill_and_submit(page: Any, values: dict[str, str], *, submit: str = "Submit",
                    confirmation_text: str | None = None, dry_run: bool,
                    confirm_timeout_ms: int = CONFIRM_TIMEOUT_MS) -> None:
    """Fill every label on the open form, then click `submit` and wait for `confirmation_text`.

    `dry_run=True` fills and checks the submit button exists, and never clicks it."""
    values = {k: "" if v is None else str(v) for k, v in values.items()}
    resolved = {label: resolve_control(page, label) for label in values}  # all before typing anything
    button = page.get_by_role("button", name=submit, exact=True)
    n = button.count()
    if n != 1:
        raise Quarantine("SINK_CONTRACT", f"submit button {submit!r} matches {n} buttons; expected exactly 1")
    for label, (kind, loc) in resolved.items():
        _fill_one(page, label, kind, loc, values[label])
    if dry_run:
        return  # the guarantee: a dry run never submits
    try:
        button.click(timeout=ACTION_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001
        raise Quarantine("SINK_CONTRACT", f"submit button {submit!r} could not be clicked: "
                                          f"{str(e).splitlines()[0][:200]}") from None
    if confirmation_text is None:
        page.wait_for_load_state("domcontentloaded", timeout=confirm_timeout_ms)
        return
    try:
        page.get_by_text(confirmation_text, exact=False).first.wait_for(timeout=confirm_timeout_ms)
    except Exception:  # noqa: BLE001
        raise Quarantine("VERIFY_MISMATCH", f"no confirmation {confirmation_text!r} after submit "
                                            f"(url {page.url}; alerts {_alerts(page)})") from None


# ---- a run's browser -----------------------------------------------------------------------------

class FormSession:
    """One headless Chromium per run (launched on first use), a fresh context and page per record."""

    def __init__(self, *, dry_run: bool = False, headless: bool = True):
        self.dry_run = dry_run
        self.headless = headless
        self._pw: Any = None
        self._browser: Any = None
        self.timings: list[dict[str, float]] = []

    def browser(self) -> Any:
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            t = time.perf_counter()
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self.timings.append({"launch_s": time.perf_counter() - t})
        return self._browser

    def _page(self) -> tuple[Any, Any]:
        ctx = self.browser().new_context()
        return ctx, ctx.new_page()

    def contract(self, url: str) -> dict:
        ctx, page = self._page()
        try:
            open_form(page, url)
            return form_contract(snapshot_form(page), title=page.title(), url=url)
        finally:
            ctx.close()

    def submit(self, url: str, values: dict, *, submit: str = "Submit",
               confirmation_text: str | None = None) -> FormResult:
        t0 = time.perf_counter()
        ctx, page = self._page()
        try:
            open_form(page, url)
            t1 = time.perf_counter()
            fill_and_submit(page, values, submit=submit, confirmation_text=confirmation_text, dry_run=self.dry_run)
            t2 = time.perf_counter()
        finally:
            ctx.close()
        self.timings.append({"load_s": t1 - t0, "fill_submit_s": t2 - t1})
        if self.dry_run:
            return FormResult(200, "dry run", detail="dry run: filled, not submitted", seconds=t2 - t0)
        return FormResult(200, "confirmed", seconds=t2 - t0)

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._browser = self._pw = None


def fetch_form_contract(url: str) -> dict:
    """Sync; launches its own browser (for the compiler, control plane and agents). Safe to call from a
    thread that is running an asyncio loop: then the browser runs in a worker thread."""
    import asyncio

    def _fetch() -> dict:
        session = FormSession()
        try:
            return session.contract(url)
        finally:
            session.close()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _fetch()
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_fetch).result()
