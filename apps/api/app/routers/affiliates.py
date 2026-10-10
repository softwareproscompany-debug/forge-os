"""Affiliate marketing: "we promote affiliate offers, we earn commissions".

Programs and links are tenant-scoped CRUD. Clicks and conversions ride the
``events`` table (``affiliate_clicked`` / ``affiliate_converted`` kinds, link
ids in the payload); the earnings endpoint aggregates them honestly from
stored events — no fabricated numbers.

``GET /r/{slug}`` is the public short-link redirect (no auth): it records an
``affiliate_clicked`` event (anonymous, contact_id null) and 302s to the
destination URL with the link's UTM params appended. It never leaks business
internals — unknown/inactive slugs are plain 404s.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from forge_db.models import AffiliateLink, AffiliateProgram, Event

from app import schemas
from app.core.deps import (
    CurrentSettings,
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)
from app.core.rate_limit import quota_limited

router = APIRouter(prefix="/affiliates", tags=["affiliates"])

#: Public redirect router — registered in main.py WITHOUT the /api/v1 prefix
#: and without auth.
redirect_router = APIRouter(tags=["affiliate-redirect"])

_PROGRAM_STATUSES = ("active", "paused")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_program_status(value: str | None) -> None:
    if value is not None and value not in _PROGRAM_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown program status: {value!r} (expected one of {', '.join(_PROGRAM_STATUSES)})",
        )


def _validate_destination_url(url: str) -> str:
    url = url.strip()
    try:
        parts = urlsplit(url)
    except Exception:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="destination_url must be an absolute http(s) URL",
        )
    return url


def _check_slug_unique(
    db: DbSession, user: CurrentUser, slug: str, exclude_id: uuid.UUID | None = None
) -> None:
    q = scoped(db, AffiliateLink, user).filter(AffiliateLink.slug == slug)
    if exclude_id is not None:
        q = q.filter(AffiliateLink.id != exclude_id)
    if q.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Slug already in use: {slug!r}",
        )


def _check_program_owned(db: DbSession, user: CurrentUser, program_id: uuid.UUID) -> AffiliateProgram:
    return get_owned_or_404(db, AffiliateProgram, program_id, user)


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


@router.get("/programs", response_model=schemas.Page[schemas.AffiliateProgramOut])
def list_programs(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, AffiliateProgram, user).order_by(AffiliateProgram.created_at.desc())
    return paginate(query, limit, offset)


@router.post(
    "/programs",
    response_model=schemas.AffiliateProgramOut,
    status_code=status.HTTP_201_CREATED,
)
def create_program(
    payload: schemas.AffiliateProgramCreate, user: CurrentUser, db: DbSession
):
    _validate_program_status(payload.status)
    program = AffiliateProgram(
        business_id=user.business_id,
        name=payload.name.strip(),
        network=payload.network.strip().lower() or "other",
        website_url=(payload.website_url or "").strip() or None,
        default_commission_pct=payload.default_commission_pct,
        cookie_days=payload.cookie_days,
        status=payload.status,
        notes=(payload.notes or "").strip() or None,
    )
    db.add(program)
    db.commit()
    db.refresh(program)
    return program


@router.get("/programs/{program_id}", response_model=schemas.AffiliateProgramOut)
def get_program(program_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, AffiliateProgram, program_id, user)


@router.patch("/programs/{program_id}", response_model=schemas.AffiliateProgramOut)
def update_program(
    program_id: uuid.UUID,
    payload: schemas.AffiliateProgramUpdate,
    user: CurrentUser,
    db: DbSession,
):
    program = get_owned_or_404(db, AffiliateProgram, program_id, user)
    _validate_program_status(payload.status)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if field in ("name", "network", "website_url", "notes") and isinstance(value, str):
            value = value.strip()
            if field == "network":
                value = value.lower() or "other"
            if field in ("website_url", "notes") and not value:
                value = None
        setattr(program, field, value)
    db.commit()
    db.refresh(program)
    return program


@router.delete("/programs/{program_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_program(program_id: uuid.UUID, user: CurrentUser, db: DbSession):
    program = get_owned_or_404(db, AffiliateProgram, program_id, user)
    db.delete(program)  # links cascade via the relationship
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@router.get("/links", response_model=schemas.Page[schemas.AffiliateLinkOut])
def list_links(
    user: CurrentUser,
    db: DbSession,
    program_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, AffiliateLink, user)
    if program_id is not None:
        query = query.filter(AffiliateLink.program_id == program_id)
    query = query.order_by(AffiliateLink.created_at.desc())
    return paginate(query, limit, offset)


@router.post(
    "/links",
    response_model=schemas.AffiliateLinkOut,
    status_code=status.HTTP_201_CREATED,
)
def create_link(payload: schemas.AffiliateLinkCreate, user: CurrentUser, db: DbSession):
    _check_program_owned(db, user, payload.program_id)
    _check_slug_unique(db, user, payload.slug)
    link = AffiliateLink(
        business_id=user.business_id,
        program_id=payload.program_id,
        label=payload.label.strip(),
        slug=payload.slug,
        destination_url=_validate_destination_url(payload.destination_url),
        # Sensible tracking defaults; explicit values (or null via PATCH)
        # override them.
        utm_source=(payload.utm_source or "").strip() or "forgeos",
        utm_medium=(payload.utm_medium or "").strip() or "affiliate",
        utm_campaign=(payload.utm_campaign or "").strip() or None,
        is_active=payload.is_active,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


@router.get("/links/{link_id}", response_model=schemas.AffiliateLinkOut)
def get_link(link_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, AffiliateLink, link_id, user)


@router.patch("/links/{link_id}", response_model=schemas.AffiliateLinkOut)
def update_link(
    link_id: uuid.UUID,
    payload: schemas.AffiliateLinkUpdate,
    user: CurrentUser,
    db: DbSession,
):
    link = get_owned_or_404(db, AffiliateLink, link_id, user)
    data = payload.model_dump(exclude_unset=True)
    if "program_id" in data and data["program_id"] is not None:
        _check_program_owned(db, user, data["program_id"])
    if "slug" in data and data["slug"] is not None:
        _check_slug_unique(db, user, data["slug"], exclude_id=link.id)
    if "destination_url" in data and data["destination_url"] is not None:
        data["destination_url"] = _validate_destination_url(data["destination_url"])
    for field, value in data.items():
        if field in ("label", "utm_source", "utm_medium", "utm_campaign") and isinstance(
            value, str
        ):
            value = value.strip() or None
            if field == "label" and not value:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="label must not be blank",
                )
        setattr(link, field, value)
    db.commit()
    db.refresh(link)
    return link


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(link_id: uuid.UUID, user: CurrentUser, db: DbSession):
    link = get_owned_or_404(db, AffiliateLink, link_id, user)
    db.delete(link)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Earnings — aggregated honestly from stored events
# ---------------------------------------------------------------------------


def _earnings_stats(
    db: DbSession, business_id: uuid.UUID, since: datetime
) -> tuple[schemas.AffiliateTotals, list[schemas.AffiliateProgramStats], list[schemas.AffiliateLinkStats]]:
    links = {
        link.id: link
        for link in db.query(AffiliateLink)
        .filter(AffiliateLink.business_id == business_id)
        .all()
    }
    programs = {
        program.id: program
        for program in db.query(AffiliateProgram)
        .filter(AffiliateProgram.business_id == business_id)
        .all()
    }

    events = (
        db.query(Event.kind, Event.payload)
        .filter(
            Event.business_id == business_id,
            Event.kind.in_(["affiliate_clicked", "affiliate_converted"]),
            Event.created_at >= since,
        )
        .all()
    )
    link_agg: dict[uuid.UUID, dict[str, object]] = {}
    for kind, payload in events:
        payload = payload or {}
        try:
            link_id = uuid.UUID(str(payload.get("link_id") or ""))
        except (ValueError, AttributeError, TypeError):
            continue
        if link_id not in links:
            continue
        slot = link_agg.setdefault(
            link_id, {"clicks": 0, "conversions": 0, "earnings": Decimal("0")}
        )
        if kind == "affiliate_clicked":
            slot["clicks"] = int(slot["clicks"]) + 1
        elif kind == "affiliate_converted":
            slot["conversions"] = int(slot["conversions"]) + 1
            try:
                commission = Decimal(str(payload.get("commission_usd") or "0"))
            except Exception:
                commission = Decimal("0")
            slot["earnings"] = Decimal(slot["earnings"]) + commission

    per_link: list[schemas.AffiliateLinkStats] = []
    prog_agg: dict[uuid.UUID, dict[str, object]] = {}
    for link_id, slot in link_agg.items():
        link = links[link_id]
        program = programs.get(link.program_id)
        clicks = int(slot["clicks"])
        conversions = int(slot["conversions"])
        earnings = Decimal(slot["earnings"])
        per_link.append(
            schemas.AffiliateLinkStats(
                link_id=link_id,
                label=link.label,
                program_name=program.name if program else "",
                clicks=clicks,
                conversions=conversions,
                conversion_rate=round(conversions / clicks, 4) if clicks else 0.0,
                earnings_usd=float(earnings),
            )
        )
        ps = prog_agg.setdefault(
            link.program_id, {"clicks": 0, "conversions": 0, "earnings": Decimal("0")}
        )
        ps["clicks"] = int(ps["clicks"]) + clicks
        ps["conversions"] = int(ps["conversions"]) + conversions
        ps["earnings"] = Decimal(ps["earnings"]) + earnings

    per_program: list[schemas.AffiliateProgramStats] = []
    for program_id, slot in prog_agg.items():
        program = programs.get(program_id)
        clicks = int(slot["clicks"])
        conversions = int(slot["conversions"])
        earnings = Decimal(slot["earnings"])
        per_program.append(
            schemas.AffiliateProgramStats(
                program_id=program_id,
                program_name=program.name if program else "",
                clicks=clicks,
                conversions=conversions,
                conversion_rate=round(conversions / clicks, 4) if clicks else 0.0,
                earnings_usd=float(earnings),
            )
        )

    total_clicks = sum(s.clicks for s in per_link)
    total_conversions = sum(s.conversions for s in per_link)
    total_earnings = sum((s.earnings_usd for s in per_link), 0.0)
    totals = schemas.AffiliateTotals(
        clicks=total_clicks,
        conversions=total_conversions,
        conversion_rate=(
            round(total_conversions / total_clicks, 4) if total_clicks else 0.0
        ),
        earnings_usd=round(total_earnings, 2),
    )
    per_link.sort(key=lambda s: (-s.earnings_usd, s.label))
    per_program.sort(key=lambda s: (-s.earnings_usd, s.program_name))
    return totals, per_program, per_link


@router.get("/earnings", response_model=schemas.AffiliateEarningsResponse)
def affiliate_earnings(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
):
    """Per-program and per-link clicks, conversions and earnings.

    Aggregated from stored ``affiliate_clicked`` / ``affiliate_converted``
    events only — if no network has reported anything, the numbers are
    honestly zero.
    """
    since = _utcnow() - timedelta(days=days)
    totals, per_program, per_link = _earnings_stats(db, user.business_id, since)
    return schemas.AffiliateEarningsResponse(
        days=days, totals=totals, per_program=per_program, per_link=per_link
    )


# ---------------------------------------------------------------------------
# Dashboard — one call for the Overview tab
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_model=schemas.AffiliateDashboardResponse)
def affiliate_dashboard(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
):
    """Aggregated dashboard data: totals, per-program stats, top links,
    daily series for the chart, and recent click/conversion activity.

    All numbers come from stored ``affiliate_clicked`` /
    ``affiliate_converted`` events — honestly zero when nothing happened.
    """
    since = _utcnow() - timedelta(days=days)
    totals, per_program, per_link = _earnings_stats(db, user.business_id, since)

    # Daily series for the chart.
    links = {
        link.id: link
        for link in db.query(AffiliateLink)
        .filter(AffiliateLink.business_id == user.business_id)
        .all()
    }
    events = (
        db.query(Event.kind, Event.payload, Event.created_at)
        .filter(
            Event.business_id == user.business_id,
            Event.kind.in_(["affiliate_clicked", "affiliate_converted"]),
            Event.created_at >= since,
        )
        .all()
    )
    by_day: dict[str, dict[str, object]] = {}
    today = _utcnow().date()
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        by_day[d] = {"clicks": 0, "conversions": 0, "earnings": Decimal("0")}
    for kind, payload, created_at in events:
        day = created_at.date().isoformat()
        slot = by_day.get(day)
        if slot is None:
            continue
        if kind == "affiliate_clicked":
            slot["clicks"] = int(slot["clicks"]) + 1
        elif kind == "affiliate_converted":
            slot["conversions"] = int(slot["conversions"]) + 1
            try:
                commission = Decimal(str((payload or {}).get("commission_usd") or "0"))
            except Exception:
                commission = Decimal("0")
            slot["earnings"] = Decimal(slot["earnings"]) + commission
    daily = [
        schemas.AffiliateDailyPoint(
            date=d,
            clicks=int(v["clicks"]),
            conversions=int(v["conversions"]),
            earnings_usd=float(Decimal(v["earnings"])),
        )
        for d, v in sorted(by_day.items())
    ]

    # Recent activity (latest 15 events with link/program names).
    programs = {
        p.id: p
        for p in db.query(AffiliateProgram)
        .filter(AffiliateProgram.business_id == user.business_id)
        .all()
    }
    recent_events = (
        db.query(Event)
        .filter(
            Event.business_id == user.business_id,
            Event.kind.in_(["affiliate_clicked", "affiliate_converted"]),
        )
        .order_by(Event.created_at.desc())
        .limit(15)
        .all()
    )
    recent_activity: list[schemas.AffiliateActivityItem] = []
    for ev in recent_events:
        payload = ev.payload or {}
        link = None
        try:
            link_id = uuid.UUID(str(payload.get("link_id") or ""))
            link = links.get(link_id)
        except (ValueError, AttributeError, TypeError):
            pass
        program = programs.get(link.program_id) if link else None
        commission = None
        if ev.kind == "affiliate_converted":
            try:
                commission = float(payload.get("commission_usd") or 0)
            except (ValueError, TypeError):
                commission = 0.0
        recent_activity.append(
            schemas.AffiliateActivityItem(
                kind=ev.kind,
                link_label=link.label if link else "unknown link",
                program_name=program.name if program else "",
                commission_usd=commission,
                created_at=ev.created_at,
            )
        )

    return schemas.AffiliateDashboardResponse(
        days=days,
        totals=totals,
        per_program=per_program,
        top_links=per_link[:5],
        daily=daily,
        recent_activity=recent_activity,
    )


# ---------------------------------------------------------------------------
# Viator network integration (one of many — see Programs for any network)
# ---------------------------------------------------------------------------


@router.post("/viator/search")
async def viator_search(
    payload: schemas.ViatorSearchRequest, user: CurrentUser, db: DbSession
):
    """Live Viator product search (no DB writes). destination_id OR keyword."""
    from app.affiliate.connectors.viator import (
        ViatorAuthError,
        ViatorError,
        ViatorNotConfigured,
        connector_for_business,
    )
    from app.core.config import get_settings

    if not payload.destination_id and not payload.keyword:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide destination_id or keyword.",
        )
    settings = get_settings()
    try:
        connector = connector_for_business(db, user.business_id, settings)
    except ViatorNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    try:
        products: list[dict] = []
        if payload.destination_id:
            products = await connector.search_products(
                payload.destination_id, count=payload.count
            )
        if not products and payload.keyword:
            products = await connector.freetext_search(
                payload.keyword, count=payload.count
            )
    except ViatorAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except ViatorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Viator search failed: {exc}",
        ) from exc
    return {"count": len(products), "products": products}


@router.post("/viator/import", response_model=schemas.ViatorImportResponse)
def viator_import(
    payload: schemas.ViatorImportRequest, user: CurrentUser, db: DbSession
):
    """Import Viator products (from /viator/search results) as programs."""
    from app.affiliate import viator_service

    products = [p.model_dump(exclude_none=False) for p in payload.products]
    items = viator_service.import_products(
        db, user.business_id, products, actor_id=user.id
    )
    imported = sum(1 for i in items if i.get("imported"))
    return schemas.ViatorImportResponse(
        imported=imported, skipped=len(items) - imported, items=items
    )


@router.post("/viator/generate-ads")
@quota_limited("generation")
def viator_generate_ads(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
    payload: schemas.ViatorGenerateAdsRequest | None = None,
):
    """Generate draft ad assets for Viator programs (FTC disclosure included,
    never auto-published). When program_ids is omitted, targets Viator
    programs with no ad yet."""
    from app.affiliate import viator_service

    program_ids = payload.program_ids if payload else None
    ads = viator_service.generate_ads(
        db, user.business_id, program_ids, actor_id=user.id
    )
    return {"ads_created": len(ads), "ads": ads}


# ---------------------------------------------------------------------------
# Automation rules — scheduled affiliate pipeline (discover → import →
# ads → draft campaigns). Everything lands as drafts; nothing auto-publishes
# or auto-launches.
# ---------------------------------------------------------------------------


@router.get("/automation/rules", response_model=list[schemas.AffiliateAutomationRuleOut])
def list_automation_rules(user: CurrentUser, db: DbSession):
    from forge_db.models import AffiliateAutomationRule

    return (
        db.query(AffiliateAutomationRule)
        .filter(AffiliateAutomationRule.business_id == user.business_id)
        .order_by(AffiliateAutomationRule.created_at.desc())
        .all()
    )


@router.post(
    "/automation/rules",
    response_model=schemas.AffiliateAutomationRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_automation_rule(
    payload: schemas.AffiliateAutomationRuleCreate, user: CurrentUser, db: DbSession
):
    from forge_db.models import AffiliateAutomationRule

    rule = AffiliateAutomationRule(
        business_id=user.business_id,
        name=payload.name,
        rule_type=payload.rule_type,
        network=payload.network,
        config=payload.config,
        schedule=payload.schedule,
        enabled=payload.enabled,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch(
    "/automation/rules/{rule_id}",
    response_model=schemas.AffiliateAutomationRuleOut,
)
def update_automation_rule(
    rule_id: uuid.UUID,
    payload: schemas.AffiliateAutomationRuleUpdate,
    user: CurrentUser,
    db: DbSession,
):
    from forge_db.models import AffiliateAutomationRule

    rule = get_owned_or_404(db, AffiliateAutomationRule, rule_id, user)
    for field in ("name", "config", "schedule", "enabled"):
        value = getattr(payload, field)
        if value is not None:
            setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/automation/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_automation_rule(rule_id: uuid.UUID, user: CurrentUser, db: DbSession):
    from forge_db.models import AffiliateAutomationRule

    rule = get_owned_or_404(db, AffiliateAutomationRule, rule_id, user)
    db.delete(rule)
    db.commit()


@router.post("/automation/rules/{rule_id}/run")
def run_automation_rule_now(rule_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Manually trigger a rule immediately (also how scheduled runs execute)."""
    from forge_db.models import AffiliateAutomationRule

    rule = get_owned_or_404(db, AffiliateAutomationRule, rule_id, user)
    result = _execute_automation_rule(db, user, rule)
    rule.last_run_at = _utcnow()
    rule.last_run_result = result.get("summary", "")
    db.commit()
    return result


