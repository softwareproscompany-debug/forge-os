"""Autopilot settings (one row per business; get-or-create)."""

from __future__ import annotations

from fastapi import APIRouter

from forge_db.models import AutopilotSettings

from app import schemas
from app.core.deps import CurrentUser, DbSession

router = APIRouter(prefix="/autopilot", tags=["autopilot"])


def _get_or_create(user: CurrentUser, db: DbSession) -> AutopilotSettings:
    settings = db.get(AutopilotSettings, user.business_id)
    if settings is None:
        settings = AutopilotSettings(business_id=user.business_id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("", response_model=schemas.AutopilotOut)
def get_autopilot(user: CurrentUser, db: DbSession):
    return _get_or_create(user, db)


@router.put("", response_model=schemas.AutopilotOut)
def update_autopilot(
    payload: schemas.AutopilotUpdate, user: CurrentUser, db: DbSession
):
    settings = _get_or_create(user, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)
    db.commit()
    db.refresh(settings)
    return settings
