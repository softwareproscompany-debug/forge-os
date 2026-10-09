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
    CampaignStep,
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


# ---------------------------------------------------------------------------
# Brain (Card 5 knowledge-graph views)
#
# One JSON document feeds every brain view (rings, links, timeline, areas):
# five layers — brand kits -> assets -> campaigns -> sends -> events — plus
# the links between them and a 30-day timeline. Read-only, tenant-scoped,
# node lists capped so the payload stays small on large accounts.
# ---------------------------------------------------------------------------

_BRAIN_NODES_PER_LAYER = 60
_BRAIN_LINK_CAP = 300
_BRAIN_TIMELINE_CAP = 240
_BRAIN_TIMELINE_DAYS = 30


def _enum_value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


@router.get("/brain", response_model=schemas.BrainResponse)
def ops_brain(user: CurrentUser, db: DbSession):
    """Read-only knowledge graph for the /brain views."""
    now = _utcnow()
    cutoff = now - timedelta(days=_BRAIN_TIMELINE_DAYS)

    links: list[schemas.BrainLink] = []
    layers: list[schemas.BrainLayer] = []

    # -- foundation: brand kits -------------------------------------------
    kits = scoped(db, BrandKit, user).order_by(BrandKit.created_at).all()
    kit_nodes = [
        schemas.BrainNode(
            id=f"bk-{k.id}",
            label=k.name,
            detail=(k.voice_description or "")[:90] or None,
        )
        for k in kits[:_BRAIN_NODES_PER_LAYER]
    ]
    layers.append(
        schemas.BrainLayer(
            key="foundation", label="Foundation", count=len(kits), nodes=kit_nodes
        )
    )
    first_kit = f"bk-{kits[0].id}" if kits else None

    # -- origination: assets ----------------------------------------------
    asset_total = scoped(db, Asset, user).count()
    assets = (
        scoped(db, Asset, user)
        .order_by(Asset.created_at.desc())
        .limit(_BRAIN_NODES_PER_LAYER)
        .all()
    )
    asset_ids = {a.id for a in assets}
    layers.append(
        schemas.BrainLayer(
            key="origination",
            label="Origination",
            count=asset_total,
            nodes=[
                schemas.BrainNode(
                    id=f"as-{a.id}",
                    label=a.title or "untitled",
                    detail=f"{_enum_value(a.kind)} · {_enum_value(a.status)}",
                    at=a.created_at,
                )
                for a in assets
            ],
        )
    )
    if first_kit:
        for a in assets:
            links.append(
                schemas.BrainLink(source=first_kit, target=f"as-{a.id}", kind="brand")
            )

    # -- growth: campaigns --------------------------------------------------
    campaign_total = scoped(db, Campaign, user).count()
    campaigns = (
        scoped(db, Campaign, user)
        .order_by(Campaign.created_at.desc())
        .limit(_BRAIN_NODES_PER_LAYER)
        .all()
    )
    campaign_ids = {c.id for c in campaigns}
    layers.append(
        schemas.BrainLayer(
            key="growth",
            label="Growth",
            count=campaign_total,
            nodes=[
                schemas.BrainNode(
                    id=f"ca-{c.id}",
                    label=c.name,
                    detail=_enum_value(c.status),
                    at=c.created_at,
                )
                for c in campaigns
            ],
        )
    )
    if campaign_ids and asset_ids:
        step_rows = (
            db.query(CampaignStep)
            .filter(
                CampaignStep.campaign_id.in_(campaign_ids),
                CampaignStep.asset_id.in_(asset_ids),
            )
            .all()
        )
        for step in step_rows:
            links.append(
                schemas.BrainLink(
                    source=f"as-{step.asset_id}",
                    target=f"ca-{step.campaign_id}",
                    kind="asset",
                )
            )

    # -- reach: sends ---------------------------------------------------------
    send_total = scoped(db, Send, user).count()
    sends = (
        scoped(db, Send, user)
        .order_by(Send.created_at.desc())
        .limit(_BRAIN_NODES_PER_LAYER)
        .all()
    )
    send_ids = {str(s.id) for s in sends}
    layers.append(
        schemas.BrainLayer(
            key="reach",
            label="Reach",
            count=send_total,
            nodes=[
                schemas.BrainNode(
                    id=f"se-{s.id}",
                    label=f"{_enum_value(s.channel)} → {s.to_address}",
                    detail=(s.subject or "").strip() or None,
                    at=s.sent_at or s.created_at,
                )
                for s in sends
            ],
        )
    )
    for s in sends:
        if s.campaign_id in campaign_ids:
            links.append(
                schemas.BrainLink(
                    source=f"ca-{s.campaign_id}", target=f"se-{s.id}", kind="campaign"
                )
            )

    # -- evidence: events -----------------------------------------------------
    event_total = scoped(db, Event, user).count()
    events = (
        scoped(db, Event, user)
        .order_by(Event.created_at.desc())
        .limit(_BRAIN_NODES_PER_LAYER)
        .all()
    )
    layers.append(
        schemas.BrainLayer(
            key="evidence",
            label="Evidence",
            count=event_total,
            nodes=[
                schemas.BrainNode(
                    id=f"ev-{e.id}", label=e.kind, detail=None, at=e.created_at
                )
                for e in events
            ],
        )
    )
    for e in events:
        payload = e.payload or {}
        send_ref = payload.get("send_id")
        if send_ref and str(send_ref) in send_ids:
            links.append(
                schemas.BrainLink(
                    source=f"se-{send_ref}", target=f"ev-{e.id}", kind="engagement"
                )
            )

    # -- timeline: sends + events over the last 30 days ------------------------
    timeline: list[schemas.BrainTimelinePoint] = []
    recent_sends = (
        scoped(db, Send, user)
        .filter(Send.created_at >= cutoff)
        .order_by(Send.created_at.asc())
        .limit(_BRAIN_TIMELINE_CAP)
        .all()
    )
    for s in recent_sends:
        timeline.append(
            schemas.BrainTimelinePoint(
                at=s.sent_at or s.created_at,
                kind="sent",
                label=f"{_enum_value(s.channel)} → {s.to_address}",
            )
        )
    _event_kind_map = {
        "email_opened": "opened",
        "email_clicked": "clicked",
        "converted": "converted",
    }
    recent_events = (
        scoped(db, Event, user)
        .filter(Event.created_at >= cutoff)
        .order_by(Event.created_at.asc())
        .limit(_BRAIN_TIMELINE_CAP)
        .all()
    )
    for e in recent_events:
        kind = _event_kind_map.get(e.kind, "event")
        timeline.append(
            schemas.BrainTimelinePoint(at=e.created_at, kind=kind, label=e.kind)
        )
    timeline.sort(key=lambda p: p.at)
    timeline = timeline[-_BRAIN_TIMELINE_CAP:]

    return schemas.BrainResponse(
        as_of=now,
        layers=layers,
        links=links[:_BRAIN_LINK_CAP],
        timeline=timeline,
    )
