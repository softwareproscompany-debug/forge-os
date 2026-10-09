"""Meeting CRUD (OS Meetings screen)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query

from forge_db.models import Meeting

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.get("", response_model=schemas.Page[schemas.MeetingOut])
def list_meetings(
    user: CurrentUser,
    db: DbSession,
    upcoming: bool | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Meeting, user).order_by(Meeting.starts_at.asc())
    if upcoming is not None:
        now = datetime.now(timezone.utc)
        if upcoming:
            query = query.filter(Meeting.starts_at >= now)
        else:
            query = query.filter(Meeting.starts_at < now)
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.MeetingOut, status_code=201)
def create_meeting(
    payload: schemas.MeetingCreate, user: CurrentUser, db: DbSession
):
    meeting = Meeting(business_id=user.business_id, **payload.model_dump())
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


@router.get("/{meeting_id}", response_model=schemas.MeetingOut)
def get_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, Meeting, meeting_id, user)


@router.put("/{meeting_id}", response_model=schemas.MeetingOut)
def update_meeting(
    meeting_id: uuid.UUID,
    payload: schemas.MeetingUpdate,
    user: CurrentUser,
    db: DbSession,
):
    meeting = get_owned_or_404(db, Meeting, meeting_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(meeting, field, value)
    db.commit()
    db.refresh(meeting)
    return meeting


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_owned_or_404(db, Meeting, meeting_id, user)
    db.delete(meeting)
    db.commit()
    return None
