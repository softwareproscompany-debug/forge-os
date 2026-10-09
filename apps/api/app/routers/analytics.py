"""Analytics: overview aggregates + per-campaign funnel.

Rate definitions (documented, stable for the web workstream):

* ``sent``      — sends created in the window (any status)
* ``delivered`` — sends with ``status = 'delivered'``
* ``opened``    — ``opened_at`` is not null
* ``clicked``   — ``clicked_at`` is not null
* ``converted`` — ``converted_at`` is not null
* ``open_rate`` / ``ctr`` / ``conversion_rate`` — divided by ``delivered``
  (0.0 when nothing was delivered)
* ``spend_usd`` — sum of ``generation_logs.cost_usd`` in the window
  (LLM generation spend for the business)
* ``by_day``    — per-calendar-day buckets of the same counters
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func

from forge_db.models import Campaign, CampaignStep, GenerationLog, Send, WeeklySummary

from app import schemas
from app.core.deps import CurrentUser, DbSession, get_owned_or_404, scoped

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _window(db, user: CurrentUser, days: int, campaign_id: uuid.UUID | None):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = scoped(db, Send, user).filter(Send.created_at >= since)
    if campaign_id is not None:
        query = query.filter(Send.campaign_id == campaign_id)
    return query


def _counters(query) -> dict[str, int]:
    return {
        "sent": query.count(),
        "delivered": query.filter(Send.status == "delivered").count(),
        "opened": query.filter(Send.opened_at.is_not(None)).count(),
        "clicked": query.filter(Send.clicked_at.is_not(None)).count(),
        "converted": query.filter(Send.converted_at.is_not(None)).count(),
    }


def _rates(counters: dict[str, int]) -> dict[str, float]:
    delivered = counters["delivered"]
    if delivered <= 0:
        return {"open_rate": 0.0, "ctr": 0.0, "conversion_rate": 0.0}
    return {
        "open_rate": counters["opened"] / delivered,
        "ctr": counters["clicked"] / delivered,
        "conversion_rate": counters["converted"] / delivered,
    }


@router.get("/overview", response_model=schemas.AnalyticsOverview)
def overview(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
    campaign_id: uuid.UUID | None = Query(default=None),
):
    if campaign_id is not None:
        get_owned_or_404(db, Campaign, campaign_id, user)

    base = _window(db, user, days, campaign_id)
    counters = _counters(base)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    spend = (
        db.query(func.coalesce(func.sum(GenerationLog.cost_usd), 0))
        .filter(
            GenerationLog.business_id == user.business_id,
            GenerationLog.created_at >= since,
        )
        .scalar()
    )

    day_col = func.date(Send.created_at).label("day")
    rows = (
        db.query(
            day_col,
            func.count(Send.id),
            func.count().filter(Send.status == "delivered"),
            func.count().filter(Send.opened_at.is_not(None)),
            func.count().filter(Send.clicked_at.is_not(None)),
            func.count().filter(Send.converted_at.is_not(None)),
        )
        .filter(
            Send.business_id == user.business_id,
            Send.created_at >= since,
            *( [Send.campaign_id == campaign_id] if campaign_id else [] ),
        )
        .group_by(day_col)
        .order_by(day_col)
        .all()
    )
    by_day = [
        schemas.DayBucket(
            date=str(row[0]),
            sent=row[1],
            delivered=row[2],
            opened=row[3],
            clicked=row[4],
            converted=row[5],
        )
        for row in rows
    ]

    return schemas.AnalyticsOverview(
        **counters,
        **_rates(counters),
        spend_usd=float(spend or 0),
        by_day=by_day,
    )


@router.get("/campaigns/{campaign_id}/funnel", response_model=schemas.FunnelResponse)
def funnel(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession):
    campaign = get_owned_or_404(db, Campaign, campaign_id, user)
    steps = (
        db.query(CampaignStep)
        .filter(CampaignStep.campaign_id == campaign.id)
        .order_by(CampaignStep.position.asc())
        .all()
    )
    items = []
    for step in steps:
        q = scoped(db, Send, user).filter(Send.step_id == step.id)
        items.append(
            schemas.FunnelStep(
                step_id=step.id,
                position=step.position,
                sent=q.count(),
                opened=q.filter(Send.opened_at.is_not(None)).count(),
                clicked=q.filter(Send.clicked_at.is_not(None)).count(),
            )
        )
    return schemas.FunnelResponse(items=items)


@router.get("/weekly-summary", response_model=schemas.WeeklySummaryOut)
def weekly_summary(user: CurrentUser, db: DbSession):
    """Latest Card 5 weekly evidence summary for the caller's business.

    404 ``{"detail": "no weekly summary yet"}`` when the hourly worker job
    has not cut one yet (it runs at each business's local Sunday 23:00).
    """
    row = (
        db.query(WeeklySummary)
        .filter(WeeklySummary.business_id == user.business_id)
        .order_by(WeeklySummary.week_start.desc())
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no weekly summary yet"
        )
    return schemas.WeeklySummaryOut(
        week_start=row.week_start,
        top_assets=row.top_assets or [],
        bottom_assets=row.bottom_assets or [],
        best_channel_per_segment=row.best_channel_per_segment or {},
        recommendation=row.recommendation,
        created_at=row.created_at,
    )
