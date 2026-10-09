"""The caller's own business."""

from __future__ import annotations

from fastapi import APIRouter

from forge_db.models import Business

from app import schemas
from app.core.deps import CurrentUser, DbSession

router = APIRouter(prefix="/businesses", tags=["businesses"])


@router.get("/me", response_model=schemas.BusinessOut)
def get_own_business(user: CurrentUser, db: DbSession):
    return db.get(Business, user.business_id)


@router.put("/me", response_model=schemas.BusinessOut)
def update_own_business(
    payload: schemas.BusinessUpdate, user: CurrentUser, db: DbSession
):
    business = db.get(Business, user.business_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(business, field, value)
    db.commit()
    db.refresh(business)
    return business
