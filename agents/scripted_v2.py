"""The v2 stage-fallback storylines for ScriptedAgentService: LEARN, TEACH (page -> form) and REPAIR of a
renamed form question, and TEACH from example pages (mapper -> learner per other site -> builder). No model. They emit the deep agent's event types and call the *same* tool functions
(agents.deep_tools.tool_functions), so every check is real: a parser this code proposes still goes through
test_page_parser's evidence/corroboration/ambiguity checks and save_parser's D2 rule.

The only thing scripted here is the choice a model would make: `propose_parser` picks extractors from the
page with a fixed preference order (JSON-LD JobPosting, a single-valued heading, meta tags, the <title>).
Fields other than a job's title/company (V15: products, papers, recipes) try the readers the integration's
parsers already use, then the field's name as a JSON-LD path on the page's types, then the usual headings/meta.
"""

from __future__ import annotations

import json
import time
from typing import Any

from agents import learn
from agents.interface import _Thread

# Candidate extractors per field, in preference order (what a careful person tries first).
TITLE_CANDIDATES = [
    {"jsonld": "title"},
    {"css": "h1"},
    {"css": 'meta[property="og:title"]', "attr": "content"},
    {"css": "head title", "regex": r"^(.+?)\s+(?:[-|@–]|at)\s+"},
]
COMPANY_CANDIDATES = [
    {"jsonld": "hiringOrganization.name"},
    {"jsonld": "name", "jsonld_type": "Organization"},
    {"css": 'meta[property="og:site_name"]', "attr": "content", "regex": r"^Careers at (.+)$"},
    {"css": 'meta[property="og:site_name"]', "attr": "content"},
    {"css": "head title", "regex": r"\bat ([^|@–-]+?)\s*$"},
    {"css": "head title", "regex": r"@\s*([^|–-]+?)\s*$"},
]
CANDIDATES = {"title": TITLE_CANDIDATES, "company": COMPANY_CANDIDATES}
SKIP_FIELD = "Skip this field"


