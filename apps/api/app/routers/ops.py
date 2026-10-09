"""Ops / mission-control read model.

Read-only aggregate backing the ``/ops`` dashboard page. It merges three
sources the worker already writes — ``generation_logs`` (job records),
``sends`` (the send log) and ``events`` — into one activity feed, plus
per-pipeline-stage counts and headline counters.

Dashboard rule (Card 5): this endpoint only reads. Nothing here writes,
and the response carries ``as_of`` so every panel can show its data
timestamp.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from forge_db.models import (
    Asset,
    BrandKit,
    Campaign,
    CampaignEnrollment,
    CampaignStatus,
    DevOutbox,
    EnrollmentStatus,
    Event,
    GenerationLog,
    Send,
    SendStatus,
)

from app import schemas
from app.core.deps import CurrentUser, DbSession, scoped

router = APIRouter(prefix="/ops", tags=["ops"])

#: Max feed items returned (also the hard cap for ``?limit=``).
_MAX_LIMIT = 100

#: Lookback window for the "24h" stage counters.
_DAY = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stage_counts(db: DbSession, user: CurrentUser, day_ago: datetime) -> dict[str, dict[str, int]]:
    """Live counts per FORGE pipeline stage (all tenant-scoped)."""
    assets = scoped(db, Asset, user)
    sends = scoped(db, Send, user)

    def count(q) -> int:
        return q.count()

    return {
        "foundation": {
            "brand_kits": count(scoped(db, BrandKit, user)),
        },
        "origination": {
            "draft": count(assets.filter(Asset.status == "draft")),
            "in_review": count(assets.filter(Asset.status == "in_review")),
            "approved": count(assets.filter(Asset.status == "approved")),
            "rejected": count(assets.filter(Asset.status == "rejected")),
        },
        "reach": {
            "in_flight": count(
                sends.filter(Send.status.in_(["queued", "sending"]))
            ),
            "outbox_24h": count(
                scoped(db, DevOutbox, user).filter(DevOutbox.created_at >= day_ago)
            ),
        },
        "growth": {
            "campaigns_running": count(
                scoped(db, Campaign, user).filter(
                    Campaign.status == CampaignStatus.running
                )
            ),
            "enrollments_active": count(
                db.query(CampaignEnrollment)
                .join(Campaign, Campaign.id == CampaignEnrollment.campaign_id)
                .filter(
                    Campaign.business_id == user.business_id,
                    CampaignEnrollment.status == EnrollmentStatus.active,
                )
            ),
        },
        "evidence": {
            "delivered_24h": count(
                sends.filter(
                    Send.status == "delivered", Send.created_at >= day_ago
                )
            ),
            "opened_24h": count(sends.filter(Send.opened_at.is_not(None), Send.created_at >= day_ago)),
            "clicked_24h": count(sends.filter(Send.clicked_at.is_not(None), Send.created_at >= day_ago)),
            "converted_24h": count(
                sends.filter(Send.converted_at.is_not(None), Send.created_at >= day_ago)
            ),
        },
    }


def _activity_feed(
    db: DbSession, user: CurrentUser, limit: int
) -> list[schemas.OpsActivityItem]:
    """Merge generation logs, sends and events into one newest-first feed."""
    items: list[schemas.OpsActivityItem] = []

    gen_rows = (
        db.query(GenerationLog, Asset.title)
        .join(Asset, Asset.id == GenerationLog.asset_id)
        .filter(GenerationLog.business_id == user.business_id)
        .order_by(GenerationLog.created_at.desc())
        .limit(limit)
        .all()
    )
    for log, title in gen_rows:
        items.append(
            schemas.OpsActivityItem(
                id=f"gen-{log.id}",
                kind="generation",
                title=f"generate_asset · {title or 'untitled'}",
                detail=(
                    f"{log.provider}/{log.model} · "
                    f"{log.tokens_in + log.tokens_out} tokens · "
                    f"${float(log.cost_usd):.4f}"
                ),
                status=None,
                at=log.created_at,
            )
        )

    send_rows = (
        scoped(db, Send, user)
        .order_by(Send.created_at.desc())
        .limit(limit)
        .all()
    )
    for send in send_rows:
        channel = send.channel.value if hasattr(send.channel, "value") else str(send.channel)
        status = send.status.value if hasattr(send.status, "value") else str(send.status)
        items.append(
            schemas.OpsActivityItem(
                id=f"send-{send.id}",
                kind="send",
                title=f"send_message · {channel} → {send.to_address}",
                detail=(send.subject or "").strip() or None,
                status=status,
                at=send.sent_at or send.created_at,
            )
        )

    event_rows = (
        scoped(db, Event, user)
        .order_by(Event.created_at.desc())
        .limit(limit)
        .all()
    )
    for event in event_rows:
        items.append(
            schemas.OpsActivityItem(
                id=f"evt-{event.id}",
                kind="event",
                title=f"event · {event.kind}",
                detail=None,
                status=None,
                at=event.created_at,
            )
        )

    items.sort(key=lambda i: i.at, reverse=True)
    return items[:limit]


@router.get("/activity", response_model=schemas.OpsActivityResponse)
def ops_activity(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=30, ge=1, le=_MAX_LIMIT),
):
    """Read-only mission-control aggregate: stages, counters, activity feed."""
    now = _utcnow()
    day_ago = now - _DAY
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    sends = scoped(db, Send, user)
    counters = schemas.OpsCounters(
        sends_today=sends.filter(Send.created_at >= today_start).count(),
        generations_today=scoped(db, GenerationLog, user)
        .filter(GenerationLog.created_at >= today_start)
        .count(),
        in_flight=sends.filter(
            Send.status.in_([SendStatus.queued, SendStatus.sending])
        ).count(),
    )

    return schemas.OpsActivityResponse(
        as_of=now,
        stages=_stage_counts(db, user, day_ago),
        counters=counters,
        activity=_activity_feed(db, user, limit),
    )
