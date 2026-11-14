import json

import pytest
import yaml
from fastapi.testclient import TestClient

from compiler.compile import CompileError, compile_spec, load_examples
from control import ops
from demo_apps.stockroom.app import create_app
from runner.loader import load_integration
from runner.pipeline import MemoryStore, process_record
from runner.testing import FakeHttp
from spec import load_spec, parse_spec
from tests.conftest import FIXTURE_EXAMPLES, FIXTURE_SPEC


@pytest.fixture(scope="module")
def openapi_v1():
    return TestClient(create_app()).get("/openapi.json").json()


def _compile(out_root, version=1, **kw):
    kw.setdefault("examples", load_examples(FIXTURE_EXAMPLES))
    return compile_spec(load_spec(FIXTURE_SPEC), version=version, out_root=out_root, **kw)


def test_compiler_is_deterministic(tmp_path, openapi_v1):
    a = _compile(tmp_path / "a", sink_openapi=openapi_v1).path
    b = _compile(tmp_path / "b", sink_openapi=openapi_v1).path
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file() and p.name != "manifest.json")
    assert files
    for rel in files:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_versions_are_immutable(tmp_path, openapi_v1):
    _compile(tmp_path, sink_openapi=openapi_v1)
    with pytest.raises(CompileError, match="immutable"):
        _compile(tmp_path, sink_openapi=openapi_v1)


def test_missing_custom_transform_is_a_compile_error(tmp_path):
    raw = yaml.safe_load(FIXTURE_SPEC.read_text())
    raw["sink"]["body"]["quantity"] = {"transform": "custom:cases_to_units", "from": "qty"}
    with pytest.raises(CompileError, match="cases_to_units"):
        compile_spec(parse_spec(raw), version=1, out_root=tmp_path)


def test_lookup_and_custom_transforms_compile_and_run(tmp_path):
    raw = yaml.safe_load(FIXTURE_SPEC.read_text())
    raw["sink"]["body"] = {
        "sku": {"from": "sku"},
        "quantity_delta": {"transform": "custom:double", "from": "qty", "args": {"factor": 2}},
        "reference": {"template": "{po_id}/{record.id}"},
        "warehouse_id": {"transform": "lookup", "from": "record.sender_domain",
                         "args": {"table": {"acme.com": "BOS", "globex.com": "NYC", "initech.com": "BOS"}}},
    }
    raw["sink"]["verify"] = None
    del raw["sink"]["verify"]
    transforms = "def double(v, factor):\n    return v * factor\n"
    res = compile_spec(parse_spec(raw), version=1, out_root=tmp_path, transforms_src=transforms)
    integ = load_integration(res.path)
    record = {"id": "m1", "sender_domain": "globex.com", "received_at": "x",
              "body": "This is Globex purchase order PO-2001. Please receive 7 units of item WID-101 now."}
    out = process_record(integ, record, FakeHttp(), MemoryStore())
    assert out.status == "success", out.detail
    assert out.body == {"sku": "WID-101", "quantity_delta": 14, "reference": "PO-2001/m1", "warehouse_id": "NYC"}


def test_generated_tests_pass_and_record_contract_snapshot(workspace, tmp_path, openapi_v1):
    oa = tmp_path / "oa.json"
    oa.write_text(json.dumps(openapi_v1))
    out = ops.compile_and_test(FIXTURE_SPEC, openapi_path=oa)
    assert out["tests_passed"], out["tests_summary"]
    assert out["contract_errors"] == []
    vdir = workspace / "integrations" / "po_to_stockroom" / "v1"
    manifest = json.loads((vdir / "manifest.json").read_text())
    assert manifest["openapi_hash"] and manifest["examples"] == len(load_examples(FIXTURE_EXAMPLES))


def test_deploy_refuses_untested_versions(workspace, tmp_path, openapi_v1):
    oa = tmp_path / "oa.json"
    oa.write_text(json.dumps(openapi_v1))
    ops.compile_and_test(FIXTURE_SPEC, openapi_path=oa)
    store = ops.Store()
    store.set_tests("po_to_stockroom", 1, False, "forced failure")
    with pytest.raises(ops.DeployRefused):
        ops.deploy("po_to_stockroom", 1, store=store)
