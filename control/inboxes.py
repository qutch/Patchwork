"""Which inbox a record id belongs to: `pg-…` is a page (control.pages.PageInbox), `doc-…` a document
(control.documents.DocumentInbox). Both are path-based, so building one per call is cheap and always sees the
current workspace."""

from __future__ import annotations

from control.documents import DocumentInbox
from control.pages import PageInbox


def inbox_for(record_id: str) -> PageInbox | DocumentInbox:
    return DocumentInbox() if str(record_id).startswith("doc-") else PageInbox()