def generic_candidates(name: str, spec: dict, rec: dict) -> list[dict]:
    """Readers to try for a field with no fixed list (V15): the integration's own readers for it (JSON-LD
    first), the same JSON-LD paths and the field name on every JSON-LD type of the page, then h1 / og:title."""
    html = rec.get((spec.get("source") or {}).get("html_field") or "html") or ""
    page_types = learn.jsonld_types(html)
    types = list(dict.fromkeys(learn.integration_jsonld_types(spec) + page_types))
    own = [dict(p["fields"][name]) for p in (spec.get("extract") or {}).get("parsers") or []
           if isinstance((p.get("fields") or {}).get(name), dict)]
    out: list[dict] = [f for f in own if f.get("jsonld") is not None]
    for path in dict.fromkeys([str(f["jsonld"]) for f in out] + [name]):
        out += [{"jsonld": path, "jsonld_type": t} for t in types]
    out += [f for f in own if f.get("jsonld") is None]
    if name in ("name", "title", "headline"):
        out += [{"css": "h1"}, {"css": 'meta[property="og:title"]', "attr": "content"}]
    seen, uniq = set(), []
    for f in out:
        key = json.dumps(f, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def _ok(out: str) -> bool:
    from agents.deep_tools import FAIL_PREFIXES
    s = out.lstrip()
    return not (s.upper().startswith(FAIL_PREFIXES) or s.startswith("Error") or s.startswith("REJECTED"))


def _short(s: str, n: int = 400) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class ScriptedV2Mixin:
    """Mixed into ScriptedAgentService (which provides emit/interrupt/_pause)."""

    def _call(self, t: _Thread, agent: str, fns: dict, tool: str, **args: Any) -> str:
        self.emit(t, "tool_call", agent=agent, tool=tool, args=args)
        self._pause()
        out = fns[tool](**args)
        self.emit(t, "tool_result", agent=agent, tool=tool, ok=_ok(out), summary=_short(out))
        return out

    # ---- LEARN -------------------------------------------------------------------------------
    def propose_parser(self, rec: dict, spec: dict) -> dict | None:
        """The extractors a model would pick: per wanted field, the first candidate whose value is
        evidenced, corroborated and unambiguous on this page; failing that, the first evidenced and unambiguous
        one (its value is on the page but nowhere else, e.g. only in a `citation_*` meta tag: save_parser's D2
        rule then has a person decide). None if a field has no evidenced, unambiguous candidate."""
        from agents.deep_tools import site_parser_id
        parsers = (spec.get("extract") or {}).get("parsers") or []
        wanted = [f for f in (parsers[0]["fields"] if parsers else {}) if all(f in p["fields"] for p in parsers)]
        html_field = (spec.get("source") or {}).get("html_field") or "html"
        fields = {}
        for name in wanted:
            fallback = None
            for cand in CANDIDATES.get(name) or generic_candidates(name, spec, rec):
                probe = {"id": "probe", "fields": {name: cand}}
                out = learn.run_page_parser(probe, rec, html_field)
                if "error" in out:
                    continue
                ev = learn.evidence(rec, out, spec.get("validate") or [], parser=probe, html_field=html_field)
                if not ev[0]["ok"] or learn.ambiguity(rec, probe, html_field):
                    continue
                if learn.corroborated(rec, out[name], html_field):
                    fields[name] = cand
                    break
                fallback = fallback or cand
            else:
                if fallback is None:
                    return None
                fields[name] = fallback
        return {"id": site_parser_id(rec.get("site") or ""), "fields": fields}

    def _run_learn(self, t: _Thread, page_id: str, integration: str) -> Any:
        from agents.deep_tools import ToolContext, seed_learn_workdir, tool_functions
        t0 = time.monotonic()
        seed_learn_workdir(integration)
        ctx = ToolContext(self, t, name=integration, page_id=page_id)
        fns = tool_functions(ctx)
        rec = ctx.get_page_record(page_id)
        site = rec.get("site") or "?"
        todos = [{"content": "Read the held page", "status": "in_progress"},
                 {"content": "Write and test a parser", "status": "pending"},
                 {"content": "Save it (code decides on deploy)", "status": "pending"}]
        self.emit(t, "todos", todos=todos)
        self.emit(t, "message", agent="supervisor", text=f"No parser reads {site} yet. Delegating to the learner.")
        self.emit(t, "subagent_start", name="learner", task=f"Learn page {page_id} (site {site}) for {integration}")
        self._call(t, "learner", fns, "get_page")
        spec = ctx.spec_dict() if ctx.spec_path.exists() else {}
        parser = self.propose_parser(rec, spec)
        if parser is None:
            self.emit(t, "subagent_end", name="learner", summary=f"No reader passed the checks on {site}")
            raise RuntimeError(f"scripted learner found no checked reader for {site}; the page stays held")
        todos[0]["status"], todos[1]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        tested = self._call(t, "learner", fns, "test_page_parser", parser=parser)
        if not tested.startswith("OK"):
            self.emit(t, "subagent_end", name="learner", summary=_short(tested))
            raise RuntimeError(f"candidate parser failed its test: {_short(tested, 300)}")
        todos[1]["status"], todos[2]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        saved = self._call(t, "learner", fns, "save_parser", parser=parser, reason=f"new layout on {site}")
        self.emit(t, "subagent_end", name="learner", summary=_short(saved))
        if ctx.learn_result is None:
            raise RuntimeError(f"save_parser refused: {_short(saved, 300)}")
        todos[2]["status"] = "completed"
        self.emit(t, "todos", todos=todos)
        r = ctx.learn_result
        self.emit(t, "message", agent="supervisor",
                  text=f"Learned {site}: parser {r['parser_id']} v{r['version']}, "
                       + ("deployed automatically (every check passed)." if r["approval"] == "auto" else
                          f"{'deployed' if r['deployed'] else 'not deployed'} after a person's decision."))
        return {"model_calls": 0, "seconds": round(time.monotonic() - t0, 1), **r}

    # ---- TEACH (page -> form) ------------------------------------------------------------------
    def _build_pages(self, t: _Thread, recording_id: str, name: str) -> Any:
        from agents.deep_tools import ToolContext, tool_functions
        from agents.scripted import load_recording, work_dir
        work_dir(name)
        ctx = ToolContext(self, t, name=name, recording_id=recording_id)
        if ctx.examples_mode():  # TEACH from example pages (wizard approach A)
            return self._build_from_examples(t, recording_id, name)
        fns = tool_functions(ctx)
        rec = load_recording(recording_id)
        todos = [{"content": "Interpret the recording into a draft spec", "status": "in_progress"},
                 {"content": "Confirm the typed constant with the user", "status": "pending"},
                 {"content": "Compile the spec and run the generated tests", "status": "pending"},
                 {"content": "Request deploy", "status": "pending"}]
        self.emit(t, "todos", todos=todos)
        self.emit(t, "message", agent="supervisor", text=f"Intent: “{rec.get('intent')}”. Delegating interpretation.")
        self.emit(t, "subagent_start", name="interpreter", task="Page -> form recording -> spec draft")
        self._call(t, "interpreter", fns, "get_recording_summary", recording_id=recording_id)
        self._call(t, "interpreter", fns, "get_candidates", recording_id=recording_id)
        drafted = self._call(t, "interpreter", fns, "draft_spec")
        if not drafted.endswith("valid") or "INVALID" in drafted:
            raise RuntimeError(f"draft_spec: {_short(drafted, 300)}")
        self._call(t, "interpreter", fns, "test_parser")
        spec = ctx.spec_dict()
        consts = {k: v["const"] for k, v in spec["sink"]["fields"].items() if "const" in v}
        self._call(t, "interpreter", fns, "validate_spec")
        self.emit(t, "subagent_end", name="interpreter", summary=_short(drafted, 300))
        todos[0]["status"], todos[1]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        if consts:
            label, value = next(iter(consts.items()))
            ans = self.interrupt(t, "ask_user", question=f"You typed {value!r} into {label!r}. Send that on every row?",
                                 options=[f"Yes, always {value}", "No, I'll fix it in the spec"])
            self.emit(t, "message", agent="supervisor", text=f"Recorded: {ans.get('answer', '')}")
        todos[1]["status"], todos[2]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        self.emit(t, "subagent_start", name="builder", task="Compile the spec into the next version")
        compiled = self._call(t, "builder", fns, "compile_spec")
        self.emit(t, "subagent_end", name="builder", summary=_short(compiled))
        if not compiled.startswith("compiled"):
            raise RuntimeError(_short(compiled, 300))
        todos[2]["status"], todos[3]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        version = ctx.last_version["version"]
        self._call(t, "supervisor", fns, "request_deploy", version=version,
                   summary="Initial version taught from the recording (page -> form).")
        return {"model_calls": 0, **(ctx.deploy_result or {"deployed": False, "version": version})}

    # ---- TEACH from example pages (wizard approach A) -------------------------------------------
    def _map_examples(self, t: _Thread, ctx: Any, fns: dict, dest: dict[str, dict], pages: list[dict]) -> dict[str, dict]:
        """The mapping a model would propose: the page's address for a url field, today for a date field, else
        `learn.guess_reader` on the first page. A field nothing fills becomes a question to the person, with the
        checked alternatives and "Skip this field" (skip is not offered for a required field)."""
        from agents.deep_tools import _DATEISH, _URLISH, _field_name, review_alternatives, source_key
        first = pages[0]
        mapping: dict[str, dict] = {}
        taken: set[str] = set()
        for label, info in dest.items():
            kind = info.get("type") or "text"
            words = set(learn._words(label))
            if kind == "array":
                mapping[label] = {"skip": True}
            elif kind == "url" or words & _URLISH:
                mapping[label] = {"record": "canonical_url"}
            elif kind == "date" or words & _DATEISH:
                mapping[label] = {"today": True}
            else:
                reader = learn.guess_reader(label, kind, first)
                if reader is not None:
                    mapping[label] = {"field": _field_name(label), "reader": reader}
                    taken.add(learn.normalize_ws(str(learn.check_reader(reader, first)["value"])))
        for label, info in dest.items():
            if label in mapping:
                continue
            alts = [a for a in review_alternatives(label, info, {"skip": True}, pages, taken)
                    if not a.get("skip")]
            options = [a["label"] for a in alts] + ([] if info.get("required") else [SKIP_FIELD])
            if not options:
                raise RuntimeError(f"nothing on the example pages fills the required field {label!r}")
            out = self._call(t, "mapper", fns, "ask_user",
                             question=f"Nothing on the first example page clearly fills {label!r} "
                                      f"({info.get('type') or 'text'}). Where should it come from?",
                             options=options, dest_field=label)
            answer = out.removeprefix("user answered:").strip()
            chosen = next((a for a in alts if a["label"] == answer), None)
            if chosen is None and answer != SKIP_FIELD:
                if info.get("required"):
                    raise RuntimeError(f"no source chosen for the required field {label!r}")
                answer = SKIP_FIELD
            mapping[label] = ({"skip": True} if chosen is None else
                              {k: v for k, v in chosen.items() if k not in ("label", "values")})
            ctx.confirmed[label] = source_key(mapping[label])  # a person chose this source: not AI-proposed
            self.emit(t, "message", agent="mapper", text=f"Recorded for {label}: {answer}")
        return mapping

    def _site_parser(self, rec: dict, spec: dict, kinds: dict[str, tuple[str, str]]) -> dict | None:
        """The readers a model would pick on another example site: the first parser's reader when it works
        here, else `learn.guess_reader`, else the generic candidates. None if a field has no checked reader."""
        parsers = (spec.get("extract") or {}).get("parsers") or []
        fields: dict[str, dict] = {}
        for fname, (label, kind) in kinds.items():
            rules = learn.type_rules("value", kind)
            tries = [dict(p["fields"][fname]) for p in parsers if isinstance((p.get("fields") or {}).get(fname), dict)]
            guess = learn.guess_reader(label, kind, rec)
            tries += ([guess] if guess else []) + generic_candidates(fname, spec, rec)
            for cand in tries:
                cand = {k: v for k, v in cand.items() if not (k == "cast" and v == "str") and v not in (None, {})}
                if learn.check_reader(cand, rec, rules)["ok"]:
                    fields[fname] = cand
                    break
            else:
                return None
        return {"fields": fields}

    def _build_from_examples(self, t: _Thread, recording_id: str, name: str) -> Any:
        from agents.deep_tools import ToolContext, examples_dest_fields, tool_functions
        from agents.scripted import work_dir
        t0 = time.monotonic()
        work_dir(name)
        ctx = ToolContext(self, t, name=name, recording_id=recording_id)
        fns = tool_functions(ctx)
        c = ctx.examples_candidates() or {}
        pages = ctx.example_records()
        if not pages:
            raise RuntimeError("the draft has no example pages")
        dest = examples_dest_fields(c, ctx.dest_contract())
        todos = [{"content": "Map the destination fields", "status": "in_progress"},
                 {"content": "Add a parser for each other example site", "status": "pending"},
                 {"content": "Compile and test", "status": "pending"},
                 {"content": "Request deploy", "status": "pending"}]
        self.emit(t, "todos", todos=todos)
        self.emit(t, "message", agent="supervisor",
                  text=f"Intent: “{c.get('intent') or name}”. {len(pages)} example page(s). Delegating the mapping.")
        first = pages[0]
        self.emit(t, "subagent_start", name="mapper",
                  task=f"Map the destination fields of {name} on page {first.get('id')} ({first.get('site')})")
        self._call(t, "mapper", fns, "get_candidates", recording_id=recording_id)
        self._call(t, "mapper", fns, "get_page", page_id=str(first.get("id")))
        mapping = self._map_examples(t, ctx, fns, dest, pages)
        mapped = self._call(t, "mapper", fns, "propose_mapping", mapping=mapping)
        self.emit(t, "subagent_end", name="mapper", summary=_short(mapped))
        if not mapped.startswith("OK"):
            raise RuntimeError(f"propose_mapping refused: {_short(mapped, 300)}")
        kinds = {m["field"]: (label, (dest.get(label) or {}).get("type") or "text")
                 for label, m in mapping.items() if "reader" in m}
        todos[0]["status"], todos[1]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        for rec in pages[1:]:
            pid, site = str(rec.get("id")), rec.get("site")
            self.emit(t, "subagent_start", name="learner", task=f"Add a parser for page {pid} ({site}) reading "
                                                               + ", ".join(kinds))
            self._call(t, "learner", fns, "get_page", page_id=pid)
            parser = self._site_parser(rec, ctx.spec_dict(), kinds)
            if parser is None:
                self.emit(t, "subagent_end", name="learner", summary=f"No checked reader on {site}")
                raise RuntimeError(f"scripted learner found no checked readers for {site}")
            added = self._call(t, "learner", fns, "add_site_parser", page_id=pid, parser=parser)
            self.emit(t, "subagent_end", name="learner", summary=_short(added))
            if not added.startswith("OK"):
                raise RuntimeError(f"add_site_parser refused: {_short(added, 300)}")
        todos[1]["status"], todos[2]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        self.emit(t, "subagent_start", name="builder", task="Compile spec.yaml and run its tests")
        compiled = self._call(t, "builder", fns, "compile_spec")
        self.emit(t, "subagent_end", name="builder", summary=_short(compiled))
        if not compiled.startswith("compiled"):
            raise RuntimeError(_short(compiled, 300))
        todos[2]["status"], todos[3]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        version = ctx.last_version["version"]
        sites = ", ".join(str(r.get("site")) for r in pages)
        self._call(t, "supervisor", fns, "request_deploy", version=version,
                   summary=f"Taught from {len(pages)} example page(s) ({sites}).")
        todos[3]["status"] = "completed"
        self.emit(t, "todos", todos=todos)
        return {"model_calls": 0, "seconds": round(time.monotonic() - t0, 1),
                **(ctx.deploy_result or {"deployed": False, "version": version})}

    # ---- REPAIR (a destination field renamed) ----------------------------------------------------
    @staticmethod
    def _is_form_integration(name: str) -> bool:
        """True when a CONTRACT_CHANGED incident of `name` is repaired by following renamed destination fields:
        a form, Airtable or SQLite sink (V14, V16), or a page integration's API sink (the wizard's)."""
        import yaml

        from runner.store import Store, workspace_dir
        active = Store().active_version(name)
        path = workspace_dir() / "integrations" / name / f"v{active}" / "spec.yaml"
        if not active or not path.exists():
            return False
        from agents.deep_tools import is_label_keyed, is_page_integration
        spec = yaml.safe_load(path.read_text()) or {}
        return is_label_keyed(spec) or is_page_integration(spec)

    def _repair_form(self, t: _Thread, inc: dict) -> Any:
        from agents.deep_tools import (ToolContext, _ptr, is_label_keyed, seed_repair_workdir, sink_kind,
                                       sink_values_path, tool_functions)
        name = inc["integration"]
        seed_repair_workdir(name)
        ctx = ToolContext(self, t, name=name, incident_id=inc["id"])
        fns = tool_functions(ctx)
        todos = [{"content": "Diagnose the form change", "status": "in_progress"},
                 {"content": "Patch the spec and migrate goldens", "status": "pending"},
                 {"content": "Compile and test the new version", "status": "pending"},
                 {"content": "Request deploy", "status": "pending"}]
        self.emit(t, "todos", todos=todos)
        spec = ctx.spec_dict()
        kind = sink_kind(spec)
        dest = {"airtable": "Airtable table", "sqlite": "SQLite table", "api": "API"}.get(kind, "form")
        self.emit(t, "subagent_start", name="repairer", task=f"Incident #{inc['id']}: {inc['type']} ({kind})")
        self._call(t, "repairer", fns, "get_incident", incident_id=inc["id"])
        d = json.loads(self._call(t, "repairer", fns, "diff_openapi"))
        patch = list(d.get("patch_hint") or [])
        base = sink_values_path(spec)
        if not is_label_keyed(spec):  # an API body: follow the renamed properties, ask nothing
            if d.get("suggested_questions"):
                raise RuntimeError("the API changed more than a rename: "
                                   + "; ".join(q["question"] for q in d["suggested_questions"]))
            values = d.get("current_sink_body") or {}
            ptr = lambda dotted: base + "".join("/" + _ptr(p) for p in dotted.split("."))  # noqa: E731
            patch = [{"op": "move", "from": ptr(old), "path": ptr(new)}
                     for old, new in (d.get("likely_renames") or {}).items() if old in values]
        for q in d.get("suggested_questions") or []:
            ans = str(self.interrupt(t, "ask_user", question=q["question"], options=q.get("options") or []).get("answer", ""))
            old = q["field"]
            if not q.get("options"):
                raise RuntimeError(f"cannot repair without a value for {old}: {ans!r}")
            if ans.startswith("None"):
                patch.append({"op": "remove", "path": f"{base}/{old}"})
            elif ans in q["options"]:
                patch.append({"op": "move", "from": f"{base}/{old}", "path": f"{base}/{ans}"})
            else:
                raise RuntimeError(f"unexpected answer {ans!r} for {old}")
        if not patch:
            raise RuntimeError("diff_openapi found nothing to patch")
        renames = ", ".join(f"{op['from'].rsplit('/', 1)[-1]} → {op['path'].rsplit('/', 1)[-1]}" for op in patch if op["op"] == "move")
        what = {"airtable": "a field", "sqlite": "a column", "api": "a body field"}.get(kind, "a question")
        self.emit(t, "message", agent="repairer", text=f"The {dest} renamed {what} ({renames or 'see patch'}). "
                                                       f"Patching {base.strip('/').replace('/', '.')}.")
        todos[0]["status"], todos[1]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        out = self._call(t, "repairer", fns, "propose_spec_patch", patch=patch, reason="follow the form's question rename")
        if not out.startswith("applied"):
            raise RuntimeError(_short(out, 300))
        out = self._call(t, "repairer", fns, "migrate_golden_examples")
        if not out.startswith("rewrote"):
            raise RuntimeError(_short(out, 300))
        self.emit(t, "subagent_end", name="repairer", summary=f"Patched {len(patch)} op(s); goldens migrated")
        todos[1]["status"], todos[2]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        self.emit(t, "subagent_start", name="builder", task="Compile the patched spec")
        compiled = self._call(t, "builder", fns, "compile_spec")
        self.emit(t, "subagent_end", name="builder", summary=_short(compiled))
        if not compiled.startswith("compiled"):
            raise RuntimeError(_short(compiled, 300))
        todos[2]["status"], todos[3]["status"] = "completed", "in_progress"
        self.emit(t, "todos", todos=todos)
        self._call(t, "supervisor", fns, "request_deploy", version=ctx.last_version["version"],
                   summary=f"Follow the tracker {dest}'s change ({renames or 'see patch'}).")
        return {"model_calls": 0, **(ctx.deploy_result or {"deployed": False, "version": ctx.last_version["version"]})}
