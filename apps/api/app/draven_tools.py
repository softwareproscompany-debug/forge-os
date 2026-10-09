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
    Send,
    Template,
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
) -> dict[str, Any]:
    """Validate, execute (or hold for approval), and audit one tool call.

    Returns a result dict with ``tool``, ``risk``, ``status``,
    ``duration_ms`` plus either ``output`` / ``approval`` / ``error``.
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
    write_audit_row(db, user, tool_id, risk, raw_input or {}, result, duration_ms)

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

    # Read-only intents.
    if re.search(r"approv|review|pending|in[- ]review|queue", msg):
        add("draven.approvals_pending", {})
        if re.search(r"detail|show me|full", msg):
            add("draven.assets_pending_review", {})
    if "campaign" in msg and re.search(
        r"status|how are|doing|running|list|all", msg
    ):
        add("draven.campaigns_status", {})
    if re.search(r"performance|analytics|metrics|stats|statistic|report", msg):
        add("draven.analytics_summary", {})
    if re.search(r"\bcontact\b|find|search|look\s+up|who is|who's", msg):
        query = _extract_search_query(message)
        add("draven.contacts_search", {"query": query} if query else {})
    if re.search(r"\bsummary\b|today|business|overview|how is|brief", msg):
        add("draven.business_summary", {})
    if re.search(r"autopilot|weekly plan|content plan", msg):
        add("draven.autopilot_status", {})

    return routed


def tool_json_schema(tool_id: str) -> dict[str, Any]:
    """JSON Schema for a tool's input model (for GET /draven/tools)."""
    tool = TOOLS[tool_id]
    return tool.input_model.model_json_schema()