@router.get("/alerts", response_model=list[schemas.AffiliateAlert])
def affiliate_alerts(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
    min_clicks: int = Query(default=50, ge=10, description="Clicks threshold for underperforming flag"),
):
    """Smart triggers: underperforming links, programs without ads, etc."""
    from forge_db.models import Asset, AssetKind, AssetStatus

    since = _utcnow() - timedelta(days=days)
    _, per_program, per_link = _earnings_stats(db, user.business_id, since)
    alerts: list[schemas.AffiliateAlert] = []

    # Links with significant clicks but zero conversions.
    for stat in per_link:
        if stat.clicks >= min_clicks and stat.conversions == 0:
            alerts.append(
                schemas.AffiliateAlert(
                    alert_type="underperforming_link",
                    severity="warning",
                    title=f"Link '{stat.label}' has {stat.clicks} clicks, 0 conversions",
                    detail=(
                        f"'{stat.label}' ({stat.program_name}) is getting traffic "
                        f"but nothing converts. Consider better ad copy, a different "
                        f"offer, or pausing the link."
                    ),
                    link_id=stat.link_id,
                )
            )

    # Programs with no draft ad creative.
    programs_with_ads = {
        a.title for a in db.query(Asset)
        .filter(
            Asset.business_id == user.business_id,
            Asset.kind == AssetKind.ad,
            Asset.is_affiliate_content.is_(True),
        )
        .all()
    }
    programs = (
        db.query(AffiliateProgram)
        .filter(
            AffiliateProgram.business_id == user.business_id,
            AffiliateProgram.status == "active",
        )
        .all()
    )
    for program in programs:
        if not any(program.name[:40] in (t or "") for t in programs_with_ads):
            alerts.append(
                schemas.AffiliateAlert(
                    alert_type="no_ads",
                    severity="info",
                    title=f"Program '{program.name}' has no ad creative",
                    detail=(
                        "No draft ads exist for this program. Generate some from "
                        "the Creatives tab or enable auto-ads."
                    ),
                    program_id=program.id,
                )
            )
    return alerts


