"""Immutable audit log writer.

Appends rows to ``audit_log`` (migration 0017). The table is append-only:
a Postgres trigger rejects UPDATE/DELETE, and this module only ever
INSERTs. Best-effort by design — a logging failure must never break the
action being logged, so errors are swallowed after a rollback (mirrors
``write_audit_row`` in ``app/draven_tools.py``).
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from forge_db.models import AuditLog

__all__ = ["log_action"]


def log_action(
    db: Session,
    *,
    action: str,
    actor_type: str = "user",
    actor_id: Optional[str] = None,
    actor_email: Optional[str] = None,
    business_id: Optional[uuid.UUID] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Insert one audit row. Never raises.

    ``action`` is a dotted verb like ``auth.login`` or ``campaign.launch``.
    ``actor_type`` is one of ``user`` | ``system`` | ``api_key``.
    """
    try:
        db.add(
            AuditLog(
                actor_type=actor_type,
                actor_id=actor_id,
                actor_email=actor_email,
                business_id=business_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
