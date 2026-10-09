"""Campaign CRUD, steps, launch/pause, enrollments.

Launch rule (server-side): every step that references an asset must reference
an ``approved`` asset, otherwise launch is rejected with 422.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func

from forge_db.models import (
    Asset,
    AssetStatus,
    Campaign,
    CampaignEnrollment,
    CampaignStatus,
    CampaignStep,
    Channel,
    Template,
)

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _get_campaign(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Campaign:
    return get_owned_or_404(db, Campaign, campaign_id, user)


def _get_step(campaign: Campaign, step_id: uuid.UUID, db: DbSession) -> CampaignStep:
    step = (
        db.query(CampaignStep)
        .filter(
            CampaignStep.id == step_id,
            CampaignStep.campaign_id == campaign.id,
        )
        .first()
    )
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="CampaignStep not found"
        )
    return step


def _validate_step_refs(
    db: DbSession, user: CurrentUser, payload: schemas.CampaignStepCreate
) -> None:
    if payload.template_id is not None:
        get_owned_or_404(db, Template, payload.template_id, user)
    if payload.asset_id is not None:
        get_owned_or_404(db, Asset, payload.asset_id, user)


@router.get("", response_model=schemas.Page[schemas.CampaignOut])
def list_campaigns(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Campaign, user).order_by(Campaign.created_at.desc())
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.CampaignOut, status_code=201)
def create_campaign(
    payload: schemas.CampaignCreate, user: CurrentUser, db: DbSession
):
    campaign = Campaign(
        business_id=user.business_id, created_by=user.id, **payload.model_dump()
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}", response_model=schemas.CampaignDetailOut)
def get_campaign(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession):
    campaign = _get_campaign(campaign_id, user, db)
    # Ensure steps are loaded and ordered for the detail view.
    db.refresh(campaign, attribute_names=["steps"])
    campaign.steps.sort(key=lambda s: s.position)
    return campaign


@router.put("/{campaign_id}", response_model=schemas.CampaignOut)
def update_campaign(
    campaign_id: uuid.UUID,
    payload: schemas.CampaignUpdate,
    user: CurrentUser,
    db: DbSession,
):
    campaign = _get_campaign(campaign_id, user, db)
    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates:
        updates["status"] = CampaignStatus(updates["status"])
    for field, value in updates.items():
        setattr(campaign, field, value)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/steps", response_model=schemas.Page[schemas.CampaignStepOut])
def add_steps(
    campaign_id: uuid.UUID,
    payload: schemas.CampaignStepsCreate,
    user: CurrentUser,
    db: DbSession,
):
    """Append steps; positions default to continuing after the current max."""
    campaign = _get_campaign(campaign_id, user, db)
    max_position = (
        db.query(func.coalesce(func.max(CampaignStep.position), -1))
        .filter(CampaignStep.campaign_id == campaign.id)
        .scalar()
    )
    created: list[CampaignStep] = []
    for i, item in enumerate(payload.steps):
        _validate_step_refs(db, user, item)
        step = CampaignStep(
            campaign_id=campaign.id,
            position=item.position if item.position is not None else max_position + 1 + i,
            channel=Channel(item.channel),
            template_id=item.template_id,
            asset_id=item.asset_id,
            delay_hours=item.delay_hours,
            trigger_event=item.trigger_event,
        )
        db.add(step)
        created.append(step)
    db.commit()
    for step in created:
        db.refresh(step)
    return {"items": created, "total": len(created)}


@router.put("/{campaign_id}/steps/{step_id}", response_model=schemas.CampaignStepOut)
def update_step(
    campaign_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: schemas.CampaignStepUpdate,
    user: CurrentUser,
    db: DbSession,
):
    campaign = _get_campaign(campaign_id, user, db)
    step = _get_step(campaign, step_id, db)
    updates = payload.model_dump(exclude_unset=True)
    if "channel" in updates and updates["channel"] is not None:
        updates["channel"] = Channel(updates["channel"])
    if updates.get("template_id") is not None:
        get_owned_or_404(db, Template, updates["template_id"], user)
    if updates.get("asset_id") is not None:
        get_owned_or_404(db, Asset, updates["asset_id"], user)
    for field, value in updates.items():
        setattr(step, field, value)
    db.commit()
    db.refresh(step)
    return step


def _assert_launchable(db: DbSession, campaign: Campaign) -> None:
    """422 unless every asset-bearing step references an approved asset."""
    steps = (
        db.query(CampaignStep)
        .filter(CampaignStep.campaign_id == campaign.id)
        .all()
    )
    for step in steps:
        if step.asset_id is None:
            continue
        asset = db.get(Asset, step.asset_id)
        if asset is None or asset.status is not AssetStatus.approved:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Step {step.position} references asset {step.asset_id} "
                    f"which is not approved (status="
                    f"{asset.status.value if asset else 'missing'})"
                ),
            )


@router.post("/{campaign_id}/launch", response_model=schemas.CampaignOut)
def launch_campaign(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession):
    campaign = _get_campaign(campaign_id, user, db)
    if campaign.status not in (
        CampaignStatus.draft,
        CampaignStatus.scheduled,
        CampaignStatus.paused,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot launch campaign in status {campaign.status.value}",
        )
    _assert_launchable(db, campaign)
    campaign.status = CampaignStatus.running
    db.commit()
    db.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/pause", response_model=schemas.CampaignOut)
def pause_campaign(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession):
    campaign = _get_campaign(campaign_id, user, db)
    if campaign.status is not CampaignStatus.running:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot pause campaign in status {campaign.status.value}",
        )
    campaign.status = CampaignStatus.paused
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get(
    "/{campaign_id}/enrollments",
    response_model=schemas.Page[schemas.EnrollmentOut],
)
def list_enrollments(
    campaign_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    campaign = _get_campaign(campaign_id, user, db)
    limit, offset = clamp_pagination(limit, offset)
    query = (
        db.query(CampaignEnrollment)
        .filter(CampaignEnrollment.campaign_id == campaign.id)
        .order_by(CampaignEnrollment.created_at.desc())
    )
    return paginate(query, limit, offset)
