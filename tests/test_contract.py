from fastapi.testclient import TestClient

from demo_apps.stockroom.app import create_app
from runner.contract import openapi_hash, validate_body

V1_BODY = {"product_id": "WID-101", "quantity": 4, "reference": "PO-1001"}
V2_BODY = {"sku": "WID-101", "quantity_delta": 4, "reference": "PO-1001", "warehouse_id": "BOS"}


def _openapi(version: int) -> dict:
    c = TestClient(create_app())
    c.post("/admin/schema", json={"version": version})
    return c.get("/openapi.json").json()


def test_v1_body_matches_v1_contract():
    assert validate_body(_openapi(1), "POST", "/api/incoming", V1_BODY) == []


def test_v2_contract_change_is_detected_with_actionable_errors():
    v1, v2 = _openapi(1), _openapi(2)
    assert openapi_hash(v1) != openapi_hash(v2)
    errors = validate_body(v2, "POST", "/api/incoming", V1_BODY)
    joined = "\n".join(errors)
    for needle in ("body.sku: required", "body.quantity_delta: required", "body.warehouse_id: required",
                   "body.product_id: not accepted", "body.quantity: not accepted"):
        assert needle in joined, joined
    assert validate_body(v2, "POST", "/api/incoming", V2_BODY) == []


def test_enum_and_minimum_are_checked():
    v2 = _openapi(2)
    errors = validate_body(v2, "POST", "/api/incoming", {**V2_BODY, "warehouse_id": "SFO", "quantity_delta": 0})
    assert any("enum" in e for e in errors)
    assert any("must be > 0" in e for e in errors)


def test_unknown_operation_is_an_error():
    assert validate_body(_openapi(1), "PUT", "/api/nope", {}) != []
