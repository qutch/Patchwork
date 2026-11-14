"""Fake papers API: an offline OpenAPI destination with a nested request body, for the wizard's API path
(TEACH from example pages into one write operation; REPAIR through the OpenAPI diff).

    POST /papers        {"title": str, "year": int, "venue": {"name": str}, "authors": [str]} -> 201 the paper + id
    GET  /papers        every paper written, oldest first
    GET  /openapi.json  FastAPI's document (the body schema refers to Venue by $ref)
    POST /admin/reset   forget every paper

`title`, `year` and `venue` (with `venue.name`) are required; `authors` is optional. Unknown keys are refused
(422), like a strict real API, so a wrong body is never stored silently.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field


class Venue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)


class PaperIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    year: int = Field(ge=1000, le=9999)
    venue: Venue
    authors: list[str] = Field(default_factory=list)


def create_app() -> FastAPI:
    app = FastAPI(title="Fake papers API")
    lock = threading.Lock()
    ids = itertools.count(1)
    papers: list[dict[str, Any]] = []

    @app.post("/papers", status_code=201, summary="Add a paper")
    def add_paper(body: PaperIn) -> dict[str, Any]:
        with lock:
            row = {"id": next(ids), **body.model_dump()}
            papers.append(row)
        return row

    @app.get("/papers", summary="List papers")
    def list_papers() -> list[dict[str, Any]]:
        with lock:
            return list(papers)

    @app.post("/admin/reset", include_in_schema=False)
    def reset() -> dict[str, bool]:
        with lock:
            papers.clear()
        return {"ok": True}

    return app
