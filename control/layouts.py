"""Revert a layout (docs/contracts.md V9, D2's one-click revert): the active spec minus one parser →
`compile_and_test` → `deploy`. No AI. Golden examples that the removed parser handled are dropped
with it (they would otherwise expect a reader that no longer exists).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from control import ops
from runner.html import fingerprint_present
from runner.store import Store


class LayoutError(Exception):
    """`status` is the HTTP status the API answers with (404 unknown, 409 refused)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _route(parsers: list[dict[str, Any]], record: dict[str, Any], html_field: str | None) -> dict | None:
    """V4 route order: first non-empty `match` equal to the record, else first `fingerprint` present
    in its HTML, else the first catch-all (no match, no fingerprint)."""
    for p in parsers:
        m = p.get("match") or {}
        if m and all(record.get(k) == v for k, v in m.items()):
            return p
    html = record.get(html_field) if html_field else None
    if isinstance(html, str):
        for p in parsers:
            if p.get("fingerprint") and fingerprint_present(html, p["fingerprint"]):
                return p
    return next((p for p in parsers if not p.get("match") and not p.get("fingerprint")), None)


def revert_parser(name: str, parser_id: str, *, store: Store | None = None) -> dict[str, Any]:
    """Compile the active spec without `parser_id`, run its tests, deploy it.
    Returns {name, version, deployed: True, previous_version, removed_parser, dropped_examples}."""
    store = store or Store()
    active = store.active_version(name)
    if active is None:
        raise LayoutError(f"{name} has no active version", 404 if not store.versions(name) else 409)
    vdir = ops.version_dir(name, active)
    spec = yaml.safe_load((vdir / "spec.yaml").read_text())
    parsers = list((spec.get("extract") or {}).get("parsers") or [])
    target = next((p for p in parsers if p.get("id") == parser_id), None)
    if target is None:
        raise LayoutError(f"{name} v{active} has no parser {parser_id}", 404)
    if len(parsers) == 1:
        raise LayoutError(f"{parser_id} is the last parser of {name}; reverting it would leave nothing to read pages with")
    others = [p for p in parsers if p is not target]
    spec["extract"]["parsers"] = others

    golden = json.loads((vdir / "golden.json").read_text()) if (vdir / "golden.json").exists() else []
    html_field = (spec.get("source") or {}).get("html_field")
    keep = [ex for ex in golden if _route(parsers, ex.get("record") or {}, html_field) is not target]
    dropped = [ex.get("name") for ex in golden if ex not in keep]

    with tempfile.TemporaryDirectory(prefix="patchwork-revert-") as tmp:
        work = Path(tmp)
        (work / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (work / "examples.yaml").write_text(yaml.safe_dump(keep, sort_keys=False))
        if (vdir / "transforms.py").exists():
            shutil.copy(vdir / "transforms.py", work / "transforms.py")
        openapi = vdir / "contract" / "openapi.json"
        try:
            built = ops.compile_and_test(work / "spec.yaml", examples_path=work / "examples.yaml",
                                         transforms_path=work / "transforms.py",
                                         openapi_path=openapi if openapi.exists() else None, store=store)
        except Exception as e:  # noqa: BLE001 - spec/compile errors are a refusal, not a crash
            raise LayoutError(f"could not compile {name} without {parser_id}: {type(e).__name__}: {e}") from None
    if not built["tests_passed"]:
        raise LayoutError(f"{name} v{built['version']} without {parser_id}: tests failed, not deployed.\n"
                          f"{built['tests_summary']}")
    out = ops.deploy(name, built["version"], store=store)
    return {"name": name, "version": built["version"], "deployed": True, "previous_version": out["previous_version"],
            "removed_parser": parser_id, "dropped_examples": dropped}
