"""Draven Market Intelligence API.

* ``POST /market-intel/research`` — start a research run. Params are
  validated, sensible defaults inferred, and the assumptions returned
  alongside the job so the user sees exactly what was assumed.
* ``GET /market-intel/research`` — job history (paginated).
* ``GET /market-intel/research/{job_id}`` — job detail incl. audit trail.
* ``GET /market-intel/opportunities`` — filter / sort / search.
* ``GET /market-intel/opportunities/{id}`` — full executive report (A-L).
* ``POST /market-intel/opportunities`` — create from user-supplied
  (manual) evidence; everything is labeled ``manual_entry``.
* ``POST /market-intel/opportunities/{id}/save`` — toggle saved flag.
* ``GET /market-intel/sources`` — connector capability registry + health.

All routes are tenant-scoped (``business_id``). Research runs are
rate-limited per business (in-process sliding window; multi-worker
deployments should move this to Redis). Untrusted source content is
stored as evidence strings and rendered as text only.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_

from forge_db.models import (
    MarketOpportunity,
    MarketResearchJob,
    MarketSource,
    RecommendationStatus,
    ResearchJobStatus,
)

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
from app.market_intel.connectors.registry import connectors
from app.market_intel.pipeline import normalize_params, run_research_job
from app.market_intel.reports import build_executive_report
from app.market_intel.scoring import ScoreInput, score_opportunity
from app.market_intel.economics import EconomicsInput, compute_economics

router = APIRouter(prefix="/market-intel", tags=["market-intel"])

# In-process sliding-window rate limit for research runs: 10/hour/business.
# (Same caveat as the TTS limiter — move to Redis for multi-worker.)
_RESEARCH_RATE: dict[uuid.UUID, list[float]] = {}
_RESEARCH_RATE_LIMIT = 10
_RESEARCH_RATE_WINDOW_S = 3600.0


def _check_research_rate_limit(business_id: uuid.UUID) -> bool:
    now = time.monotonic()
    window_start = now - _RESEARCH_RATE_WINDOW_S
    stamps = [s for s in _RESEARCH_RATE.get(business_id, []) if s > window_start]
    if len(stamps) >= _RESEARCH_RATE_LIMIT:
        _RESEARCH_RATE[business_id] = stamps
        return False
    stamps.append(now)
    _RESEARCH_RATE[business_id] = stamps
    return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResearchCreate(BaseModel):
    target_month: int | str | None = Field(default=None)
    target_year: int | None = Field(default=None, ge=2020, le=2100)
    market: str | None = Field(default=None, max_length=2)
    category: str | None = Field(default=None, max_length=128)
    seed_terms: list[str] | None = Field(default=None, max_length=25)
    ticket_tier: Literal["low", "mid", "high", "any"] | None = None
    ticket_thresholds: dict[str, float] | None = None
    channel: str | None = Field(default=None, max_length=64)
    max_results: int | None = Field(default=None, ge=1, le=100)
    unit_costs: dict[str, float] | None = None
    inbound_per_unit: float | None = Field(default=None, ge=0)
    marketplace_fee_pct: float | None = Field(default=None, ge=0, le=1)
    payment_fee_pct: float | None = Field(default=None, ge=0, le=1)
    fulfillment_per_unit: float | None = Field(default=None, ge=0)
    cac_per_order: float | None = Field(default=None, ge=0)
    return_rate_pct: float | None = Field(default=None, ge=0, le=1)


class ManualEvidenceItem(BaseModel):
    kind: str = Field(pattern="^(price|demand|sourcing|note)$")
    label: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.5, ge=0, le=1)


class OpportunityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=480)
    category: str = Field(default="", max_length=128)
    description: str | None = Field(default=None, max_length=5000)
    target_month: int | str | None = None
    target_year: int | None = Field(default=None, ge=2020, le=2100)
    evidence: list[ManualEvidenceItem] = Field(default_factory=list, max_length=50)
    # Optional sourcing costs — without unit_cost the economics stay
    # provisional (stated plainly, never faked).
    unit_cost: float | None = Field(default=None, ge=0)
    inbound_per_unit: float | None = Field(default=None, ge=0)
    marketplace_fee_pct: float | None = Field(default=None, ge=0, le=1)
    payment_fee_pct: float | None = Field(default=None, ge=0, le=1)
    fulfillment_per_unit: float | None = Field(default=None, ge=0)
    cac_per_order: float | None = Field(default=None, ge=0)
    return_rate_pct: float | None = Field(default=None, ge=0, le=1)


def _job_out(job: MarketResearchJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "status": job.status.value,
        "progress": job.progress,
        "params": job.params,
        "assumptions": job.assumptions,
        "results": job.results,
        "audit_trail": job.audit_trail,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _opp_summary(opp: MarketOpportunity) -> dict[str, Any]:
    prices = [p.get("price") for p in (opp.price_evidence or []) if p.get("price")]
    return {
        "id": str(opp.id),
        "name": opp.name,
        "category": opp.category,
        "opportunity_score": opp.opportunity_score,
        "confidence_score": opp.confidence_score,
        "recommendation": opp.recommendation.value,
        "saved": opp.saved,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "signal_kinds": sorted(
            {s.get("kind") for s in (opp.demand_indicators or []) if s.get("kind")}
        ),
        "evidence_gaps": opp.evidence_gaps,
        "freshness": "live"
        if any(
            (s.get("retrieved_at") or "")[:10]
            == _now().date().isoformat()
            for s in (opp.demand_indicators or [])
        )
        else ("estimated" if opp.demand_indicators else "unavailable"),
        "created_at": opp.created_at.isoformat() if opp.created_at else None,
    }


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


@router.post("/research", status_code=201)
@quota_limited("research")
def start_research(
    payload: ResearchCreate,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
) -> dict[str, Any]:
    """Validate params, infer defaults, run the pipeline, return job + assumptions."""
    if not _check_research_rate_limit(user.business_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Research rate limit reached (10 runs/hour). Try again later.",
        )
    raw = payload.model_dump(exclude_none=True)
    try:
        params, assumptions = normalize_params(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    job = MarketResearchJob(
        business_id=user.business_id,
        user_id=user.id,
        params=params,
        assumptions=assumptions,
        status=ResearchJobStatus.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Deterministic staged pipeline, synchronous in the foundation slice.
    run_research_job(db, user, job, settings)
    db.refresh(job)
    return _job_out(job)


@router.get("/research")
def list_research(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, MarketResearchJob, user).order_by(
        MarketResearchJob.created_at.desc()
    )
    page = paginate(query, limit, offset)
    page["items"] = [_job_out(j) for j in page["items"]]
    return page


@router.get("/research/{job_id}")
def get_research(job_id: uuid.UUID, user: CurrentUser, db: DbSession):
    job = get_owned_or_404(db, MarketResearchJob, job_id, user)
    return _job_out(job)


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


@router.get("/opportunities")
def list_opportunities(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=128),
    recommendation: str | None = Query(default=None, max_length=32),
    saved_only: bool = Query(default=False),
    min_score: float | None = Query(default=None, ge=0, le=100),
    sort: str = Query(default="score"),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, MarketOpportunity, user)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                MarketOpportunity.name.ilike(like),
                MarketOpportunity.category.ilike(like),
            )
        )
    if category:
        query = query.filter(MarketOpportunity.category.ilike(f"%{category}%"))
    if recommendation:
        try:
            rec = RecommendationStatus(recommendation)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"unknown recommendation: {recommendation}",
            )
        query = query.filter(MarketOpportunity.recommendation == rec)
    if saved_only:
        query = query.filter(MarketOpportunity.saved.is_(True))
    if min_score is not None:
        query = query.filter(MarketOpportunity.opportunity_score >= min_score)
    if sort == "confidence":
        query = query.order_by(MarketOpportunity.confidence_score.desc())
    elif sort == "newest":
        query = query.order_by(MarketOpportunity.created_at.desc())
    else:  # score — NULL scores last
        query = query.order_by(MarketOpportunity.opportunity_score.desc().nullslast())
    page = paginate(query, limit, offset)
    page["items"] = [_opp_summary(o) for o in page["items"]]
    return page


@router.get("/opportunities/{opportunity_id}")
def get_opportunity(opportunity_id: uuid.UUID, user: CurrentUser, db: DbSession):
    opp = get_owned_or_404(db, MarketOpportunity, opportunity_id, user)
    data = {
        "id": str(opp.id),
        "name": opp.name,
        "category": opp.category,
        "created_at": opp.created_at.isoformat() if opp.created_at else None,
        "demand_indicators": opp.demand_indicators,
        "price_evidence": opp.price_evidence,
        "seasonality_profile": opp.seasonality_profile,
        "competition_summary": opp.competition_summary,
        "unit_economics": opp.unit_economics,
        "sourcing_evidence": opp.sourcing_evidence,
        "risk_flags": opp.risk_flags,
        "score_components": opp.score_components,
        "opportunity_score": opp.opportunity_score,
        "confidence_score": opp.confidence_score,
        "evidence_gaps": opp.evidence_gaps,
        "recommendation": opp.recommendation.value,
        "saved": opp.saved,
        "source_urls": opp.source_urls,
        "identifiers": opp.identifiers,
    }
    return {"opportunity": _opp_summary(opp), "report": build_executive_report(data)}


@router.post("/opportunities", status_code=201)
def create_opportunity(payload: OpportunityCreate, user: CurrentUser, db: DbSession):
    """Create an opportunity from user-supplied evidence (manual_entry).

    Everything is labeled manual_entry with user-asserted confidence.
    Scored with the same engine; confidence is capped accordingly.
    """
    m, y, note = (None, None, "")
    from app.market_intel.pipeline import resolve_month_year

    try:
        m, y, note = resolve_month_year(payload.target_month, payload.target_year)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    price_evidence: list[dict[str, Any]] = []
    demand_indicators: list[dict[str, Any]] = []
    sourcing_evidence: list[dict[str, Any]] = []
    retrieved = _now().isoformat()
    for item in payload.evidence:
        entry = {
            "kind": "manual_entry",
            "label": item.label,
            "value": item.value,
            "confidence": item.confidence,
            "source": "manual",
            "retrieved_at": retrieved,
            "note": "User-supplied evidence — user-asserted, not independently verified.",
        }
        if item.kind == "price":
            try:
                price_evidence.append(
                    {
                        "price": float(item.value),
                        "currency": "USD",
                        "source": "manual",
                        "observed_at": retrieved,
                        "kind": "observed",
                    }
                )
            except ValueError:
                demand_indicators.append({**entry, "unit": "n/a"})
        elif item.kind == "demand":
            demand_indicators.append({**entry, "unit": "user-asserted"})
        elif item.kind == "sourcing":
            sourcing_evidence.append(entry)

    prices = [p["price"] for p in price_evidence]
    selling = sum(prices) / len(prices) if prices else None
    econ = compute_economics(
        EconomicsInput(
            selling_price=selling,
            unit_cost=payload.unit_cost,
            inbound_per_unit=payload.inbound_per_unit or 0.0,
            marketplace_fee_pct=payload.marketplace_fee_pct or 0.0,
            payment_fee_pct=payload.payment_fee_pct or 0.0,
            fulfillment_per_unit=payload.fulfillment_per_unit or 0.0,
            cac_per_order=payload.cac_per_order or 0.0,
            return_rate_pct=payload.return_rate_pct or 0.0,
        )
    )

    score_in = ScoreInput(
        demand_signals=demand_indicators,
        economics={
            **econ.to_dict(),
            "price_evidence_count": len(prices),
            "selling_price": selling,
        },
        seasonality={"target_month": m, "target_year": y, "peak_months": [], "basis": "none"},
        competition={},
        sourcing_evidence=sourcing_evidence,
        risk_flags=[],
        target_month=m,
    )
    result = score_opportunity(score_in)

    opp = MarketOpportunity(
        business_id=user.business_id,
        name=payload.name,
        category=payload.category,
        description=payload.description,
        identifiers={"origin": "manual_entry"},
        price_evidence=price_evidence,
        demand_indicators=demand_indicators,
        seasonality_profile=score_in.seasonality,
        competition_summary={},
        unit_economics=econ.to_dict(),
        sourcing_evidence=sourcing_evidence,
        risk_flags=[],
        score_components=result.components,
        opportunity_score=result.opportunity_score,
        confidence_score=result.confidence_score,
        evidence_gaps=[*result.evidence_gaps, note] if note else result.evidence_gaps,
    )
    db.add(opp)
    db.commit()
    db.refresh(opp)
    return _opp_summary(opp)


@router.post("/opportunities/{opportunity_id}/save")
def toggle_save(opportunity_id: uuid.UUID, user: CurrentUser, db: DbSession):
    opp = get_owned_or_404(db, MarketOpportunity, opportunity_id, user)
    opp.saved = not opp.saved
    db.commit()
    return {"id": str(opp.id), "saved": opp.saved}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.get("/sources")
def list_sources(
    user: CurrentUser, db: DbSession, settings: CurrentSettings
) -> dict[str, Any]:
    """Connector capability registry + live health per business."""
    # Vault DataForSEO credentials layer over env for this tenant.
    try:
        from app.settings_vault import service as _vault

        settings = _vault.vault_dataforseo_settings(
            db, user.business_id, settings
        )
    except Exception:
        pass
    out: list[dict[str, Any]] = []
    for conn in connectors():
        cap = conn.capability.to_dict()
        try:
            configured = conn.is_configured(settings)
            note = "configured" if configured else "not configured"
            ok: bool | None = True if configured else None
        except Exception:  # never let a health check crash the listing
            configured, ok, note = False, False, "health check failed"
        row = (
            db.query(MarketSource)
            .filter(
                MarketSource.business_id == user.business_id,
                MarketSource.connector == conn.name,
            )
            .first()
        )
        if row is None:
            row = MarketSource(
                business_id=user.business_id,
                connector=conn.name,
                display_name=cap["display_name"],
                capability=cap,
                configured=configured,
                last_check_at=_now(),
                last_check_ok=ok,
                last_check_note=note,
            )
            db.add(row)
        else:
            row.display_name = cap["display_name"]
            row.capability = cap
            row.configured = configured
            row.last_check_at = _now()
            row.last_check_ok = ok
            row.last_check_note = note
        out.append(
            {
                "connector": conn.name,
                "display_name": cap["display_name"],
                "configured": configured,
                "last_check_ok": ok,
                "last_check_note": note,
                "capability": cap,
            }
        )
    db.commit()
    return {"sources": out}
