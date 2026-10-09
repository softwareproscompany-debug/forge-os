"""Brand kit CRUD."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from forge_db.models import BrandKit

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/brand-kits", tags=["brand-kits"])


@router.get("", response_model=schemas.Page[schemas.BrandKitOut])
def list_brand_kits(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, BrandKit, user).order_by(BrandKit.created_at.desc())
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.BrandKitOut, status_code=201)
def create_brand_kit(
    payload: schemas.BrandKitCreate, user: CurrentUser, db: DbSession
):
    kit = BrandKit(business_id=user.business_id, **payload.model_dump())
    db.add(kit)
    db.commit()
    db.refresh(kit)
    return kit


@router.get("/{kit_id}", response_model=schemas.BrandKitOut)
def get_brand_kit(kit_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, BrandKit, kit_id, user)


@router.put("/{kit_id}", response_model=schemas.BrandKitOut)
def update_brand_kit(
    kit_id: uuid.UUID,
    payload: schemas.BrandKitUpdate,
    user: CurrentUser,
    db: DbSession,
):
    kit = get_owned_or_404(db, BrandKit, kit_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(kit, field, value)
    kit.version = kit.version + 1
    db.commit()
    db.refresh(kit)
    return kit
