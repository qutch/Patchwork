"""Fake papers API: an offline OpenAPI destination with a nested request body, for the wizard's API path
(TEACH from example pages into one write operation; REPAIR through the OpenAPI diff).

    POST /papers        {"title": str, "year": int, "venue": {"name": str}, "authors": [str]} -> 201 the paper + id
    GET  /papers        every paper written, oldest first
    GET  /openapi.json  FastAPI's document (the body schema refers to Venue by $ref)
    POST /admin/rename  {"from": "title", "to": "paper_title"}: the API renames a top-level body field (the
                        document and POST /papers follow; the old name is refused from then on)
    POST /admin/reset   forget every paper and every rename

`title`, `year` and `venue` (with `venue.name`) are required; `authors` is optional. Unknown keys are refused
(422), like a strict real API, so a wrong body is never stored silently.
"""

from __future__ import annotations

import copy
import itertools
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Venue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)


class PaperIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    year: int = Field(ge=1000, le=9999)
    venue: Venue
    authors: list[str] = Field(default_factory=list)


class Rename(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str = Field(min_length=1)


BODY = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/PaperIn"}}}, "required": True}


def _schemas() -> dict[str, Any]:
    """PaperIn and Venue as OpenAPI components (what FastAPI writes for a `body: PaperIn` parameter)."""
    doc = PaperIn.model_json_schema(ref_template="#/components/schemas/{model}")
    defs = doc.pop("$defs", {})
    return {"PaperIn": doc, **defs}


def create_app() -> FastAPI:
    app = FastAPI(title="Fake papers API")
    lock = threading.Lock()
    ids = itertools.count(1)
    papers: list[dict[str, Any]] = []
    renamed: dict[str, str] = {}  # original field -> its current name

    def current(original: str) -> str:
        return renamed.get(original, original)

    @app.post("/papers", status_code=201, summary="Add a paper", openapi_extra={"requestBody": BODY})
    async def add_paper(request: Request) -> Any:
        try:
            raw = await request.json()
        except ValueError:
            return JSONResponse({"detail": "the body is not JSON"}, status_code=422)
        if not isinstance(raw, dict):
            return JSONResponse({"detail": "the body must be a JSON object"}, status_code=422)
        with lock:
            back = {new: old for old, new in renamed.items()}
            stale = [k for k in raw if k in renamed and k not in back]
            if stale:
                return JSONResponse({"detail": [{"type": "extra_forbidden", "loc": ["body", k],
                                                 "msg": "Extra inputs are not permitted"} for k in stale]},
                                    status_code=422)
            try:
                body = PaperIn.model_validate({back.get(k, k): v for k, v in raw.items()})
            except ValidationError as e:
                errs = [{**{k: v for k, v in err.items() if k in ("type", "msg")},
                         "loc": ["body", *[current(str(p)) if i == 0 else p for i, p in enumerate(err["loc"])]]}
                        for err in e.errors()]
                return JSONResponse({"detail": errs}, status_code=422)
            row = {"id": next(ids), **{current(k): v for k, v in body.model_dump().items()}}
            papers.append(row)
        return JSONResponse(row, status_code=201)

    @app.get("/papers", summary="List papers")
    def list_papers() -> list[dict[str, Any]]:
        with lock:
            return copy.deepcopy(papers)

    @app.post("/admin/rename", include_in_schema=False)
    def rename(body: Rename) -> dict[str, Any]:
        with lock:
            original = next((old for old, new in renamed.items() if new == body.from_), body.from_)
            if original not in PaperIn.model_fields or current(original) != body.from_:
                raise HTTPException(404, f"no top-level body field named {body.from_!r}")
            renamed[original] = body.to
            return {"fields": [current(k) for k in PaperIn.model_fields]}

    @app.post("/admin/reset", include_in_schema=False)
    def reset() -> dict[str, bool]:
        with lock:
            papers.clear()
            renamed.clear()
        return {"ok": True}

    def openapi() -> dict[str, Any]:
        doc = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schemas = doc.setdefault("components", {}).setdefault("schemas", {})
        schemas.update(_schemas())
        with lock:
            pairs = dict(renamed)
        paper = schemas["PaperIn"]
        paper["properties"] = {pairs.get(k, k): v for k, v in paper["properties"].items()}
        paper["required"] = [pairs.get(k, k) for k in paper.get("required", [])]
        return doc

    app.openapi = openapi  # type: ignore[method-assign]  # rebuilt on every request: renames show at once
    return app
