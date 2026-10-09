"""Shared helpers for the stub (dev-outbox) channel providers."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - avoids a runtime circular import
    from forge_channels import SendRequest


def stub_message_id(channel: str, to: str, subject: str | None, body: str) -> str:
    """Deterministic message id: identical sends produce identical ids."""
    digest = hashlib.sha256(
        f"{channel}|{to}|{subject or ''}|{body}".encode("utf-8")
    ).hexdigest()
    return f"stub-{digest[:16]}"


def stub_context(req: "SendRequest") -> tuple[Any, Any]:
    """Extract the (db, business_id) stub providers need from request metadata.

    Raises:
        ValueError: if the caller didn't supply both in ``req.metadata``.
    """
    db = req.metadata.get("db")
    business_id = req.metadata.get("business_id")
    if db is None or business_id is None:
        raise ValueError(
            "Stub channel providers require metadata['db'] (a sync session) and "
            "metadata['business_id'] so the message can be recorded in dev_outbox. "
            f"Got db={db!r}, business_id={business_id!r}."
        )
    return db, business_id
