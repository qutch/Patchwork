"""The v2 stage-fallback storylines for ScriptedAgentService: LEARN, TEACH (page -> form) and REPAIR of a
renamed form question. No model. They emit the deep agent's event types and call the *same* tool functions
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
        evidenced, corroborated and unambiguous on this page. None if a field has no such candidate."""
        from agents.deep_tools import site_parser_id
        parsers = (spec.get("extract") or {}).get("parsers") or []
        wanted = [f for f in (parsers[0]["fields"] if parsers else {}) if all(f in p["fields"] for p in parsers)]
        html_field = (spec.get("source") or {}).get("html_field") or "html"
        fields = {}
        for name in wanted:
            for cand in CANDIDATES.get(name) or generic_candidates(name, spec, rec):
                probe = {"id": "probe", "fields": {name: cand}}
                out = learn.run_page_parser(probe, rec, html_field)
                if "error" in out:
                    continue
                ev = learn.evidence(rec, out, spec.get("validate") or [], parser=probe, html_field=html_field)
                if ev[0]["ok"] and learn.corroborated(rec, out[name], html_field) and not learn.ambiguity(rec, probe, html_field):
                    fields[name] = cand
                    break
            else:
                return None
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

    # ---- REPAIR (form question renamed) --------------------------------------------------------
    @staticmethod
    def _is_form_integration(name: str) -> bool:
        import yaml

        from runner.store import Store, workspace_dir
        active = Store().active_version(name)
        path = workspace_dir() / "integrations" / name / f"v{active}" / "spec.yaml"
        if not active or not path.exists():
            return False
        from agents.deep_tools import is_label_keyed
        return is_label_keyed(yaml.safe_load(path.read_text()) or {})  # form or Airtable (V14): same repair

    def _repair_form(self, t: _Thread, inc: dict) -> Any:
        from agents.deep_tools import ToolContext, seed_repair_workdir, tool_functions
        name = inc["integration"]
        seed_repair_workdir(name)
        ctx = ToolContext(self, t, name=name, incident_id=inc["id"])
        fns = tool_functions(ctx)
        todos = [{"content": "Diagnose the form change", "status": "in_progress"},
                 {"content": "Patch the spec and migrate goldens", "status": "pending"},
                 {"content": "Compile and test the new version", "status": "pending"},
                 {"content": "Request deploy", "status": "pending"}]
        self.emit(t, "todos", todos=todos)
        kind = (ctx.spec_dict().get("sink") or {}).get("kind", "form")
        dest = "Airtable table" if kind == "airtable" else "form"
        self.emit(t, "subagent_start", name="repairer", task=f"Incident #{inc['id']}: {inc['type']} ({kind})")
        self._call(t, "repairer", fns, "get_incident", incident_id=inc["id"])
        d = json.loads(self._call(t, "repairer", fns, "diff_openapi"))
        patch = list(d.get("patch_hint") or [])
        base = "/sink/fields"
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
        what = "a field" if kind == "airtable" else "a question"
        self.emit(t, "message", agent="repairer", text=f"The {dest} renamed {what} ({renames or 'see patch'}). Patching sink.fields.")
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
