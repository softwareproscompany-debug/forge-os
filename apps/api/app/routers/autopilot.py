"""Autopilot settings (one row per business; get-or-create) plus the Card 4
weekly content-plan review flow:

* ``GET /autopilot/plan`` — the business's latest content plan (404 if none).
* ``POST /autopilot/plan/approve`` — approve a draft plan, materializing it
  into a **running** autopilot campaign. Approval is the launch gate: the
  week runs itself from here (previously the campaign was created
  ``scheduled`` and nothing ever launched it — ``campaign_tick`` only
  processes ``running`` campaigns).
* ``POST /autopilot/plan/run-now`` — owner/admin: draft this week's plan
  immediately via the worker (ignores the configured schedule, still
  idempotent per week).
* ``PUT /autopilot`` — also carries the planner schedule (``plan_day``,
  ``plan_hour``, ``plan_cadence``); the hourly worker cron gates per
  business on these instead of a fixed Monday 06:00.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status

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
    User,
)

from app import schemas
from app.core.deps import (
    CurrentSettings,
    CurrentUser,
    DbSession,
    get_owned_or_404,
    require_role,
    scoped,
)
from app.core.queue import enqueue_and_await

router = APIRouter(prefix="/autopilot", tags=["autopilot"])

AdminUser = Annotated[User, Depends(require_role("owner", "admin"))]

#: How long ``POST /autopilot/plan/run-now`` waits for the worker to draft
#: the plan before giving up (the draft is one fast job: stub LLM or a
#: single live planning call).
_RUN_NOW_TIMEOUT_S = 60.0


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


def _day_9am(week_start: date, tz_name: str, day_offset: int) -> datetime:
    """``week_start`` + ``day_offset`` days at 09:00 local (UTC fallback)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    day = week_start + timedelta(days=day_offset)
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=tz)


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
    """Approve a draft plan, materializing it into a running campaign.

    422 unless the plan is still a draft. Asset references are re-validated
    (owned + approved) per item; invalid ones become null steps. The
    campaign starts ``running`` immediately — the human approval is the
    launch gate, so the week runs itself without a manual launch step.

    Timing: items run in day order; the campaign's ``starts_at`` is the
    first item's day at 09:00 business-local and each step's
    ``delay_hours`` is the gap from the *previous* step's send
    (``(day[i] - day[i-1]) * 24``), so sends land on the days the plan
    promises (previously every step used ``day * 24`` against a Monday
    start, so the first send always went out a day early and the rest
    drifted).
    """
    plan = get_owned_or_404(db, ContentPlan, payload.plan_id, user)
    if plan.status is not PlanStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Only draft plans can be approved (status={plan.status.value})",
        )

    business = db.get(Business, user.business_id)
    tz_name = business.timezone if business and business.timezone else "UTC"

    items: list = plan.items or []
    # Day order: the tick advances steps with inter-step delays, so steps
    # must be ordered by send day for the delays to land on the right days.
    ordered = sorted(items, key=lambda it: int(it.get("day", 0)))
    first_day = int(ordered[0].get("day", 0)) if ordered else 0

    campaign = Campaign(
        business_id=user.business_id,
        name=f"Autopilot — week of {plan.week_start.isoformat()}",
        status=CampaignStatus.running,
        autopilot=True,
        created_by=user.id,
        starts_at=_day_9am(plan.week_start, tz_name, first_day),
        timezone=tz_name,
    )
    db.add(campaign)
    db.flush()  # assign campaign.id before creating steps

    prev_day = first_day
    for position, item in enumerate(ordered):
        channel = Channel(str(item.get("channel", "email")))
        day = int(item.get("day", 0))
        db.add(
            CampaignStep(
                campaign_id=campaign.id,
                position=position,
                channel=channel,
                asset_id=_resolve_step_asset_id(db, user, item.get("asset_id")),
                # Inter-step gap, not an absolute week offset.
                delay_hours=max(day - prev_day, 0) * 24,
                trigger_event=None,
            )
        )
        prev_day = day

    plan.status = PlanStatus.approved
    plan.approved_by = user.id
    plan.approved_at = datetime.now(timezone.utc)
    plan.campaign_id = campaign.id

    db.commit()
    db.refresh(plan)
    return {"plan": plan, "campaign_id": campaign.id}


@router.post("/plan/run-now", response_model=schemas.PlanRunNowOut)
async def run_plan_now(
    user: AdminUser, db: DbSession, settings: CurrentSettings
):
    """Draft this business's content plan immediately (owner/admin only).

    Enqueues the worker's ``autopilot_plan_now`` job and waits for the
    draft (up to 60s). The manual trigger ignores the configured schedule
    but is still idempotent: when a draft/approved plan already exists for
    this week, it is returned with ``created=false`` instead of drafting a
    duplicate.

    503 when the worker queue is unreachable (Redis down / worker not
    running), 504 when the job does not finish in time, 502 when the job
    itself failed — all with an actionable ``detail``.
    """
    # Ensure the settings row exists so the worker can stamp
    # ``last_planned_at`` (biweekly gating) on the draft.
    _get_or_create(user, db)

    try:
        job_id, result = await enqueue_and_await(
            settings.REDIS_URL,
            "autopilot_plan_now",
            str(user.business_id),
            str(user.id),
            timeout=_RUN_NOW_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - surface the job's own error
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Planner job failed: {exc}",
        ) from exc
    if job_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Worker queue unreachable (Redis): the planner draft could "
                "not be queued. Check that Redis and the worker are running."
            ),
        )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                "Planner job did not finish within 60s. Check the worker "
                "logs, then GET /autopilot/plan to see if the draft landed."
            ),
        )
    if not isinstance(result, dict) or not result.get("ok"):
        detail = (
            result.get("error")
            if isinstance(result, dict)
            else "Planner job failed."
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail
        )
    plan = get_owned_or_404(
        db, ContentPlan, uuid.UUID(str(result["plan_id"])), user
    )
    return {"plan": plan, "created": bool(result.get("created", False))}