def _execute_automation_rule(db, user, rule) -> dict:
    """Execute one automation rule synchronously. Returns a result summary."""
    from app.affiliate import viator_service

    cfg = rule.config or {}
    summary_parts: list[str] = []

    if rule.rule_type in ("auto_import", "autopilot_sweep"):
        # Discover products from the network.
        products: list[dict] = []
        if rule.network == "viator":
            import asyncio
            from app.affiliate.connectors.viator import connector_for_business
            from app.core.config import get_settings

            settings = get_settings()
            connector = connector_for_business(db, user.business_id, settings)
            count = int(cfg.get("count", 10))
            dest = cfg.get("destination_id")
            keyword = cfg.get("keyword")
            min_rating = cfg.get("min_rating")

            async def _search():
                out: list[dict] = []
                if dest:
                    out = await connector.search_products(int(dest), count=count)
                if not out and keyword:
                    out = await connector.freetext_search(str(keyword), count=count)
                return out

            products = asyncio.run(_search())
            if min_rating:
                try:
                    mr = float(min_rating)
                    products = [
                        p for p in products
                        if (p.get("rating") or 0) >= mr
                    ]
                except (ValueError, TypeError):
                    pass
            items = viator_service.import_products(
                db, user.business_id, products, actor_id=user.id
            )
            imported = sum(1 for i in items if i.get("imported"))
            summary_parts.append(f"imported {imported} products")
        else:
            summary_parts.append(f"network '{rule.network}' has no auto-import connector yet")

    if rule.rule_type in ("auto_ads", "autopilot_sweep"):
        if rule.network == "viator":
            program_ids = cfg.get("program_ids")
            if program_ids:
                try:
                    program_ids = [uuid.UUID(str(pid)) for pid in program_ids]
                except (ValueError, AttributeError):
                    program_ids = None
            ads = viator_service.generate_ads(
                db, user.business_id, program_ids, actor_id=user.id
            )
            summary_parts.append(f"generated {len(ads)} draft ads")
        else:
            summary_parts.append(
                f"auto-ads not yet supported for network '{rule.network}' "
                "(create creatives manually)"
            )

    if rule.rule_type == "autopilot_sweep" and cfg.get("create_campaign"):
        from forge_db.models import Campaign, CampaignStatus

        template = cfg.get("campaign_name_template") or "Affiliate Sweep {date}"
        name = template.replace("{date}", _utcnow().date().isoformat())
        campaign = Campaign(
            business_id=user.business_id,
            created_by=user.id,
            name=name,
            description=f"Auto-created by rule '{rule.name}' (draft).",
            status=CampaignStatus.draft,
        )
        db.add(campaign)
        db.commit()
        summary_parts.append(f"created draft campaign '{name}'")

    return {"rule_id": str(rule.id), "summary": "; ".join(summary_parts) or "nothing to do"}


