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

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from forge_db.models import AffiliateLink, AffiliateProgram, Event

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

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
