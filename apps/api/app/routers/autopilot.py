"""Autopilot settings (one row per business; get-or-create) plus the Card 4
weekly content-plan review flow:

* ``GET /autopilot/plan`` — the business's latest content plan (404 if none).
* ``POST /autopilot/plan/approve`` — approve a draft plan, materializing it
  into a scheduled autopilot campaign (one step per plan item,
  ``delay_hours = day * 24``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, status

from forge_db.models import (
    Asset,
    AssetStatus,
    AutopilotSettings,
    Business,
    Campaign,
    CampaignStatus,
    CampaignStep,
    Channel,
    ContentPlan,
    PlanStatus,
)

from app import schemas
from app.core.deps import CurrentUser, DbSession, get_owned_or_404, scoped

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


# ---------------------------------------------------------------------------
# Card 4: weekly content-plan review
# ---------------------------------------------------------------------------


def _monday_9am(week_start: date, tz_name: str) -> datetime:
    """Monday 09:00 local time as an aware datetime (UTC fallback)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime(
        week_start.year, week_start.month, week_start.day, 9, 0, tzinfo=tz
    )


def _resolve_step_asset_id(
    db: DbSession, user: CurrentUser, raw: object
) -> uuid.UUID | None:
    """Re-validate a plan item's asset reference at approve time.

    Returns the asset id only when it exists, is owned by this business,
    and is ``approved`` — anything else (missing, unapproved, wrong tenant,
    malformed) degrades to ``None`` rather than failing the whole approve.
    """
    if raw is None:
        return None
    try:
        asset_id = uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
    asset = scoped(db, Asset, user).filter(Asset.id == asset_id).first()
    if asset is None or asset.status is not AssetStatus.approved:
        return None
    return asset.id


@router.get("/plan", response_model=schemas.ContentPlanOut)
def get_plan(user: CurrentUser, db: DbSession):
    """The business's latest content plan (by ``week_start`` desc), 404 if none."""
    plan = (
        scoped(db, ContentPlan, user)
        .order_by(ContentPlan.week_start.desc(), ContentPlan.created_at.desc())
        .first()
    )
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No content plan has been drafted for this business yet.",
        )
    return plan


@router.post("/plan/approve", response_model=schemas.PlanApproveOut)
def approve_plan(
    payload: schemas.PlanApproveIn, user: CurrentUser, db: DbSession
):
    """Approve a draft plan, materializing it into a scheduled campaign.

    422 unless the plan is still a draft. Asset references are re-validated
    (owned + approved) per item; invalid ones become null steps.
    """
    plan = get_owned_or_404(db, ContentPlan, payload.plan_id, user)
    if plan.status is not PlanStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Only draft plans can be approved (status={plan.status.value})",
        )

    business = db.get(Business, user.business_id)
    tz_name = business.timezone if business and business.timezone else "UTC"

    campaign = Campaign(
        business_id=user.business_id,
        name=f"Autopilot — week of {plan.week_start.isoformat()}",
        status=CampaignStatus.scheduled,
        autopilot=True,
        created_by=user.id,
        starts_at=_monday_9am(plan.week_start, tz_name),
        timezone=tz_name,
    )
    db.add(campaign)
    db.flush()  # assign campaign.id before creating steps

    items: list = plan.items or []
    for position, item in enumerate(items):
        channel = Channel(str(item.get("channel", "email")))
        day = int(item.get("day", 0))
        db.add(
            CampaignStep(
                campaign_id=campaign.id,
                position=position,
                channel=channel,
                asset_id=_resolve_step_asset_id(db, user, item.get("asset_id")),
                delay_hours=day * 24,
                trigger_event=None,
            )
        )

    plan.status = PlanStatus.approved
    plan.approved_by = user.id
    plan.approved_at = datetime.now(timezone.utc)
    plan.campaign_id = campaign.id

    db.commit()
    db.refresh(plan)
    return {"plan": plan, "campaign_id": campaign.id}
