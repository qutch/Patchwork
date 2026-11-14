"""Assemble the walkthrough page: build.py <repo> <data.json> <out.html>"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = Path(sys.argv[1])
data = json.loads(Path(sys.argv[2]).read_text())
spec = data["spec_yaml"].splitlines()
code = data["code"]
pipeline = (REPO / "runner" / "pipeline.py").read_text().splitlines()


def yaml_block(lines, key_line_startswith, include_children=True):
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(key_line_startswith):
            indent = len(ln) - len(ln.lstrip())
            out = [ln]
            if include_children:
                for nxt in lines[i + 1:]:
                    if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent and not nxt.lstrip().startswith("- ") or \
                       (nxt.strip() and (len(nxt) - len(nxt.lstrip())) < indent):
                        break
                    out.append(nxt)
            return "\n".join(x for x in out).rstrip()
    raise KeyError(key_line_startswith)


def parser_block(pid):
    for i, ln in enumerate(spec):
        if ln.strip() == f"- id: {pid}":
            out = [ln]
            for nxt in spec[i + 1:]:
                if nxt.strip().startswith("- id:") or (nxt.strip() and len(nxt) - len(nxt.lstrip()) < 4):
                    break
                out.append(nxt)
            return "\n".join(out).rstrip()
    raise KeyError(pid)


def py_block(src_lines, starts, until_blank_pair=True):
    for i, ln in enumerate(src_lines):
        if ln.startswith(starts):
            out = [ln]
            for nxt in src_lines[i + 1:]:
                if nxt and not nxt.startswith((" ", "\t", ")", "]", "}")):
                    break
                out.append(nxt)
            while out and not out[-1].strip():
                out.pop()
            return "\n".join(out)
    raise KeyError(starts)


def lines_between(src_lines, start_contains, end_contains):
    s = next(i for i, l in enumerate(src_lines) if start_contains in l)
    e = next(i for i, l in enumerate(src_lines) if i > s and end_contains in l)
    return "\n".join(src_lines[s:e + 1])


P = code["parsers.py"].splitlines()
I = code["integration.py"].splitlines()
A = code["adapters.py"].splitlines()

snippets = {
    "route": {
        "spec": "extract:\n  route_by: sender_domain\n  parsers:\n" + "\n".join(
            f"    - id: {p}\n      match: {{sender_domain: {d}}}" for p, d in
            [("acme_v1", "acme.com"), ("globex_v1", "globex.com"), ("initech_v1", "initech.com")]),
        "code": py_block(P, "ROUTES = [") + "\n\n\n" + py_block(P, "def route("),
        "file": "parsers.py",
    },
    "extract": {
        "spec": {pid: parser_block(pid) for pid in ("acme_v1", "globex_v1", "initech_v1")},
        "code": {pid: "\n".join(l for l in P if l.startswith(f"_{pid.upper()}__")) + "\n\n\n" +
                 py_block(P, f"def parse_{pid}(") for pid in ("acme_v1", "globex_v1", "initech_v1")},
        "file": "parsers.py",
    },
    "validate": {"spec": yaml_block(spec, "validate:"), "code": py_block(I, "def validate("), "file": "integration.py"},
    "dedupe": {
        "spec": yaml_block(spec, "dedupe:"),
        "code": py_block(I, "def dedupe_key(") + "\n\n# runner/pipeline.py\n" +
                lines_between(pipeline, "out.key = integ.dedupe_key", "return out"),
        "file": "integration.py + runner/pipeline.py",
    },
    "map": {"spec": yaml_block(spec, "body:"), "code": py_block(I, "def build_body("), "file": "integration.py"},
    "send": {
        "spec": yaml_block(spec, "call:", include_children=False),
        "code": py_block(A, "def send(") + "\n\n# runner/pipeline.py\n" + py_block(pipeline, "def _sink_status("),
        "file": "adapters.py + runner/pipeline.py",
    },
    "verify": {
        "spec": yaml_block(spec, "verify:", include_children=False),
        "code": py_block(A, "VERIFY = ") + "\n\n\n" + py_block(A, "def verify("),
        "file": "adapters.py",
    },
}
data["snippets"] = snippets
payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
html = (HERE / "page.template.html").read_text().replace("__DATA__", payload)
out = Path(sys.argv[3])
out.write_text(html)
print("wrote", out, f"{len(html) / 1024:.0f} KB")
