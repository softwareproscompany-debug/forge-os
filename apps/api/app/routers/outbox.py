"""Dev outbox (stub provider deliveries)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from forge_db.models import DevOutbox

from app import schemas
from app.core.deps import CurrentUser, DbSession, clamp_pagination, paginate, scoped

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/outbox", response_model=schemas.Page[schemas.OutboxOut])
def list_outbox(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, DevOutbox, user).order_by(DevOutbox.created_at.desc())
    return paginate(query, limit, offset)
