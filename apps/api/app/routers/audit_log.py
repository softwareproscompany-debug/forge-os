"""Immutable audit log — admin-only read.

``GET /api/v1/audit-log`` lists audit rows newest-first with optional
filters. The table is append-only (Postgres trigger blocks UPDATE/DELETE);
there are deliberately no mutation endpoints here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from forge_db.models import AuditLog, User

from app import schemas
from app.core.deps import (
    DbSession,
    clamp_pagination,
    paginate,
    require_role,
)

router = APIRouter(prefix="/audit-log", tags=["audit-log"])

AdminUser = Annotated[User, Depends(require_role("owner", "admin"))]


@router.get("", response_model=schemas.Page[schemas.AuditLogOut])
def list_audit_log(
    admin: AdminUser,
    db: DbSession,
    business_id: Optional[uuid.UUID] = Query(default=None),
    action: Optional[str] = Query(default=None, max_length=128),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    """List audit rows, newest first. Admin/owner only, all businesses."""
    limit, offset = clamp_pagination(limit, offset)
    query = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if business_id is not None:
        query = query.filter(AuditLog.business_id == business_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if since is not None:
        query = query.filter(AuditLog.created_at >= since)
    if until is not None:
        query = query.filter(AuditLog.created_at <= until)
    return paginate(query, limit, offset)
