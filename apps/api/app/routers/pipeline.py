"""Opportunity / pipeline CRUD (OS Pipeline screen)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from forge_db.models import Opportunity

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("", response_model=schemas.Page[schemas.OpportunityOut])
def list_opportunities(
    user: CurrentUser,
    db: DbSession,
    stage: str | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Opportunity, user).order_by(Opportunity.created_at.desc())
    if stage:
        query = query.filter(Opportunity.stage == stage)
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.OpportunityOut, status_code=201)
def create_opportunity(
    payload: schemas.OpportunityCreate, user: CurrentUser, db: DbSession
):
    opp = Opportunity(business_id=user.business_id, **payload.model_dump())
    db.add(opp)
    db.commit()
    db.refresh(opp)
    return opp


@router.get("/{opportunity_id}", response_model=schemas.OpportunityOut)
def get_opportunity(opportunity_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, Opportunity, opportunity_id, user)


@router.put("/{opportunity_id}", response_model=schemas.OpportunityOut)
def update_opportunity(
    opportunity_id: uuid.UUID,
    payload: schemas.OpportunityUpdate,
    user: CurrentUser,
    db: DbSession,
):
    opp = get_owned_or_404(db, Opportunity, opportunity_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(opp, field, value)
    db.commit()
    db.refresh(opp)
    return opp


@router.delete("/{opportunity_id}", status_code=204)
def delete_opportunity(
    opportunity_id: uuid.UUID, user: CurrentUser, db: DbSession
):
    opp = get_owned_or_404(db, Opportunity, opportunity_id, user)
    db.delete(opp)
    db.commit()
    return None
