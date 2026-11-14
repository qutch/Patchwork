"""Wizard endpoints: draft integrations, delete an integration completely (docs/superpowers/specs/
2026-11-14-new-integration-flow-design.md). Destinations and teach-from-examples are added in later tasks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from control import destinations, drafts
from runner.store import Store

router = APIRouter(prefix="/api", tags=["drafts"])


class NewIntegration(BaseModel):
    name: str | None = None
    intent: str


def require_draft(name: str) -> drafts.Draft:
    draft = drafts.get(name) if drafts.NAME.match(name) else None
    if draft is None:
        raise HTTPException(404, f"draft {name} not found")
    return draft


@router.post("/integrations/new", status_code=201)
def new_integration(body: NewIntegration) -> dict[str, Any]:
    from agents.deep_tools import intent_slug

    name = (body.name or "").strip() or intent_slug(body.intent)
    if not name:
        raise HTTPException(422, "intent needs at least one word, or send a name")
    try:
        return drafts.create(name, body.intent).model_dump()
    except drafts.DraftExists as e:
        raise HTTPException(409, str(e)) from None
    except drafts.DraftError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/integrations/{name}/draft")
def get_draft(name: str) -> dict[str, Any]:
    return require_draft(name).model_dump()


@router.put("/integrations/{name}/destination")
def put_destination(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Check a destination (Airtable, form, API operation, SQLite) and save it on the draft. Returns the check:
    `{kind, title, fields, operations?}`. 422 says what to do next."""
    draft = require_draft(name)
    if draft.state != "draft":
        raise HTTPException(409, f"{name} is already {draft.state}: its destination can no longer change")
    try:
        dest = destinations.normalize(body)
        result = destinations.check(dest)
    except destinations.DestinationError as e:
        raise HTTPException(422, str(e)) from None
    drafts.update(name, destination=dest)
    return result


@router.get("/destinations/airtable/tables")
def airtable_tables(base_id: str, credential: str = "AIRTABLE_TOKEN") -> list[dict[str, str]]:
    """The tables of an Airtable base (the base URL or id), read with the named credential."""
    try:
        return destinations.airtable_tables(base_id, credential)
    except destinations.DestinationError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/destinations/api/operations")
def api_operations(base_url: str, credential: str | None = None) -> list[dict[str, Any]]:
    """The write operations (POST, PUT, PATCH) of an API, with their flattened body fields."""
    try:
        return destinations.api_operations(base_url, credential or None)
    except destinations.DestinationError as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/integrations/{name}")
def delete_integration(name: str, request: Request, delete_rows: bool = False) -> dict[str, Any]:
    store = Store()
    try:
        known = any(i["name"] == name for i in store.integrations())
    finally:
        store.close()
    if not known and drafts.get(name) is None:
        raise HTTPException(404, f"integration {name} not found")
    try:
        out = drafts.delete_integration(name, delete_rows=delete_rows)
    except Exception as e:  # noqa: BLE001 - say which kind of failure, never a value
        raise HTTPException(502, f"could not delete {name} completely ({type(e).__name__}); try again") from None
    request.app.state.auto.forget(name)
    return out
