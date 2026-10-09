"""FTC + affiliate-network compliance engine API.

``POST /compliance/check`` scans marketing text against a versioned rule
pack and returns violations with severity + fix suggestions.
``GET /compliance/issues`` lists flags raised by the checker and the daily
compliance bot (human review queue — bots never auto-delete or auto-edit).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from forge_db.models import ComplianceIssue as ComplianceIssueRow

from app import schemas
from app.compliance import check_text, get_rule_pack
from app.compliance.rules import DEFAULT_PACK_ID, list_rule_packs
from app.core.deps import CurrentUser, DbSession, paginate, scoped

router = APIRouter(prefix="/compliance", tags=["compliance"])

_CONTENT_TYPES = ("social_post", "email", "product_review", "ad_hoc")


class ComplianceCheckIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    content_type: str = Field(default="social_post")
    pack_id: str | None = Field(default=None, description="Rule pack id; defaults to FTC baseline")


class ComplianceCheckOut(BaseModel):
    pack_id: str
    pack_version: str
    content_type: str
    affiliate_detected: bool
    affiliate_signals: list[str]
    compliant: bool
    violations: list[dict[str, Any]]
    notes: list[str]


@router.post("/check", response_model=ComplianceCheckOut)
def check_compliance(
    payload: ComplianceCheckIn, user: CurrentUser, db: DbSession
):
    """Scan marketing text for FTC / network disclosure compliance.

    Tenant-scoped by construction (the check is pure computation on the
    caller's own text). Nothing is stored unless violations are found and
    the caller asks — use ``store_issue=true`` to file it for review.
    """
    content_type = payload.content_type if payload.content_type in _CONTENT_TYPES else "ad_hoc"
    result = check_text(payload.text, content_type=content_type, pack_id=payload.pack_id)
    return ComplianceCheckOut(**result.to_dict())


@router.get("/rules")
def list_rules(user: CurrentUser):
    """Available compliance rule packs (id, version, authority)."""
    return {"default": DEFAULT_PACK_ID, "packs": list_rule_packs()}


@router.get("/rules/{pack_id}")
def get_rules(pack_id: str, user: CurrentUser):
    """Full rule pack: requirements, accepted/vague terms, notes."""
    return get_rule_pack(pack_id)


class ComplianceIssueOut(BaseModel):
    id: str
    source: str
    pack_id: str
    content_type: str
    subject_type: str
    subject_id: str | None
    subject_title: str | None
    severity: str
    violations: list[dict[str, Any]]
    status: str
    created_at: str


@router.get("/issues", response_model=list[ComplianceIssueOut])
def list_issues(
    user: CurrentUser,
    db: DbSession,
    status: str = Query(default="open"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Human review queue of compliance flags (checker + bot)."""
    q = scoped(db, ComplianceIssueRow, user)
    if status != "all":
        q = q.filter(ComplianceIssueRow.status == status)
    q = q.order_by(ComplianceIssueRow.created_at.desc())
    limit = max(1, min(limit, 200))
    rows = q.limit(limit).all()
    return [
        ComplianceIssueOut(
            id=str(r.id),
            source=r.source,
            pack_id=r.pack_id,
            content_type=r.content_type,
            subject_type=r.subject_type,
            subject_id=str(r.subject_id) if r.subject_id else None,
            subject_title=r.subject_title,
            severity=r.severity,
            violations=r.violations or [],
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.post("/issues/{issue_id}/acknowledge")
def acknowledge_issue(issue_id: str, user: CurrentUser, db: DbSession):
    """Mark a flag acknowledged (human reviewed, no action taken)."""
    row = (
        scoped(db, ComplianceIssueRow, user)
        .filter(ComplianceIssueRow.id == uuid.UUID(issue_id))
        .first()
    )
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Compliance issue not found")
    row.status = "acknowledged"
    db.commit()
    return {"id": str(row.id), "status": row.status}


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(issue_id: str, user: CurrentUser, db: DbSession):
    """Mark a flag resolved (fixed the content)."""
    row = (
        scoped(db, ComplianceIssueRow, user)
        .filter(ComplianceIssueRow.id == uuid.UUID(issue_id))
        .first()
    )
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Compliance issue not found")
    row.status = "resolved"
    db.commit()
    return {"id": str(row.id), "status": row.status}
