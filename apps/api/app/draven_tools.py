"""Draven tool registry — typed, validated, tenant-scoped tools.

Every tool in this registry:

* has a unique id (``draven.*``), a description, a pydantic input schema,
  a risk level, and a timeout;
* scopes ALL queries to the caller's ``business_id`` (cross-tenant access
  is impossible by construction — there is no code path that queries
  another business);
* executes immediately only when ``risk == "low"``. ``"medium"``/``"high"``
  tools never execute: they return a structured ``approval_required``
  result describing the exact action, affected resources, and estimated
  cost, and the chat layer surfaces them as pending approvals;
* writes an audit row (``draven_tool_runs``) for every invocation,
  including ``approval_required`` outcomes, with input, an output
  summary, risk, status, and duration.

``route_intent`` maps a natural-language message to candidate
(tool_id, input) pairs with keyword rules — no LLM needed for routing.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from forge_db.models import (
    Asset,
    AssetStatus,
    AutopilotSettings,
    Business,
    Campaign,
    CampaignStatus,
    Contact,
    ContentPlan,
    DravenToolRun,
    GenerationLog,
    MarketOpportunity,
    Send,
    Template,
    TravelCustomer,
    TravelLead,
    TravelLeadStatus,
    TripRequest,
    User,
)

# analytics router helpers — the same math the /analytics/overview
# endpoint serves, reused so Draven can never disagree with the UI.
from app.routers.analytics import _counters as _analytics_counters
from app.routers.analytics import _rates as _analytics_rates
from app.routers.analytics import _window as _analytics_window

ToolRisk = Literal["low", "medium", "high"]

DEFAULT_TOOL_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class EmptyInput(BaseModel):
    pass


class ContactsSearchInput(BaseModel):
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


class CampaignRefInput(BaseModel):
    """Identify a campaign by id or by (case-insensitive) name fragment."""

    campaign_id: str | None = Field(default=None)
    campaign_name: str | None = Field(default=None)


class AssetRefInput(BaseModel):
    """Identify an asset by id or by (case-insensitive) title fragment."""

    asset_id: str | None = Field(default=None)
    asset_title: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _ok(output: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "output": output}


def _approval_required(
    action: str,
    description: str,
    affected: list[str],
    estimated_cost_usd: float = 0.0,
    input_echo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "approval_required",
        "approval": {
            "action": action,
            "description": description,
            "affected_resources": affected,
            "estimated_cost_usd": estimated_cost_usd,
            "input": input_echo or {},
        },
    }


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# Tenant-scoped resolution helpers
# ---------------------------------------------------------------------------


def _resolve_campaign(
    db: Session, business_id: uuid.UUID, ref: CampaignRefInput
) -> Campaign | None:
    q = db.query(Campaign).filter(Campaign.business_id == business_id)
    if ref.campaign_id:
        try:
            cid = uuid.UUID(str(ref.campaign_id))
        except ValueError:
            return None
        return q.filter(Campaign.id == cid).first()
    if ref.campaign_name:
        return q.filter(Campaign.name.ilike(f"%{ref.campaign_name}%")).first()
    return None


def _resolve_asset(
    db: Session, business_id: uuid.UUID, ref: AssetRefInput
) -> Asset | None:
    q = db.query(Asset).filter(Asset.business_id == business_id)
    if ref.asset_id:
        try:
            aid = uuid.UUID(str(ref.asset_id))
        except ValueError:
            return None
        return q.filter(Asset.id == aid).first()
    if ref.asset_title:
        return q.filter(Asset.title.ilike(f"%{ref.asset_title}%")).first()
    return None


# ---------------------------------------------------------------------------
# Tool implementations (all tenant-scoped)
# ---------------------------------------------------------------------------


async def _business_summary(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    bid = user.business_id
    business = db.query(Business).filter(Business.id == bid).first()
    counts = {
        "campaigns": db.query(func.count(Campaign.id))
        .filter(Campaign.business_id == bid)
        .scalar()
        or 0,
        "contacts": db.query(func.count(Contact.id))
        .filter(Contact.business_id == bid)
        .scalar()
        or 0,
        "assets": db.query(func.count(Asset.id))
        .filter(Asset.business_id == bid)
        .scalar()
        or 0,
        "templates": db.query(func.count(Template.id))
        .filter(Template.business_id == bid)
        .scalar()
        or 0,
        "assets_in_review": db.query(func.count(Asset.id))
        .filter(Asset.business_id == bid, Asset.status == AssetStatus.in_review)
        .scalar()
        or 0,
        "campaigns_running": db.query(func.count(Campaign.id))
        .filter(
            Campaign.business_id == bid,
            Campaign.status == CampaignStatus.running,
        )
        .scalar()
        or 0,
    }
    return _ok(
        {
            "business_name": business.name if business else None,
            "business_id": str(bid),
            **counts,
        }
    )


async def _approvals_pending(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    rows = (
        db.query(Asset)
        .filter(
            Asset.business_id == user.business_id,
            Asset.status == AssetStatus.in_review,
        )
        .order_by(Asset.created_at.desc())
        .limit(50)
        .all()
    )
    return _ok(
        {
            "count": len(rows),
            "assets": [
                {
                    "id": str(a.id),
                    "title": a.title,
                    "kind": a.kind.value,
                    "version": a.version,
                    "created_at": _iso(a.created_at),
                }
                for a in rows
            ],
        }
    )


async def _assets_pending_review(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    rows = (
        db.query(Asset)
        .filter(
            Asset.business_id == user.business_id,
            Asset.status == AssetStatus.in_review,
        )
        .order_by(Asset.created_at.desc())
        .limit(25)
        .all()
    )
    return _ok(
        {
            "count": len(rows),
            "assets": [
                {
                    "id": str(a.id),
                    "title": a.title,
                    "kind": a.kind.value,
                    "status": a.status.value,
                    "version": a.version,
                    "body_preview": (a.body or "")[:500],
                    "variables": a.variables,
                    "brand_kit_version": a.brand_kit_version,
                    "created_by": str(a.created_by) if a.created_by else None,
                    "created_at": _iso(a.created_at),
                }
                for a in rows
            ],
        }
    )


async def _campaigns_status(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    rows = (
        db.query(Campaign)
        .filter(Campaign.business_id == user.business_id)
        .order_by(Campaign.created_at.desc())
        .limit(50)
        .all()
    )
    return _ok(
        {
            "count": len(rows),
            "campaigns": [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "status": c.status.value,
                    "autopilot": c.autopilot,
                    # NOTE: campaigns has no updated_at column; created_at is
                    # the honest timestamp we have.
                    "created_at": _iso(c.created_at),
                    "starts_at": _iso(c.starts_at),
                }
                for c in rows
            ],
        }
    )


async def _analytics_summary(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    days = 30
    base = _analytics_window(db, user, days, None)
    counters = _analytics_counters(base)
    rates = _analytics_rates(counters)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    spend = (
        db.query(func.coalesce(func.sum(GenerationLog.cost_usd), 0))
        .filter(
            GenerationLog.business_id == user.business_id,
            GenerationLog.created_at >= since,
        )
        .scalar()
    )
    return _ok(
        {
            "days": days,
            **counters,
            **rates,
            "spend_usd": float(spend or 0),
        }
    )


async def _contacts_search(
    db: Session, user: User, inp: ContactsSearchInput
) -> dict[str, Any]:
    q = inp.query.strip()
    if not q:
        return _ok({"count": 0, "contacts": [], "note": "empty query"})
    like = f"%{q}%"
    rows = (
        db.query(Contact)
        .filter(
            Contact.business_id == user.business_id,
            or_(
                Contact.first_name.ilike(like),
                Contact.last_name.ilike(like),
                Contact.email.ilike(like),
                Contact.phone.ilike(like),
            ),
        )
        .order_by(Contact.created_at.desc())
        .limit(inp.limit)
        .all()
    )
    return _ok(
        {
            "count": len(rows),
            "contacts": [
                {
                    "id": str(c.id),
                    "first_name": c.first_name,
                    "last_name": c.last_name,
                    "email": c.email,
                    "phone": c.phone,
                    "tags": c.tags,
                    "unsubscribed": c.unsubscribed,
                }
                for c in rows
            ],
        }
    )


async def _autopilot_status(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    settings = (
        db.query(AutopilotSettings)
        .filter(AutopilotSettings.business_id == user.business_id)
        .first()
    )
    plan = (
        db.query(ContentPlan)
        .filter(ContentPlan.business_id == user.business_id)
        .order_by(ContentPlan.week_start.desc())
        .first()
    )
    return _ok(
        {
            "settings": (
                {
                    "auto_approve": settings.auto_approve,
                    "require_approval_for_channels": settings.require_approval_for_channels,
                    "daily_send_cap": settings.daily_send_cap,
                    "quiet_hours": [settings.quiet_hours_start, settings.quiet_hours_end],
                }
                if settings
                else None
            ),
            "latest_plan": (
                {
                    "id": str(plan.id),
                    "week_start": str(plan.week_start),
                    "status": plan.status.value,
                    "items": len(plan.items or []),
                    "campaign_id": str(plan.campaign_id) if plan.campaign_id else None,
                    "created_at": _iso(plan.created_at),
                }
                if plan
                else None
            ),
        }
    )


async def _campaign_pause(
    db: Session, user: User, inp: CampaignRefInput
) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(db, user.business_id, inp)
    if campaign is None:
        return _error("campaign not found in your business")
    if campaign.status != CampaignStatus.running:
        return _error(
            f"campaign '{campaign.name}' is {campaign.status.value}, not running — "
            "nothing to pause"
        )
    return _approval_required(
        action="campaign.pause",
        description=(
            f"Pause campaign '{campaign.name}' ({campaign.id}). Pausing stops "
            "all future sends and enrollment advances immediately; already-sent "
            "messages are unaffected and enrolled contacts keep their progress. "
            "The campaign can be resumed later."
        ),
        affected=[f"campaign:{campaign.id} ({campaign.name})"],
        estimated_cost_usd=0.0,
        input_echo={"campaign_id": str(campaign.id)},
    )


async def _asset_approve(
    db: Session, user: User, inp: AssetRefInput
) -> dict[str, Any]:
    if not inp.asset_id and not inp.asset_title:
        return _error("asset_id or asset_title is required")
    asset = _resolve_asset(db, user.business_id, inp)
    if asset is None:
        return _error("asset not found in your business")
    if asset.status != AssetStatus.in_review:
        return _error(
            f"asset '{asset.title}' is {asset.status.value}, not in_review — "
            "only in_review assets can be approved"
        )
    return _approval_required(
        action="asset.approve",
        description=(
            f"Approve asset '{asset.title}' ({asset.kind.value}, v{asset.version}). "
            "Approved assets become eligible for sending in campaigns."
        ),
        affected=[f"asset:{asset.id} ({asset.title})"],
        estimated_cost_usd=0.0,
        input_echo={"asset_id": str(asset.id)},
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ToolDef:
    id: str
    description: str
    input_model: type[BaseModel]
    risk: ToolRisk
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    execute: Callable[[Session, User, BaseModel], Awaitable[dict[str, Any]]] | None = field(
        default=None, repr=False
    )


TOOLS: dict[str, ToolDef] = {}


def _register(tool: ToolDef) -> ToolDef:
    if tool.id in TOOLS:
        raise ValueError(f"duplicate tool id: {tool.id}")
    TOOLS[tool.id] = tool
    return tool


_register(
    ToolDef(
        id="draven.business_summary",
        description="Business profile plus entity counts (campaigns, contacts, assets, templates).",
        input_model=EmptyInput,
        risk="low",
        execute=_business_summary,
    )
)
_register(
    ToolDef(
        id="draven.approvals_pending",
        description="Summary of assets awaiting review (id, title, kind, version, created_at).",
        input_model=EmptyInput,
        risk="low",
        execute=_approvals_pending,
    )
)
_register(
    ToolDef(
        id="draven.assets_pending_review",
        description="Full detail of assets awaiting review, including body preview and variables.",
        input_model=EmptyInput,
        risk="low",
        execute=_assets_pending_review,
    )
)
_register(
    ToolDef(
        id="draven.campaigns_status",
        description="All campaigns with status, name, and timestamps.",
        input_model=EmptyInput,
        risk="low",
        execute=_campaigns_status,
    )
)
_register(
    ToolDef(
        id="draven.analytics_summary",
        description="30-day analytics overview: sent, delivered, opened, clicked, converted, rates, spend.",
        input_model=EmptyInput,
        risk="low",
        execute=_analytics_summary,
    )
)
_register(
    ToolDef(
        id="draven.contacts_search",
        description="Search contacts by name, email, or phone (tenant-scoped).",
        input_model=ContactsSearchInput,
        risk="low",
        execute=_contacts_search,
    )
)
_register(
    ToolDef(
        id="draven.autopilot_status",
        description="Autopilot settings and the latest weekly content plan status.",
        input_model=EmptyInput,
        risk="low",
        execute=_autopilot_status,
    )
)
_register(
    ToolDef(
        id="draven.campaign_pause",
        description="HIGH RISK — propose pausing a running campaign. Never executes; returns an approval request.",
        input_model=CampaignRefInput,
        risk="high",
        execute=_campaign_pause,
    )
)
_register(
    ToolDef(
        id="draven.asset_approve",
        description="HIGH RISK — propose approving an in_review asset. Never executes; returns an approval request.",
        input_model=AssetRefInput,
        risk="high",
        execute=_asset_approve,
    )
)

# NOTE: draven.calendar_upcoming is deliberately omitted — there is no
# calendar router or scheduled-items data source in the API, and the
# registry must not invent one. campaigns_status already covers
# scheduled campaigns.


# ---------------------------------------------------------------------------
# Compliance tools (FTC + affiliate network rules)
# ---------------------------------------------------------------------------


class ComplianceCheckToolInput(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    content_type: str = Field(default="social_post")
    pack_id: str | None = Field(default=None)


class ComplianceRulesToolInput(BaseModel):
    pack_id: str | None = Field(
        default=None,
        description="Rule pack id: ftc_baseline, amazon_associates. Omit to list all.",
    )


async def _compliance_check(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    from app.compliance import check_text

    assert isinstance(inp, ComplianceCheckToolInput)
    result = check_text(
        inp.text, content_type=inp.content_type, pack_id=inp.pack_id
    )
    return _ok(result.to_dict())


async def _compliance_rules(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    from app.compliance.rules import list_rule_packs
    from app.compliance import get_rule_pack

    assert isinstance(inp, ComplianceRulesToolInput)
    if inp.pack_id:
        return _ok({"pack": get_rule_pack(inp.pack_id)})
    return _ok({"packs": list_rule_packs()})


_register(
    ToolDef(
        id="draven.compliance_check",
        description=(
            "Scan marketing text for FTC disclosure compliance and affiliate "
            "network rules (Amazon Associates etc.). Returns violations with "
            "severity and specific fixes. Answers 'is this post compliant?'."
        ),
        input_model=ComplianceCheckToolInput,
        risk="low",
        execute=_compliance_check,
    )
)
_register(
    ToolDef(
        id="draven.compliance_rules",
        description=(
            "Explain the compliance rules for a network (FTC baseline, Amazon "
            "Associates). Answers 'what does Amazon require?'."
        ),
        input_model=ComplianceRulesToolInput,
        risk="low",
        execute=_compliance_rules,
    )
)


# ---------------------------------------------------------------------------
# Travel Agency workspace tools (Phase 1: CRM)
# ---------------------------------------------------------------------------


class TravelLeadCreateToolInput(BaseModel):
    destination: str | None = Field(
        default=None, max_length=255, description="Where the traveler wants to go."
    )
    trip_purpose: str | None = Field(default=None, max_length=255)
    budget: float | None = Field(default=None, ge=0)
    date_start: str | None = Field(default=None, description="YYYY-MM-DD")
    date_end: str | None = Field(default=None, description="YYYY-MM-DD")
    party_size: int | None = Field(default=None, ge=1, le=500)
    customer_name: str | None = Field(
        default=None, max_length=255, description="Attach to an existing customer by name."
    )
    source: str | None = Field(default="draven", max_length=64)


class TravelLeadStatusToolInput(BaseModel):
    status: str | None = Field(
        default=None,
        description="Filter: new, qualified, quoted, booked, lost. Omit for all.",
    )
    limit: int = Field(default=10, ge=1, le=50)


class TravelCustomerSearchToolInput(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


def _travel_lead_out(lead: TravelLead) -> dict[str, Any]:
    status = lead.status.value if hasattr(lead.status, "value") else str(lead.status)
    return {
        "id": str(lead.id),
        "destination": lead.destination,
        "trip_purpose": lead.trip_purpose,
        "budget": float(lead.budget) if lead.budget is not None else None,
        "date_start": lead.date_start.isoformat() if lead.date_start else None,
        "date_end": lead.date_end.isoformat() if lead.date_end else None,
        "status": status,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


async def _travel_lead_create(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    assert isinstance(inp, TravelLeadCreateToolInput)
    from datetime import date as _date

    data: dict[str, Any] = {
        "destination": inp.destination,
        "trip_purpose": inp.trip_purpose,
        "source": inp.source or "draven",
        "status": TravelLeadStatus.new,
    }
    if inp.budget is not None:
        from decimal import Decimal

        data["budget"] = Decimal(str(inp.budget))
    for key in ("date_start", "date_end"):
        raw = getattr(inp, key)
        if raw:
            try:
                data[key] = _date.fromisoformat(raw)
            except ValueError:
                return {
                    "ok": False,
                    "error": f"Could not parse {key}={raw!r}; use YYYY-MM-DD.",
                }
    if inp.customer_name:
        like = f"%{inp.customer_name.strip()}%"
        customer = (
            db.query(TravelCustomer)
            .filter(
                TravelCustomer.business_id == user.business_id,
                TravelCustomer.name.ilike(like),
            )
            .first()
        )
        if customer:
            data["customer_id"] = customer.id
    lead = TravelLead(business_id=user.business_id, **data)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    if inp.party_size:
        # Record the party size as a linked trip request so the detail
        # is not lost (Phase 1 has no quotes yet).
        db.add(
            TripRequest(
                business_id=user.business_id,
                lead_id=lead.id,
                customer_id=lead.customer_id,
                party_size=inp.party_size,
                destinations=[inp.destination] if inp.destination else [],
                date_start=lead.date_start,
                date_end=lead.date_end,
            )
        )
        db.commit()
    return _ok({"lead": _travel_lead_out(lead)})


async def _travel_lead_status(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    assert isinstance(inp, TravelLeadStatusToolInput)
    query = (
        db.query(TravelLead)
        .filter(TravelLead.business_id == user.business_id)
        .order_by(TravelLead.created_at.desc())
    )
    if inp.status:
        valid = {s.value for s in TravelLeadStatus}
        if inp.status not in valid:
            return {
                "ok": False,
                "error": f"Unknown status {inp.status!r}; valid: {sorted(valid)}.",
            }
        query = query.filter(TravelLead.status == inp.status)
    rows = query.limit(inp.limit).all()
    return _ok({"count": len(rows), "leads": [_travel_lead_out(r) for r in rows]})


async def _travel_customer_search(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    assert isinstance(inp, TravelCustomerSearchToolInput)
    q = inp.query.strip()
    like = f"%{q}%"
    rows = (
        db.query(TravelCustomer)
        .filter(
            TravelCustomer.business_id == user.business_id,
            or_(
                TravelCustomer.name.ilike(like),
                TravelCustomer.email.ilike(like),
                TravelCustomer.phone.ilike(like),
            ),
        )
        .order_by(TravelCustomer.name.asc())
        .limit(inp.limit)
        .all()
    )
    return _ok(
        {
            "count": len(rows),
            "customers": [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "email": c.email,
                    "phone": c.phone,
                    "type": c.type.value if hasattr(c.type, "value") else str(c.type),
                }
                for c in rows
            ],
        }
    )


_register(
    ToolDef(
        id="travel.lead_create",
        description=(
            "Create a travel lead from natural language: destination, dates, "
            "budget, purpose. Born as status 'new'."
        ),
        input_model=TravelLeadCreateToolInput,
        risk="low",
        execute=_travel_lead_create,
    )
)
_register(
    ToolDef(
        id="travel.lead_status",
        description="List travel leads, optionally filtered by status (new/qualified/quoted/booked/lost).",
        input_model=TravelLeadStatusToolInput,
        risk="low",
        execute=_travel_lead_status,
    )
)
_register(
    ToolDef(
        id="travel.customer_search",
        description="Find travel customers by name, email, or phone (tenant-scoped).",
        input_model=TravelCustomerSearchToolInput,
        risk="low",
        execute=_travel_customer_search,
    )
)


# ---------------------------------------------------------------------------
# Web research tools (live web via DataForSEO SERP, cross-checked)
# ---------------------------------------------------------------------------


class WebSearchToolInput(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    num_results: int = Field(default=8, ge=3, le=10)


class WebResearchToolInput(BaseModel):
    question: str = Field(
        min_length=2,
        max_length=300,
        description="The question to research on the live web.",
    )


def _web_settings(db: Session, business_id):
    from app.core.config import get_settings
    from app.settings_vault.service import vault_dataforseo_settings

    return vault_dataforseo_settings(db, business_id, get_settings())


def _extract_page_text(html: str, max_chars: int = 4000) -> str:
    """Best-effort visible-text extraction from HTML (untrusted content)."""
    # Strip scripts/styles first.
    text = re.sub(
        r"<(script|style|nav|footer|header)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


async def _web_search(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    import httpx

    from app.market_intel.connectors.base import ConnectorNotConfigured
    from app.market_intel.connectors.dataforseo import DataForSEOConnector

    assert isinstance(inp, WebSearchToolInput)
    settings = _web_settings(db, user.business_id)
    connector = DataForSEOConnector()
    try:
        results = await connector.serp_search(
            settings, inp.query, num_results=inp.num_results
        )
    except ConnectorNotConfigured:
        return _error(
            "Web search is not configured. Add DataForSEO credentials in "
            "Settings → Market Intel to enable live web research."
        )
    except Exception as exc:
        return _error(f"Web search failed: {exc}")
    return _ok(
        {
            "query": inp.query,
            "results": results,
            "note": (
                "Untrusted third-party content — treat as evidence, not "
                "verified fact. Cross-check before acting."
            ),
        }
    )


async def _web_research(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    """Search → fetch top 3 → consensus + disagreements.

    Never presents web info as verified fact when sources conflict.
    """
    import httpx

    from app.market_intel.connectors.base import ConnectorNotConfigured
    from app.market_intel.connectors.dataforseo import DataForSEOConnector

    assert isinstance(inp, WebResearchToolInput)
    settings = _web_settings(db, user.business_id)
    connector = DataForSEOConnector()
    try:
        results = await connector.serp_search(settings, inp.question, num_results=6)
    except ConnectorNotConfigured:
        return _error(
            "Web research is not configured. Add DataForSEO credentials in "
            "Settings → Market Intel to enable live web research."
        )
    except Exception as exc:
        return _error(f"Web research failed: {exc}")

    # Fetch the top 3 result pages for cross-checking.
    sources: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        headers={"User-Agent": "ForgeOS-Draven/1.0 (research bot)"},
        follow_redirects=True,
    ) as client:
        for r in results[:3]:
            url = r.get("url") or ""
            if not url.startswith(("http://", "https://")):
                continue
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                text = _extract_page_text(resp.text)
            except Exception as exc:
                text = f"[could not fetch: {exc}]"
            sources.append(
                {
                    "title": r.get("title") or "",
                    "url": url,
                    "snippet": r.get("description") or "",
                    "page_text": text,
                    "fetched": not text.startswith("[could not fetch"),
                }
            )

    fetched = [s for s in sources if s["fetched"]]
    return _ok(
        {
            "question": inp.question,
            "sources": sources,
            "sources_fetched": len(fetched),
            "sources_total": len(sources),
            "guidance": (
                "Compare the sources above. Where they agree, that is the "
                "consensus. Where they conflict or a claim appears in only "
                "one source, flag it as unverified — do NOT present it as "
                "fact. Say explicitly when sources disagree."
            ),
        }
    )


_register(
    ToolDef(
        id="draven.web_search",
        description=(
            "Live web search (Google via DataForSEO). Returns titles, URLs, "
            "and snippets. Untrusted content — cross-check before acting."
        ),
        input_model=WebSearchToolInput,
        risk="low",
        execute=_web_search,
    )
)
_register(
    ToolDef(
        id="draven.web_research",
        description=(
            "Deep web research: searches the live web, fetches the top 3 "
            "result pages, and returns them for consensus analysis. Flags "
            "agreements and conflicts between sources — never presents "
            "conflicted info as verified fact."
        ),
        input_model=WebResearchToolInput,
        risk="low",
        execute=_web_research,
    )
)


# ---------------------------------------------------------------------------
# Viator affiliate tools (tours & experiences product search, import, ads)
# ---------------------------------------------------------------------------


class ViatorProductSearchInput(BaseModel):
    destination_id: int = Field(
        description="Viator destination ID (e.g. 77 for Rome, 732 for Paris).",
    )
    count: int = Field(default=10, ge=1, le=25)
    currency: str = Field(default="USD", max_length=3)
    keyword: str | None = Field(
        default=None,
        max_length=100,
        description="Optional filter: only products whose title/description matches.",
    )


class ViatorProductImportInput(BaseModel):
    destination_id: int = Field(
        description="Viator destination ID to import products from.",
    )
    count: int = Field(default=10, ge=1, le=25)
    currency: str = Field(default="USD", max_length=3)


class ViatorGenerateAdsInput(BaseModel):
    program_ids: list[str] | None = Field(
        default=None,
        description="Affiliate program UUIDs to generate ads for. Omit to target all imported Viator programs without ads.",
    )


def _viator_settings(db: Session, business_id):
    from app.core.config import get_settings

    return get_settings()


async def _viator_product_search(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    from app.affiliate.connectors.viator import (
        ViatorAuthError,
        ViatorError,
        ViatorNotConfigured,
        connector_for_business,
    )

    assert isinstance(inp, ViatorProductSearchInput)
    settings = _viator_settings(db, user.business_id)
    try:
        connector = connector_for_business(db, user.business_id, settings)
    except ViatorNotConfigured as exc:
        return _error(str(exc))
    try:
        products = await connector.search_products(
            inp.destination_id, count=inp.count, currency=inp.currency
        )
    except ViatorAuthError as exc:
        return _error(str(exc))
    except ViatorError as exc:
        return _error(f"Viator search failed: {exc}")
    if inp.keyword:
        kw = inp.keyword.lower()
        products = [
            p
            for p in products
            if kw in (p.get("title") or "").lower()
            or kw in (p.get("description") or "").lower()
        ]
    return _ok(
        {
            "destination_id": inp.destination_id,
            "count": len(products),
            "currency": inp.currency,
            "products": products,
            "note": (
                "Live Viator Partner API results (affiliate productUrl "
                "included). Prices/availability are as-reported — revalidate "
                "before quoting to customers."
            ),
        }
    )


async def _viator_product_import(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    from app.affiliate import viator_service
    from app.affiliate.connectors.viator import (
        ViatorAuthError,
        ViatorError,
        ViatorNotConfigured,
        connector_for_business,
    )

    assert isinstance(inp, ViatorProductImportInput)
    settings = _viator_settings(db, user.business_id)
    try:
        connector = connector_for_business(db, user.business_id, settings)
    except ViatorNotConfigured as exc:
        return _error(str(exc))
    try:
        products = await connector.search_products(
            inp.destination_id, count=inp.count, currency=inp.currency
        )
    except ViatorAuthError as exc:
        return _error(str(exc))
    except ViatorError as exc:
        return _error(f"Viator search failed: {exc}")
    imported = viator_service.import_products(
        db, user.business_id, products, actor_id=user.id
    )
    new = sum(1 for r in imported if r["imported"])
    return _ok(
        {
            "destination_id": inp.destination_id,
            "products_found": len(products),
            "programs_new": new,
            "programs_total": len(imported),
            "items": imported,
            "note": (
                "Imported as affiliate programs (network=viator) with "
                "trackable /r/{slug} links. Nothing published externally."
            ),
        }
    )


async def _viator_generate_ads(
    db: Session, user: User, inp: BaseModel
) -> dict[str, Any]:
    import uuid as _uuid

    from app.affiliate import viator_service

    assert isinstance(inp, ViatorGenerateAdsInput)
    program_ids = None
    if inp.program_ids:
        try:
            program_ids = [_uuid.UUID(pid) for pid in inp.program_ids]
        except ValueError:
            return _error("program_ids must be valid UUIDs")
    ads = viator_service.generate_ads(
        db, user.business_id, program_ids, actor_id=user.id
    )
    flagged = sum(1 for a in ads if a["compliance_violations"])
    return _ok(
        {
            "ads_created": len(ads),
            "compliance_flagged": flagged,
            "ads": ads,
            "note": (
                "All ads are DRAFT with FTC disclosure included; "
                f"{flagged} flagged for human compliance review. "
                "Nothing published or sent."
            ),
        }
    )


_register(
    ToolDef(
        id="viator.product_search",
        description=(
            "Search Viator tours & experiences by destination ID (live "
            "Partner API). Returns title, price, rating, affiliate productUrl. "
            "Answers 'find viator tours in Rome'."
        ),
        input_model=ViatorProductSearchInput,
        risk="low",
        execute=_viator_product_search,
    )
)
_register(
    ToolDef(
        id="viator.product_import",
        description=(
            "Import Viator products as affiliate programs with trackable "
            "/r/{slug} links (idempotent — skips already-imported). "
            "Answers 'import viator products'."
        ),
        input_model=ViatorProductImportInput,
        risk="low",
        execute=_viator_product_import,
    )
)
_register(
    ToolDef(
        id="viator.generate_ads",
        description=(
            "Generate DRAFT ad copy (headline, body, CTA, FTC disclosure) "
            "for imported Viator products. Ads are never auto-published; "
            "compliance issues are flagged for review. Answers 'create ads "
            "for viator products'."
        ),
        input_model=ViatorGenerateAdsInput,
        risk="low",
        execute=_viator_generate_ads,
    )
)


# ---------------------------------------------------------------------------
# Execution + audit
# ---------------------------------------------------------------------------


def _summarize_output(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "ok":
        out = result.get("output", {})
        if isinstance(out, dict):
            count = out.get("count")
            if count is not None:
                return f"ok, count={count}"
            keys = ",".join(sorted(out.keys())[:6])
            return f"ok, keys={keys}"
        return "ok"
    if status == "approval_required":
        action = (result.get("approval") or {}).get("action", "?")
        return f"approval_required: {action}"
    return f"error: {str(result.get('error'))[:200]}"


async def execute_tool(
    tool_id: str,
    db: Session,
    user: User,
    raw_input: dict[str, Any] | None,
    agent_id: str | None = None,
    swarm_run_id: Any | None = None,
) -> dict[str, Any]:
    """Validate, execute (or hold for approval), and audit one tool call.

    Returns a result dict with ``tool``, ``risk``, ``status``,
    ``duration_ms`` plus either ``output`` / ``approval`` / ``error``.
    ``agent_id`` / ``swarm_run_id`` attribute the audit row to a swarm
    agent run (None for direct chat calls).
    """
    started = time.perf_counter()
    tool = TOOLS.get(tool_id)
    if tool is None:
        result: dict[str, Any] = _error(f"unknown tool: {tool_id}")
        risk: ToolRisk = "low"
    else:
        risk = tool.risk
        try:
            validated = tool.input_model(**(raw_input or {}))
        except Exception as exc:
            result = _error(f"invalid input: {exc}")
        else:
            try:
                assert tool.execute is not None
                result = await tool.execute(db, user, validated)
            except Exception as exc:  # never let a tool crash the chat
                result = _error(f"tool execution failed: {exc}")

    duration_ms = int((time.perf_counter() - started) * 1000)
    write_audit_row(
        db, user, tool_id, risk, raw_input or {}, result, duration_ms,
        agent_id=agent_id, swarm_run_id=swarm_run_id,
    )

    return {
        "tool": tool_id,
        "risk": risk,
        "status": result.get("status"),
        "duration_ms": duration_ms,
        **{k: v for k, v in result.items() if k != "status"},
    }


def write_audit_row(
    db: Session,
    user: User,
    tool_id: str,
    risk: ToolRisk,
    raw_input: dict[str, Any],
    result: dict[str, Any],
    duration_ms: int,
    agent_id: str | None = None,
    swarm_run_id: Any | None = None,
) -> None:
    """Persist one ``draven_tool_runs`` audit row (best-effort, never raises)."""
    try:
        db.add(
            DravenToolRun(
                business_id=user.business_id,
                user_id=user.id,
                tool=tool_id,
                input=raw_input or {},
                output_summary=_summarize_output(result)[:2000],
                risk=risk,
                status=result.get("status", "error"),
                duration_ms=duration_ms,
                agent_id=agent_id,
                swarm_run_id=swarm_run_id,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


# ---------------------------------------------------------------------------
# Intent routing (keyword rules — no LLM needed)
# ---------------------------------------------------------------------------


def _extract_search_query(message: str) -> str | None:
    """Best-effort contact query: quoted text, or words after find/search/for."""
    m = re.search(r"['\"]([^'\"]{2,80})['\"]", message)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"\b(?:find|search|look\s+up|look\s+for)\s+([a-zA-Z0-9@._\- ]{2,60})",
        message,
        re.IGNORECASE,
    )
    if m:
        q = m.group(1).strip()
        q = re.sub(r"^(?:contact|contacts)\s+", "", q, flags=re.IGNORECASE).strip()
        return q or None
    return None


#: Well-known Viator destination IDs for keyword routing. Users can also
#: pass a numeric destination ID directly ("destination 77").
_VIATOR_DESTINATIONS: dict[str, int] = {
    "rome": 77,
    "paris": 732,
    "london": 737,
    "barcelona": 562,
    "new york": 687,
    "nyc": 687,
    "las vegas": 684,
    "orlando": 685,
    "los angeles": 686,
    "miami": 689,
    "san francisco": 690,
    "tokyo": 1032,
    "kyoto": 1033,
    "bali": 1042,
    "dubai": 1210,
    "amsterdam": 525,
    "athens": 578,
    "madrid": 572,
    "lisbon": 573,
    "florence": 73,
    "venice": 74,
    "milan": 75,
    "naples": 76,
    "berlin": 545,
    "prague": 546,
    "vienna": 547,
    "budapest": 548,
    "sydney": 1051,
    "melbourne": 1052,
    "cairo": 1205,
    "marrakech": 1206,
    "cancun": 700,
    "honolulu": 696,
    "hawaii": 696,
}


def _extract_viator_destination(message: str) -> int | None:
    """Best-effort Viator destination ID: explicit number or known city."""
    m = re.search(r"destinations?\s*[:#]?\s*(\d{1,6})", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    lowered = message.lower()
    for name, did in _VIATOR_DESTINATIONS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return did
    return None


def _extract_viator_keyword(message: str) -> str | None:
    """Best-effort keyword: quoted text, or words after tour/activity nouns."""
    m = re.search(r"['\"]([^'\"]{2,60})['\"]", message)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"\b(?:tours?|activities|experiences?)\b\s+(?:for\s+)?([a-zA-Z ]{2,40})",
        message,
        re.IGNORECASE,
    )
    if m and m.group(1):
        kw = m.group(1).strip()
        kw = re.sub(r"^in\s+", "", kw, flags=re.IGNORECASE).strip()
        # Don't treat a city name as a keyword (it's the destination).
        if kw and kw.lower() not in _VIATOR_DESTINATIONS:
            return kw
    return None


def _extract_name_fragment(message: str, keywords: list[str]) -> str | None:
    """Words following e.g. 'pause ... campaign' / 'approve ... asset'."""
    for kw in keywords:
        m = re.search(
            rf"\b{kw}\b\s+(?:the\s+)?([a-zA-Z0-9 _\-']{{2,60}}?)(?:\s+(?:campaign|asset))?\s*$",
            message,
            re.IGNORECASE,
        )
        if m:
            frag = m.group(1).strip().strip("'\"")
            if frag and frag.lower() not in ("it", "this", "that", "the"):
                return frag
    return None


def _extract_market_params(message: str) -> dict[str, Any]:
    """Best-effort research params from natural language.

    Understands: result counts ("find 20 products"), month names,
    years, markets ("in the US", "United States", "UK"), ticket tiers
    ("low-ticket", "high-ticket"), and a trailing category/question focus.
    Everything not found is left for normalize_params defaults.
    """
    from app.market_intel.pipeline import MONTHS

    params: dict[str, Any] = {}
    msg = message.lower()

    m = re.search(r"\b(?:find|show|get|list|compare)\s+(\d{1,3})\b", msg)
    if m:
        params["max_results"] = max(1, min(int(m.group(1)), 100))

    for name, num in MONTHS.items():
        if re.search(rf"\b{name}\b", msg):
            params["target_month"] = num
            break
    m = re.search(r"\b(20\d{2})\b", msg)
    if m:
        params["target_year"] = int(m.group(1))

    if re.search(r"\bunited states\b|\bthe us\b|\bin us\b|\bu\.s\.", msg):
        params["market"] = "US"
    elif re.search(r"\bunited kingdom\b|\buk\b", msg):
        params["market"] = "GB"
    elif re.search(r"\bcanada\b", msg):
        params["market"] = "CA"
    elif re.search(r"\baustralia\b", msg):
        params["market"] = "AU"
    elif re.search(r"\bgermany\b", msg):
        params["market"] = "DE"

    if re.search(r"low[- ]ticket", msg):
        params["ticket_tier"] = "low"
    elif re.search(r"high[- ]ticket", msg):
        params["ticket_tier"] = "high"
    elif re.search(r"mid[- ]ticket", msg):
        params["ticket_tier"] = "mid"

    # Category: text after "in <category>" / "for <category>" that is not a
    # month, year, or market phrase.
    m = re.search(
        r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z &\-']{2,40}?)(?:\s+products?|\s+items?|\s+opportunit|\s*$)",
        message,
        re.IGNORECASE,
    )
    if m:
        frag = m.group(1).strip()
        if frag.lower() not in (
            "the us", "us", "united states", "november", "december",
            "october", "september", "general",
        ) and not re.search(r"\b(20\d{2})\b", frag):
            params["category"] = frag
    # Category: "<words> products" directly after find/show (e.g.
    # "find home fitness products likely to sell in November").
    if "category" not in params:
        m = re.search(
            r"\b(?:find|show|get|list|compare)\s+(?:\d{1,3}\s+)?"
            r"([a-zA-Z][a-zA-Z &\-']{2,40}?)\s+(?:products?|gadgets?|items?)\b",
            message,
            re.IGNORECASE,
        )
        if m:
            frag = m.group(1).strip()
            first = frag.split()[0].lower() if frag.split() else ""
            if first not in (
                "me", "you", "my", "your", "the", "some", "those",
                "these", "us", "all",
            ) and frag.lower() not in ("some", "good", "best", "top", "new"):
                params["category"] = frag
    return params


def route_intent(message: str) -> list[tuple[str, dict[str, Any]]]:
    """Map a message to candidate (tool_id, input) pairs.

    A message may map to multiple tools. Returns [] when nothing matches.
    """
    msg = message.lower()
    routed: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def add(tool_id: str, inp: dict[str, Any]) -> None:
        if tool_id not in seen:
            seen.add(tool_id)
            routed.append((tool_id, inp))

    # High-risk intents first (approval-gated, never execute).
    if "pause" in msg and "campaign" in msg:
        name = _extract_name_fragment(message, ["pause"])
        add("draven.campaign_pause", {"campaign_name": name} if name else {})
    if "approv" in msg and "asset" in msg:
        name = _extract_name_fragment(message, ["approve"])
        add("draven.asset_approve", {"asset_title": name} if name else {})

    # Alpha workflow intents (before generic approval/contact rules so the
    # lead-to-follow-up verbs win).
    _email_in_msg = re.search(
        r"[\w.+-]+@[\w-]+\.[\w.]+", message
    )
    _email_hint = _email_in_msg.group(0) if _email_in_msg else None

    if re.search(r"process (this |the )?lead|add (a |the )?lead|new lead|"
                 r"intake.*lead|create (a |the )?lead", msg):
        add("alpha.lead_intake", {"email": _email_hint} if _email_hint else {})
    if re.search(r"qualify (this |the |that )?lead|qualify lead", msg):
        add("alpha.qualify_lead", {"email": _email_hint} if _email_hint else {})
    if re.search(r"draft (a |the )?follow[- ]?up|prepare (a |the )?follow[- ]?up|"
                 r"follow[- ]?up (draft|for)|write (a |the )?follow[- ]?up", msg):
        add("alpha.prepare_followup", {"email": _email_hint} if _email_hint else {})
    if re.search(r"approve (the |this |that )?follow[- ]?up", msg):
        add("alpha.approve_followup", {"email": _email_hint} if _email_hint else {})
    if re.search(r"(workflow|run|lead) status|status of.*(run|workflow|lead)|"
                 r"where is (the |this )?lead", msg):
        add("alpha.run_status", {"email": _email_hint} if _email_hint else {})

    # Market intelligence intents (before generic search — a "find N
    # products" ask is market research, not a contact search).
    if re.search(
        r"product|opportunit|market research|sell (well|in)|trending products|"
        r"niche|seasonal products",
        msg,
    ) and re.search(
        r"find|show|get|list|compare|research|discover|what.*sell|which.*sell",
        msg,
    ) and not re.search(r"top|best|strongest", msg):
        add("market.research_start", _extract_market_params(message))
    if re.search(r"research (job |run )?(status|progress)", msg):
        add("market.research_status", {})
    if re.search(r"top opportunit|best opportunit|strongest opportunit", msg):
        add("market.top_opportunities", {})

    # Read-only intents.
    # Skip the generic review queue when the message is really about the
    # weekly content plan — the autopilot rules below own that.
    if re.search(r"approv|review|pending|in[- ]review|queue", msg) and not re.search(
        r"weekly plan|content plan", msg
    ):
        add("draven.approvals_pending", {})
        if re.search(r"detail|show me|full", msg):
            add("draven.assets_pending_review", {})
    if "campaign" in msg and re.search(
        r"status|how are|doing|running|list|all", msg
    ):
        add("draven.campaigns_status", {})
    if re.search(r"performance|analytics|metrics|stats|statistic|report", msg):
        add("draven.analytics_summary", {})
    # Travel agency workspace — "new travel lead", "show my travel leads",
    # "find travel customer …". Placed before the generic contact search so
    # travel-specific verbs win.
    if re.search(r"\btravel\b", msg):
        if re.search(
            r"\bnew\b.*\bleads?\b|\bleads?\b.*\b(new|create|add)\b|create.*travel.*lead|add.*travel.*lead",
            msg,
        ):
            dest = _extract_name_fragment(message, ["to", "for"])
            add(
                "travel.lead_create",
                {"destination": dest} if dest else {},
            )
        elif re.search(r"\bleads?\b", msg) and re.search(
            r"\blist\b|\bshow\b|\bmy\b|\ball\b|\bstatus\b", msg
        ):
            status = None
            for s in ("new", "qualified", "quoted", "booked", "lost"):
                if re.search(rf"\b{s}\b", msg):
                    status = s
                    break
            add("travel.lead_status", {"status": status} if status else {})
        elif re.search(r"\bcustomers?\b", msg) and re.search(
            r"\bfind\b|\bsearch\b|\blook\b|\bshow\b", msg
        ):
            query = _extract_search_query(message)
            if query:
                query = re.sub(
                    r"^travel\s+customers?\s+", "", query, flags=re.IGNORECASE
                ).strip()
            add("travel.customer_search", {"query": query} if query else {"query": ""})

    # Viator affiliate intents — "find viator tours in Rome",
    # "import viator products", "create ads for viator products". Placed
    # before the generic contact search so viator verbs win.
    if "viator" in msg:
        if re.search(r"\bimport\b", msg):
            dest = _extract_viator_destination(message)
            add(
                "viator.product_import",
                {"destination_id": dest} if dest else {},
            )
        elif re.search(r"\bad\b|\bads\b|\bcopy\b|\bpromot", msg):
            add("viator.generate_ads", {})
        elif re.search(
            r"\bfind\b|\bsearch\b|\bshow\b|\blist\b|\btour\b|\bactivit|\bexperience",
            msg,
        ):
            dest = _extract_viator_destination(message)
            kw = _extract_viator_keyword(message)
            inp: dict[str, Any] = {}
            if dest:
                inp["destination_id"] = dest
            if kw:
                inp["keyword"] = kw
            add("viator.product_search", inp)

    # Skip contact search when the message is really a market-research ask
    # ("find 20 products…", "research status") — the market intent owns it —
    # or an explicit contact LISTING ("list my contacts").
    if "market.research_start" not in seen and "market.research_status" not in seen \
            and "draven.contacts_list" not in seen \
            and "travel.lead_create" not in seen \
            and "travel.lead_status" not in seen \
            and "travel.customer_search" not in seen \
            and "viator.product_search" not in seen \
            and "viator.product_import" not in seen \
            and "viator.generate_ads" not in seen \
            and re.search(
        r"\bcontact\b|find|search|look\s+up|who is|who's", msg
    ):
        query = _extract_search_query(message)
        add("draven.contacts_search", {"query": query} if query else {})
    if re.search(r"\bsummary\b|today|business|overview|how is|brief", msg):
        add("draven.business_summary", {})
    if re.search(r"autopilot|weekly plan|content plan", msg):
        add("draven.autopilot_status", {})

    # --- Voice parity intents (brand kits, contacts, templates, assets,
    # --- campaigns, autopilot writes, affiliates, analytics, interview,
    # --- ops, business) ----------------------------------------------

    # Brand kits
    if re.search(r"brand[- ]?kit", msg):
        if re.search(r"\bcreate\b|\bnew\b|\bmake\b", msg):
            name = _extract_name_fragment(message, ["create", "make", "new"])
            add(
                "draven.brandkit_create",
                {"name": name} if name else {},
            )
        elif re.search(r"\bupdate\b|\bchange\b|\bedit\b", msg):
            name = _extract_name_fragment(message, ["update", "change", "edit"])
            add(
                "draven.brandkit_update",
                {"name": name} if name else {},
            )
        else:
            add("draven.brandkit_list", {})

    # Contacts — writes first (before the generic search above already ran,
    # but listing is handled here via the seen-guard above).
    if re.search(r"\blist\b.*\bcontacts?\b|\bcontacts?\b.*\blist\b|\bmy contacts\b|\ball contacts\b", msg):
        add("draven.contacts_list", {})
    if re.search(r"\badd\b.*\bcontact\b|\bnew contact\b|\bcreate\b.*\bcontact\b", msg):
        add("draven.contact_create", {"email": _email_hint} if _email_hint else {})
    if re.search(r"\bupdate\b.*\bcontact\b|\bedit\b.*\bcontact\b", msg):
        frag = _extract_name_fragment(message, ["update", "edit"])
        add(
            "draven.contact_update",
            {"email": frag or _email_hint} if (frag or _email_hint) else {},
        )
    if re.search(r"\bdelete\b.*\bcontact\b|\bremove\b.*\bcontact\b", msg):
        frag = _extract_name_fragment(message, ["delete", "remove"])
        add(
            "draven.contact_delete",
            {"email": frag or _email_hint} if (frag or _email_hint) else {},
        )
    if "consent" in msg and "contact" in msg:
        frag = _extract_name_fragment(message, ["consent"])
        channel = "sms" if re.search(r"\bsms\b", msg) else "email"
        granted = not re.search(r"\brevoke\b|\bremove\b|\bwithdraw\b|\boff\b", msg)
        inp: dict[str, Any] = {"channel": channel, "granted": granted}
        if frag or _email_hint:
            inp["email"] = frag or _email_hint
        add("draven.contact_consent", inp)

    # Templates
    if "template" in msg:
        if re.search(r"\bcreate\b|\bnew\b|\bmake\b", msg):
            name = _extract_name_fragment(message, ["create", "make", "new"])
            add("draven.template_create", {"name": name} if name else {})
        elif re.search(r"\bpreview\b|\brender\b", msg):
            name = _extract_name_fragment(message, ["preview", "render"])
            vars_ = dict(
                re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([^\s,;]+)", message)
            )
            add(
                "draven.template_preview",
                {**( {"name": name} if name else {}), "variables": vars_},
            )
        elif re.search(r"\bupdate\b|\bchange\b|\bedit\b", msg):
            name = _extract_name_fragment(message, ["update", "change", "edit"])
            add("draven.template_update", {"name": name} if name else {})
        elif re.search(r"\bdelete\b|\bremove\b", msg):
            name = _extract_name_fragment(message, ["delete", "remove"])
            add(
                "draven.template_delete",
                {"name": name} if name else {},
            )
        elif re.search(r"\blist\b|\ball\b|\bmy\b|\bshow\b", msg):
            add("draven.template_list", {})

    # Assets — generate/submit/reject/versions/list (approve already above).
    if re.search(r"\bgenerate\b", msg) and re.search(
        r"asset|email|sms|copy|post|blog|ad\b", msg
    ):
        kind = "email_copy"
        if re.search(r"\bsms\b|\btext\b", msg):
            kind = "sms"
        elif re.search(r"\bsocial\b|\bpost\b", msg):
            kind = "social_post"
        elif re.search(r"\bblog\b", msg):
            kind = "blog"
        elif re.search(r"\bad\b|\badvert", msg):
            kind = "ad"
        m = re.search(r"['\"]([^'\"]{2,120})['\"]", message)
        title = m.group(1).strip() if m else None
        if not title:
            m2 = re.search(
                r"\babout\s+([a-zA-Z0-9 ,&\-']{2,80}?)(?:\s*$|\.)",
                message,
                re.IGNORECASE,
            )
            title = m2.group(1).strip() if m2 else None
        add(
            "draven.asset_generate",
            {**( {"title": title} if title else {}), "kind": kind},
        )
    if re.search(r"\bsubmit\b", msg) and "asset" in msg:
        name = _extract_name_fragment(message, ["submit"])
        add("draven.asset_submit", {"asset_title": name} if name else {})
    if re.search(r"\breject\b", msg) and "asset" in msg:
        name = _extract_name_fragment(message, ["reject"])
        add("draven.asset_reject", {"asset_title": name} if name else {"reason": "rejected via voice"})
    if re.search(r"\bversions?\b", msg) and "asset" in msg:
        name = _extract_name_fragment(message, ["versions", "version"])
        add("draven.asset_versions", {"asset_title": name} if name else {})
    if re.search(r"\bmy assets\b|\ball assets\b|\blist\b.*\bassets?\b|\bassets?\b.*\blist\b", msg):
        add("draven.assets_list", {})

    # Campaigns — create/update/steps/launch/enrollments.
    if re.search(r"\blaunch\b", msg) and "campaign" in msg:
        name = _extract_name_fragment(message, ["launch"])
        add("draven.campaign_launch", {"campaign_name": name} if name else {})
    if re.search(r"\bcreate\b.*\bcampaign\b|\bnew campaign\b", msg):
        name = _extract_name_fragment(message, ["create", "new"])
        add("draven.campaign_create", {"name": name} if name else {})
    # Drip/automation phrasing: "create a drip campaign", "set up a welcome
    # sequence", "build a nurture sequence" — all map to campaign_create.
    if re.search(
        r"\bdrip\b|\bwelcome sequence\b|\bnurture sequence\b|\bfollow[- ]?up sequence\b|"
        r"\bemail sequence\b|\bautomation\b.*\bcampaign\b|\bcampaign\b.*\bautomation\b",
        msg,
    ) and "draven.campaign_create" not in seen:
        name = _extract_name_fragment(
            message, ["create", "set", "setup", "set up", "build", "make", "new"]
        )
        add("draven.campaign_create", {"name": name} if name else {})
    if re.search(r"\bupdate\b.*\bcampaign\b|\bedit\b.*\bcampaign\b", msg):
        name = _extract_name_fragment(message, ["update", "edit"])
        add(
            "draven.campaign_update",
            {"campaign_name": name} if name else {},
        )
    if re.search(r"\bdetail\b|\bshow\b.*\bcampaign\b", msg) and "campaign" in msg \
            and not re.search(r"\blaunch\b|\bpause\b|\bcreate\b|\bupdate\b", msg):
        name = _extract_name_fragment(message, ["show", "detail"])
        add("draven.campaign_get", {"campaign_name": name} if name else {})
    # Campaign names usually precede the word ("the welcome campaign").
    _before_campaign = re.search(
        r"(?:the\s+)?([a-zA-Z0-9][a-zA-Z0-9 _\-']{1,50}?)\s+campaign\b",
        message,
        re.IGNORECASE,
    )
    _campaign_frag = (
        _before_campaign.group(1).strip() if _before_campaign else None
    )
    if re.search(r"\badd\b.*\bsteps?\b|\bnew step\b", msg) and "campaign" in msg:
        add(
            "draven.campaign_steps_add",
            (
                {"campaign_name": _campaign_frag, "steps": []}
                if _campaign_frag
                else {"steps": []}
            ),
        )
    if re.search(r"\benrollment", msg) and "campaign" in msg:
        add(
            "draven.campaign_enrollments",
            {"campaign_name": _campaign_frag} if _campaign_frag else {},
        )
    # Enrolling contacts: "enroll contacts in the welcome campaign",
    # "add these contacts to the campaign" — maps to campaign_enroll.
    if re.search(
        r"\benroll\b.*\bcontacts?\b|\badd\b.*\bcontacts?\b.*\bto\b.*\bcampaign\b|"
        r"\bsubscribe\b.*\bcontacts?\b.*\bcampaign\b",
        msg,
    ):
        query = _extract_search_query(message)
        inp_enroll: dict[str, Any] = {}
        if _campaign_frag:
            inp_enroll["campaign_name"] = _campaign_frag
        if query:
            inp_enroll["contact_query"] = query
        add("draven.campaign_enroll", inp_enroll)

    # Autopilot writes.
    if re.search(r"\bupdate\b.*\bautopilot\b|\bautopilot\b.*\bsettings\b", msg):
        inp_ap: dict[str, Any] = {}
        if re.search(r"auto[- ]?approve\s+(on|enable|enabled|true)", msg):
            inp_ap["auto_approve"] = True
        elif re.search(r"auto[- ]?approve\s+(off|disable|disabled|false)", msg):
            inp_ap["auto_approve"] = False
        m_cap = re.search(r"(?:daily\s+)?cap(?:\s+of)?\s+(\d{1,5})", msg)
        if m_cap:
            inp_ap["daily_send_cap"] = int(m_cap.group(1))
        add("draven.autopilot_update", inp_ap)
    if re.search(r"\bapprove\b", msg) and re.search(
        r"weekly plan|content plan", msg
    ):
        add("draven.autopilot_plan_approve", {})

    # Affiliates.
    if "affiliate" in msg:
        if re.search(r"\bearning|revenue|commission", msg):
            add("draven.affiliate_earnings", {})
        elif re.search(r"\blink", msg):
            if re.search(r"\bcreate\b|\bnew\b|\bmake\b|\badd\b", msg):
                add("draven.affiliate_link_create", {})
            elif re.search(r"\bupdate\b|\bedit\b", msg):
                slug = _extract_name_fragment(message, ["update", "edit"])
                add(
                    "draven.affiliate_link_update",
                    {"slug": slug} if slug else {},
                )
            elif re.search(r"\bdelete\b|\bremove\b", msg):
                slug = _extract_name_fragment(message, ["delete", "remove"])
                add(
                    "draven.affiliate_link_delete",
                    {"slug": slug} if slug else {},
                )
            else:
                add("draven.affiliate_links_list", {})
        elif re.search(r"\bprogram", msg):
            if re.search(r"\bcreate\b|\bnew\b|\bmake\b|\badd\b", msg):
                name = _extract_name_fragment(message, ["create", "make", "new", "add"])
                add(
                    "draven.affiliate_program_create",
                    {"name": name} if name else {},
                )
            elif re.search(r"\bupdate\b|\bedit\b", msg):
                name = _extract_name_fragment(message, ["update", "edit"])
                add(
                    "draven.affiliate_program_update",
                    {"name": name} if name else {},
                )
            elif re.search(r"\bdelete\b|\bremove\b", msg):
                name = _extract_name_fragment(message, ["delete", "remove"])
                add(
                    "draven.affiliate_program_delete",
                    {"name": name} if name else {},
                )
            else:
                add("draven.affiliate_programs_list", {})

    # Analytics extras.
    if "funnel" in msg and "campaign" in msg:
        add(
            "draven.analytics_funnel",
            {"campaign_name": _campaign_frag} if _campaign_frag else {},
        )
    if re.search(r"\bweekly summary\b", msg):
        add("draven.analytics_weekly_summary", {})

    # Interview.
    if re.search(r"\binterview\b", msg):
        if re.search(r"\bstart\b|\bbegin\b", msg):
            add("draven.interview_start", {})
        else:
            add("draven.interview_status", {})

    # Ops.
    if re.search(r"\bmission control\b|\bactivity feed\b|\bops activity\b", msg):
        add("draven.ops_activity", {})
    if re.search(r"\bbrain\b|\bknowledge graph\b", msg):
        add("draven.ops_brain", {})

    # Business.
    if re.search(r"\bupdate\b.*\bbusiness\b|\brename\b.*\bbusiness\b", msg):
        name = _extract_name_fragment(message, ["update", "rename"])
        add("draven.business_update", {"name": name} if name else {})
    elif re.search(r"\bmy business\b|\bbusiness profile\b|\bbusiness settings\b", msg):
        add("draven.business_get", {})

    # Compliance — "is this post FTC compliant?", "what does Amazon require?"
    if re.search(
        r"\bftc\b|\bcompliant\b|\bcompliance\b|\bdisclosure\b|"
        r"\baffiliate\b.*\b(require|rule|policy)|"
        r"\bamazon\b.*\b(require|rule|disclosure)",
        msg,
    ):
        if re.search(r"\bwhat\b.*\brequire|\brequirement|\brules\b", msg) or re.search(
            r"\bamazon\b.*\b(require|rule|disclosure)", msg
        ):
            pack = "amazon_associates" if "amazon" in msg else None
            add("draven.compliance_rules", {"pack_id": pack} if pack else {})
        else:
            quoted = re.search(r"['\"]([^'\"]{10,2000})['\"]", message)
            add(
                "draven.compliance_check",
                {"text": quoted.group(1)} if quoted else {},
            )
    # Web research — "search the web for…", "look up…", "research…"
    if re.search(
        r"\bsearch the web\b|\blook up\b|\bresearch\b|\bwhat'?s happening\b|\blatest\b.*\bnews\b",
        msg,
    ) and "draven.web_research" not in seen:
        q = _extract_search_query(message)
        add("draven.web_research", {"question": q} if q else {})

    return routed


def tool_json_schema(tool_id: str) -> dict[str, Any]:
    """JSON Schema for a tool's input model (for GET /draven/tools)."""
    tool = TOOLS[tool_id]
    return tool.input_model.model_json_schema()



# ---------------------------------------------------------------------------
# Voice-parity tool pack (app/draven_tools_parity.py). Imported last so the
# parity tools register into the same TOOLS registry above.
# ---------------------------------------------------------------------------
from app.draven_tools_parity import *  # noqa: F401,F403
