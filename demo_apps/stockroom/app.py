"""Stockroom (:8002 live, :8003 staging): the destination.

Product list, an "Add incoming stock" form that submits via fetch to POST /api/incoming,
and a live /openapi.json. POST /admin/schema {"version": 2} switches the contract (Break 1):
v2 renames product_id -> sku and quantity -> quantity_delta, and adds a required
warehouse_id (BOS | NYC).
"""


import html
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from demo_apps.seed import CATALOG


class IncomingV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    quantity: int = Field(gt=0)
    reference: str


class Warehouse(str, Enum):
    BOS = "BOS"
    NYC = "NYC"


class IncomingV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    quantity_delta: int = Field(gt=0)
    reference: str
    warehouse_id: Warehouse


MODELS: dict[int, type[BaseModel]] = {1: IncomingV1, 2: IncomingV2}


class SchemaSwitch(BaseModel):
    version: int


def _schema_router(version: int) -> APIRouter:
    """Routes used only to render /openapi.json for a given contract version."""
    r = APIRouter()
    model = MODELS[version]

    @r.post("/api/incoming", status_code=201)
    def create_incoming(body: model) -> dict:  # type: ignore[valid-type]
        ...

    @r.get("/api/incoming")
    def list_incoming(reference: str | None = None) -> list[dict]:
        ...

    @r.get("/api/products")
    def list_products() -> list[dict]:
        ...

    @r.get("/api/products/{sku}")
    def get_product(sku: str) -> dict:
        ...

    return r


def create_app() -> FastAPI:
    app = FastAPI(title="Stockroom", version="1.0.0")
    lock = threading.Lock()
    state: dict[str, Any] = {"version": 1, "incoming": []}
    openapi_cache: dict[int, dict] = {}

    def current_openapi() -> dict:
        v = state["version"]
        if v not in openapi_cache:
            openapi_cache[v] = get_openapi(
                title="Stockroom", version=f"{v}.0.0", routes=_schema_router(v).routes
            )
        return openapi_cache[v]

    app.openapi = current_openapi  # type: ignore[method-assign]

    @app.get("/api/products", include_in_schema=False)
    def list_products() -> list[dict]:
        return [{"sku": s, "name": n} for s, n in sorted(CATALOG.items())]

    @app.get("/api/products/{sku}", include_in_schema=False)
    def get_product(sku: str) -> dict:
        if sku not in CATALOG:
            raise HTTPException(404, f"unknown product {sku}")
        return {"sku": sku, "name": CATALOG[sku]}

    @app.post("/api/incoming", include_in_schema=False)
    async def create_incoming(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"detail": "body must be JSON"}, status_code=400)
        model = MODELS[state["version"]]
        try:
            item = model.model_validate(payload)
        except ValidationError as e:
            return JSONResponse({"detail": jsonable_encoder(e.errors(include_url=False, include_context=False))},
                                status_code=422)
        data = item.model_dump(mode="json")
        sku = data.get("product_id") or data.get("sku")
        if sku not in CATALOG:
            return JSONResponse({"detail": f"unknown product {sku}"}, status_code=422)
        with lock:
            row = {"id": len(state["incoming"]) + 1, "schema_version": state["version"],
                   "created_at": datetime.now(timezone.utc).isoformat(), **data}
            state["incoming"].append(row)
        return JSONResponse(row, status_code=201)

    @app.get("/api/incoming", include_in_schema=False)
    def list_incoming(reference: str | None = None) -> list[dict]:
        rows = state["incoming"]
        return [r for r in rows if reference is None or r.get("reference") == reference]

    @app.get("/admin/schema", include_in_schema=False)
    def get_schema() -> dict:
        return {"version": state["version"]}

    @app.post("/admin/schema", include_in_schema=False)
    def switch_schema(body: SchemaSwitch) -> dict:
        if body.version not in MODELS:
            raise HTTPException(400, f"version must be one of {sorted(MODELS)}")
        state["version"] = body.version
        return {"version": state["version"]}

    @app.post("/admin/reset", include_in_schema=False)
    def reset() -> dict:
        with lock:
            state["incoming"] = []
            state["version"] = 1
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home() -> str:
        rows = "".join(
            f"<tr><td>{html.escape(r['reference'])}</td><td>{html.escape(r.get('product_id') or r.get('sku'))}</td>"
            f"<td>{r.get('quantity') or r.get('quantity_delta')}</td><td>{r.get('warehouse_id', '')}</td></tr>"
            for r in reversed(state["incoming"][-50:])
        )
        return _page("Stockroom", (
            f"<p>Contract v{state['version']} &middot; <a href='/incoming/new'>Add incoming stock</a></p>"
            f"<h3>Recent incoming ({len(state['incoming'])} total)</h3>"
            f"<table><tr><th>Reference</th><th>Product</th><th>Qty</th><th>Warehouse</th></tr>{rows}</table>"
        ))

    @app.get("/incoming/new", response_class=HTMLResponse, include_in_schema=False)
    def new_incoming() -> str:
        v2 = state["version"] == 2
        prod, qty = ("sku", "quantity_delta") if v2 else ("product_id", "quantity")
        options = "".join(f"<option value='{s}'>{s} &mdash; {html.escape(n)}</option>" for s, n in sorted(CATALOG.items()))
        wh = ("<label>Warehouse <select name='warehouse_id' data-testid='warehouse'>"
              "<option>BOS</option><option>NYC</option></select></label>") if v2 else ""
        form = f"""
<p><a href="/">&larr; Stockroom</a></p><h2>Add incoming stock</h2>
<form id="f">
  <label>Product <select name="{prod}" data-testid="product">{options}</select></label>
  <label>Quantity <input name="{qty}" type="number" min="1" data-testid="quantity"></label>
  <label>Reference <input name="reference" data-testid="reference"></label>
  {wh}
  <button type="submit">Save</button>
</form><p id="status"></p>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  body['{qty}'] = parseInt(body['{qty}'], 10);
  const r = await fetch('/api/incoming', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                                          body: JSON.stringify(body)}});
  document.getElementById('status').textContent = r.ok ? 'Saved' : 'Error ' + r.status;
}});
</script>"""
        return _page("Add incoming stock", form)

    return app


def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui;margin:2rem;max-width:60rem}td,th{padding:.3rem .6rem;"
        "text-align:left;border-bottom:1px solid #ddd}label{display:block;margin:.6rem 0}</style></head>"
        f"<body>{body}</body></html>"
    )


app = create_app()