# ---------------------------------------------------------------------------
# Conversion ingest — network postback stand-in
# ---------------------------------------------------------------------------


@router.post(
    "/conversions",
    response_model=schemas.AffiliateConversionResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_conversion(
    payload: schemas.AffiliateConversionRequest, user: CurrentUser, db: DbSession
):
    """Record a conversion for one of the business's links.

    Networks that support server postbacks should be pointed at
    ``POST /api/v1/affiliates/conversions`` with the account's JWT; the
    ``link_slug`` identifies the link. ``commission_usd`` defaults to
    ``order_value_usd`` × the program's ``default_commission_pct`` / 100.
    """
    link = (
        scoped(db, AffiliateLink, user)
        .filter(AffiliateLink.slug == payload.link_slug)
        .first()
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No link with slug {payload.link_slug!r}",
        )
    program = _check_program_owned(db, user, link.program_id)
    if payload.commission_usd is not None:
        commission = payload.commission_usd
    else:
        commission = (
            payload.order_value_usd * program.default_commission_pct / Decimal("100")
        )
    commission = commission.quantize(Decimal("0.0001"))
    event = Event(
        business_id=user.business_id,
        contact_id=None,
        kind="affiliate_converted",
        payload={
            "link_id": str(link.id),
            "program_id": str(program.id),
            "order_value_usd": float(payload.order_value_usd),
            "commission_usd": float(commission),
        },
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return schemas.AffiliateConversionResponse(
        event_id=event.id,
        link_id=link.id,
        program_id=program.id,
        commission_usd=float(commission),
    )


# ---------------------------------------------------------------------------
# Public short-link redirect — GET /r/{slug} (no auth)
# ---------------------------------------------------------------------------


def _append_utms(destination_url: str, link: AffiliateLink) -> str:
    params = {
        "utm_source": link.utm_source,
        "utm_medium": link.utm_medium,
        "utm_campaign": link.utm_campaign,
    }
    extra = {k: v for k, v in params.items() if v}
    if not extra:
        return destination_url
    parts = urlsplit(destination_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in extra.items():
        query.setdefault(key, value)  # never override the merchant's own params
    return urlunsplit(parts._replace(query=urlencode(query)))


@redirect_router.get("/r/{slug}", include_in_schema=False)
def redirect_short_link(slug: str, db: DbSession):
    """Public affiliate redirect.

    No auth. Looks up an *active* link by slug (plus an active program),
    records one anonymous ``affiliate_clicked`` event, and 302s to the
    destination with the link's UTM params appended. Unknown or inactive
    slugs are plain 404s — nothing about the business leaks.
    """
    link = (
        db.query(AffiliateLink)
        .join(AffiliateProgram, AffiliateProgram.id == AffiliateLink.program_id)
        .filter(
            AffiliateLink.slug == slug,
            AffiliateLink.is_active.is_(True),
            AffiliateProgram.status == "active",
        )
        .order_by(AffiliateLink.created_at.asc())
        .first()
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
        )
    db.add(
        Event(
            business_id=link.business_id,
            contact_id=None,
            kind="affiliate_clicked",
            payload={"link_id": str(link.id), "program_id": str(link.program_id)},
        )
    )
    db.commit()
    return RedirectResponse(url=_append_utms(link.destination_url, link), status_code=302)
