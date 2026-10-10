"""Alpha reliability slice — lead-to-follow-up API (``/alpha``).

Every endpoint is tenant-scoped (``business_id`` from the JWT) and
rate-limited. Cross-tenant access returns 404 (never 403) so row existence
is not revealed.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forge_db.models import (
    ActionEvidence,
    AlphaApproval,
    AlphaApprovalStatus,
    Lead,
    LeadDuplicate,
    LeadSource,
    LeadStatus,
    OutboundMessage,
    OutboundStatus,
    QualificationResult,
    QualificationRules,
    RunState,
    StepState,
    User,
    WorkflowRun,
    WorkflowStep,
    WorkflowTransition,
)

from app.alpha import service
from app.alpha.connectors.stub import StubConnector
from app.alpha.policy import APPROVAL_TTL, payload_digest
from app.alpha.qualify import DEFAULT_RULES
from app.alpha.workflow import InvalidTransition, is_terminal, transition_log
from app.core.deps import (
    CurrentSettings,
    CurrentUser,
    DbSession,
    get_owned_or_404,
    require_role,
    scoped,
)
from app.draven_crypto import decrypt_secret, encrypt_secret, get_fernet
from app.settings_vault import service as vault

router = APIRouter(prefix="/alpha", tags=["alpha"])

AdminUser = Annotated[User, Depends(require_role("owner", "admin"))]


# ---------------------------------------------------------------------------
# Rate limiting (in-process sliding window; Redis for multi-worker — documented)
# ---------------------------------------------------------------------------

_rate: dict[tuple[uuid.UUID, str], list[float]] = {}
_RATE_LIMITS = {"default": (120, 60.0), "webhook": (300, 60.0)}


def _check_rate(business_id: uuid.UUID, bucket: str = "default") -> None:
    limit, window = _RATE_LIMITS[bucket]
    now = time.monotonic()
    key = (business_id, bucket)
    stamps = [s for s in _rate.get(key, []) if s > now - window]
    if len(stamps) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded — slow down",
        )
    stamps.append(now)
    _rate[key] = stamps


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LeadIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    source: str = "manual"
    source_event_id: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RulesIn(BaseModel):
    name: str = "default"
    criteria: list[dict[str, Any]]
    threshold: float = 0.6


class DraftIn(BaseModel):
    recipient: Optional[str] = None
    subject: str
    body: str


class EditDraftIn(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


class RejectIn(BaseModel):
    reason: Optional[str] = None


class ReleaseIn(BaseModel):
    confirm: str


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _lead_out(lead: Lead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "source": lead.source,
        "source_event_id": lead.source_event_id,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "company": lead.company,
        "status": lead.status.value,
        "duplicate_of_id": str(lead.duplicate_of_id) if lead.duplicate_of_id else None,
        "provenance": lead.provenance,
        "created_at": _iso(lead.created_at),
    }


def _run_out(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "lead_id": str(run.lead_id),
        "state": run.state.value,
        "terminal": is_terminal(run.state),
        "current_step": run.current_step,
        "retry_count": run.retry_count,
        "paused": run.paused,
        "cost_cents": run.cost_cents,
        "latency_ms": run.latency_ms,
        "error": run.error,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _timeline(db: Session, run: WorkflowRun) -> dict[str, Any]:
    steps = (
        db.query(WorkflowStep)
        .filter(WorkflowStep.run_id == run.id)
        .order_by(WorkflowStep.created_at.asc())
        .all()
    )
    evidence = (
        db.query(ActionEvidence)
        .filter(ActionEvidence.run_id == run.id)
        .order_by(ActionEvidence.created_at.asc())
        .all()
    )
    approvals = (
        db.query(AlphaApproval)
        .filter(AlphaApproval.run_id == run.id)
        .order_by(AlphaApproval.created_at.asc())
        .all()
    )
    messages = (
        db.query(OutboundMessage)
        .filter(OutboundMessage.run_id == run.id)
        .order_by(OutboundMessage.created_at.asc())
        .all()
    )
    return {
        "run": _run_out(run),
        "transitions": transition_log(db, run.id),
        "steps": [
            {
                "name": s.name,
                "state": s.state.value,
                "attempts": s.attempts,
                "evidence": s.evidence,
                "error": s.error,
                "started_at": _iso(s.started_at),
                "ended_at": _iso(s.ended_at),
            }
            for s in steps
        ],
        "evidence": [
            {
                "step": e.step,
                "connector": e.connector,
                "result": e.result,
                "verified_at": _iso(e.verified_at),
            }
            for e in evidence
        ],
        "approvals": [
            {
                "id": str(a.id),
                "action_type": a.action_type,
                "status": a.status.value,
                "payload_digest": a.payload_digest,
                "expires_at": _iso(a.expires_at),
                "decided_at": _iso(a.decided_at),
            }
            for a in approvals
        ],
        "messages": [
            {
                "id": str(m.id),
                "recipient": m.recipient,
                "subject": m.subject,
                "status": m.status.value,
                "provider": m.provider,
                "provider_message_id": m.provider_message_id,
                "send_attempts": m.send_attempts,
                "last_error": m.last_error,
            }
            for m in messages
        ],
    }


def _get_run(db: Session, user: User, run_id: uuid.UUID) -> WorkflowRun:
    return get_owned_or_404(db, WorkflowRun, run_id, user)


# ---------------------------------------------------------------------------
# Webhook sources (HMAC secrets)
# ---------------------------------------------------------------------------


@router.post("/sources", status_code=201)
def register_source(
    source: str, user: AdminUser, db: DbSession, settings: CurrentSettings
) -> dict[str, Any]:
    _check_rate(user.business_id)
    secret = "whsec_" + uuid.uuid4().hex
    row = service.register_source(
        db, business_id=user.business_id, source=source,
        secret=secret,
        encrypt=lambda s: encrypt_secret(
            get_fernet(settings.DRAVEN_CONFIG_KEY), s
        ),
    )
    # Write-through: the vault is the canonical store for webhook secrets.
    vault.put_secret(
        db, user.business_id, f"webhook.{source}.secret", secret,
        actor=f"user:{user.id}", config_key=settings.DRAVEN_CONFIG_KEY,
        label=f"Webhook secret for {source}",
    )
    db.commit()
    # The secret is returned ONCE — afterwards it is only stored encrypted.
    return {"source": row.source, "secret": secret,
            "note": "store this secret now; it is never returned again"}


@router.get("/sources")
def list_sources(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    rows = scoped(db, LeadSource, user).all()
    return {
        "sources": [
            {"source": r.source, "is_active": r.is_active,
             "created_at": _iso(r.created_at)}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Lead intake
# ---------------------------------------------------------------------------


@router.post("/leads", status_code=201)
def create_lead(
    payload: LeadIn, user: CurrentUser, db: DbSession,
    process: bool = Query(default=True),
) -> dict[str, Any]:
    _check_rate(user.business_id)
    raw = {"name": payload.name, "email": payload.email,
           "phone": payload.phone, "company": payload.company,
           **payload.extra}
    try:
        lead, created, _ = service.intake_lead(
            db, business_id=user.business_id, source=payload.source,
            raw=raw, source_event_id=payload.source_event_id,
            actor=f"user:{user.id}",
        )
    except service.IntakeError as exc:
        lead = service.quarantine_lead(
            db, business_id=user.business_id, source=payload.source,
            raw=raw, reason=str(exc), actor=f"user:{user.id}",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": str(exc), "lead_id": str(lead.id),
                    "note": "lead quarantined as needs_review — not dropped"},
        ) from exc
    run = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.lead_id == lead.id)
        .order_by(WorkflowRun.created_at.desc())
        .first()
    )
    if process and created and run is not None:
        service.process_pipeline(db, run=run, user=user,
                                 actor=f"user:{user.id}")
    db.commit()
    return {"lead": _lead_out(lead), "created": created,
            "run": _run_out(run) if run else None}


@router.post("/leads/webhook/{source}")
async def webhook_lead(
    source: str, request: Request, user: CurrentUser, db: DbSession,
    settings: CurrentSettings,
) -> dict[str, Any]:
    # NOTE: real deployments verify the source independently of the user
    # session; this demo binds the webhook to the caller's business so the
    # tenant is unambiguous. The HMAC check is the real gate.
    _check_rate(user.business_id, "webhook")
    body = await request.body()
    sig = request.headers.get("X-Signature-256")
    src = (
        scoped(db, LeadSource, user)
        .filter(LeadSource.source == source, LeadSource.is_active.is_(True))
        .first()
    )
    if src is None:
        raise HTTPException(status_code=404, detail="unknown webhook source")
    # Vault first (canonical), deprecated lead_sources.secret_enc as fallback.
    secret = vault.get_secret(
        db, user.business_id, f"webhook.{source}.secret",
        settings.DRAVEN_CONFIG_KEY,
    )
    if not secret:
        secret = decrypt_secret(
            get_fernet(settings.DRAVEN_CONFIG_KEY), src.secret_enc
        )
    if not service.verify_webhook_signature(secret, body, sig):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    source_event_id = raw.get("event_id") or raw.get("id")
    try:
        lead, created, _ = service.intake_lead(
            db, business_id=user.business_id, source=source,
            raw=raw, source_event_id=source_event_id,
            actor=f"webhook:{source}",
        )
    except service.IntakeError as exc:
        lead = service.quarantine_lead(
            db, business_id=user.business_id, source=source,
            raw=raw, reason=str(exc), actor=f"webhook:{source}",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": str(exc), "lead_id": str(lead.id)},
        ) from exc
    run = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.lead_id == lead.id)
        .order_by(WorkflowRun.created_at.desc())
        .first()
    )
    if created and run is not None:
        service.process_pipeline(db, run=run, user=user,
                                 actor=f"webhook:{source}")
    db.commit()
    # Idempotent: repeat delivery returns the ORIGINAL record (200, created=false).
    return {"lead": _lead_out(lead), "created": created,
            "run": _run_out(run) if run else None}


@router.get("/leads")
def list_leads(
    user: CurrentUser, db: DbSession,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> dict[str, Any]:
    _check_rate(user.business_id)
    q = scoped(db, Lead, user).order_by(Lead.created_at.desc())
    if status:
        try:
            q = q.filter(Lead.status == LeadStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown status")
    rows = q.limit(limit).all()
    return {"leads": [_lead_out(r) for r in rows]}


@router.get("/leads/{lead_id}")
def get_lead(lead_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    lead = get_owned_or_404(db, Lead, lead_id, user)
    results = (
        db.query(QualificationResult)
        .filter(QualificationResult.lead_id == lead.id)
        .order_by(QualificationResult.created_at.desc())
        .all()
    )
    dups = (
        db.query(LeadDuplicate)
        .filter(LeadDuplicate.lead_id == lead.id)
        .all()
    )
    return {
        "lead": _lead_out(lead),
        "qualification": [
            {"verdict": r.verdict.value, "score": r.score,
             "confidence": r.confidence, "rules_version": r.rules_version,
             "criteria": r.criteria, "created_at": _iso(r.created_at)}
            for r in results
        ],
        "duplicates": [
            {"candidate_lead_id": str(d.candidate_lead_id),
             "match_reason": d.match_reason, "status": d.status.value}
            for d in dups
        ],
    }


# ---------------------------------------------------------------------------
# Qualification rules (versioned)
# ---------------------------------------------------------------------------


@router.post("/rules", status_code=201)
def create_rules(payload: RulesIn, user: AdminUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    latest = (
        scoped(db, QualificationRules, user)
        .order_by(QualificationRules.version.desc())
        .first()
    )
    version = (latest.version + 1) if latest else 1
    row = QualificationRules(
        business_id=user.business_id, version=version, name=payload.name,
        rules={"criteria": payload.criteria, "threshold": payload.threshold},
        threshold=payload.threshold, is_active=True,
    )
    db.add(row)
    # Only one active version at a time; old versions stay for audit.
    for r in scoped(db, QualificationRules, user).filter(
        QualificationRules.id != row.id
    ).all():
        r.is_active = False
    db.flush()
    db.commit()
    return {"version": row.version, "name": row.name,
            "threshold": row.threshold}


@router.get("/rules")
def list_rules(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    rows = (
        scoped(db, QualificationRules, user)
        .order_by(QualificationRules.version.desc())
        .all()
    )
    return {
        "rules": [
            {"version": r.version, "name": r.name,
             "threshold": r.threshold, "is_active": r.is_active,
             "criteria": r.rules.get("criteria", []),
             "created_at": _iso(r.created_at)}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Runs + ledger
# ---------------------------------------------------------------------------


@router.get("/runs")
def list_runs(
    user: CurrentUser, db: DbSession,
    state: Optional[str] = Query(default=None),
    lead_id: Optional[uuid.UUID] = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> dict[str, Any]:
    _check_rate(user.business_id)
    q = scoped(db, WorkflowRun, user).order_by(WorkflowRun.created_at.desc())
    if state:
        try:
            q = q.filter(WorkflowRun.state == RunState(state))
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown state")
    if lead_id:
        q = q.filter(WorkflowRun.lead_id == lead_id)
    return {"runs": [_run_out(r) for r in q.limit(limit).all()]}


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    return _timeline(db, _get_run(db, user, run_id))


@router.post("/runs/{run_id}/prepare", status_code=201)
def prepare(
    run_id: uuid.UUID, payload: DraftIn, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    recipient = payload.recipient
    if not recipient:
        lead = get_owned_or_404(db, Lead, run.lead_id, user)
        recipient = lead.email or ""
    if not recipient:
        raise HTTPException(status_code=422, detail="no recipient email available")
    try:
        message, approval = service.prepare_followup(
            db, run=run, user=user, recipient=recipient,
            subject=payload.subject, body=payload.body,
            actor=f"user:{user.id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {
        "message": {"id": str(message.id), "recipient": message.recipient,
                    "subject": message.subject,
                    "status": message.status.value},
        "approval": {"id": str(approval.id),
                     "status": approval.status.value,
                     "expires_at": _iso(approval.expires_at)},
        "run": _run_out(run),
    }


@router.post("/runs/{run_id}/approve")
def approve(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    approval = (
        db.query(AlphaApproval)
        .filter(AlphaApproval.run_id == run.id,
                AlphaApproval.business_id == user.business_id)
        .order_by(AlphaApproval.created_at.desc())
        .first()
    )
    if approval is None:
        raise HTTPException(status_code=404, detail="no approval on this run")
    try:
        service.approve_followup(db, approval=approval, user=user,
                                 actor=f"user:{user.id}")
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"approval": {"id": str(approval.id),
                         "status": approval.status.value},
            "run": _run_out(run)}


@router.post("/runs/{run_id}/reject")
def reject(
    run_id: uuid.UUID, payload: RejectIn, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    approval = (
        db.query(AlphaApproval)
        .filter(AlphaApproval.run_id == run.id,
                AlphaApproval.business_id == user.business_id)
        .order_by(AlphaApproval.created_at.desc())
        .first()
    )
    if approval is None:
        raise HTTPException(status_code=404, detail="no approval on this run")
    try:
        service.reject_followup(db, approval=approval, user=user,
                                actor=f"user:{user.id}",
                                reason=payload.reason)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"approval": {"id": str(approval.id),
                         "status": approval.status.value},
            "run": _run_out(run)}


@router.post("/runs/{run_id}/submit")
def submit(
    run_id: uuid.UUID, user: CurrentUser, db: DbSession,
    auto_approve: bool = Query(default=False),
) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    connector = StubConnector()
    try:
        message = service.submit_followup(
            db, run=run, user=user, connector=connector,
            actor=f"user:{user.id}", auto_approve=auto_approve,
            sleep_on_retry=False,
        )
    except service.PolicyDenied as exc:
        db.commit()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"message": {"id": str(message.id),
                        "status": message.status.value,
                        "provider_message_id": message.provider_message_id,
                        "send_attempts": message.send_attempts,
                        "note": "submitted = provider accepted; "
                                "confirmed requires verify_outcome"},
            "run": _run_out(run)}


@router.post("/runs/{run_id}/reconcile")
def reconcile(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    connector = StubConnector()
    try:
        message = service.reconcile(db, run=run, connector=connector,
                                    actor=f"user:{user.id}")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"message": {"id": str(message.id),
                        "status": message.status.value},
            "run": _run_out(run)}


@router.post("/runs/{run_id}/retry")
def retry(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    connector = StubConnector()
    try:
        message = service.retry_run(
            db, run=run, user=user, connector=connector,
            actor=f"user:{user.id}", sleep_on_retry=False,
        )
    except (ValueError, service.PolicyDenied) as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"message": {"id": str(message.id),
                        "status": message.status.value},
            "run": _run_out(run)}


@router.post("/runs/{run_id}/cancel")
def cancel(
    run_id: uuid.UUID, payload: RejectIn, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    try:
        service.cancel_run(db, run=run, actor=f"user:{user.id}",
                           reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"run": _run_out(run)}


@router.post("/runs/{run_id}/pause")
def pause(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    try:
        service.pause_run(db, run=run, actor=f"user:{user.id}")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"run": _run_out(run)}


@router.post("/runs/{run_id}/resume")
def resume(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    run = _get_run(db, user, run_id)
    service.resume_run(db, run=run, actor=f"user:{user.id}")
    db.commit()
    return {"run": _run_out(run)}


@router.put("/messages/{message_id}")
def edit_draft(
    message_id: uuid.UUID, payload: EditDraftIn, user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    _check_rate(user.business_id)
    message = get_owned_or_404(db, OutboundMessage, message_id, user)
    try:
        service.edit_draft(
            db, message=message, user=user,
            subject=payload.subject, body=payload.body,
            actor=f"user:{user.id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    run = _get_run(db, user, message.run_id)
    return {"message": {"id": str(message.id),
                        "status": message.status.value},
            "run": _run_out(run),
            "note": "edit invalidated the pending approval — re-approval required"}


# ---------------------------------------------------------------------------
# Approval queue + kill switch + connector health
# ---------------------------------------------------------------------------


@router.get("/approvals")
def approval_queue(
    user: CurrentUser, db: DbSession,
    status_filter: Optional[str] = Query(default="pending", alias="status"),
) -> dict[str, Any]:
    _check_rate(user.business_id)
    q = (
        scoped(db, AlphaApproval, user)
        .order_by(AlphaApproval.created_at.desc())
    )
    if status_filter:
        try:
            q = q.filter(
                AlphaApproval.status == AlphaApprovalStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown status")
    rows = q.limit(100).all()
    out = []
    for a in rows:
        msg = (
            db.query(OutboundMessage)
            .filter(OutboundMessage.approval_id == a.id)
            .first()
        )
        out.append({
            "id": str(a.id),
            "run_id": str(a.run_id),
            "action_type": a.action_type,
            "status": a.status.value,
            "payload_digest": a.payload_digest,
            "payload": a.payload,
            "expires_at": _iso(a.expires_at),
            "message": (
                {"id": str(msg.id), "recipient": msg.recipient,
                 "subject": msg.subject, "body": msg.body,
                 "status": msg.status.value}
                if msg else None
            ),
        })
    return {"approvals": out}


@router.post("/kill-switch")
def kill_switch(user: AdminUser, db: DbSession) -> dict[str, Any]:
    result = service.engage_kill(
        db, business_id=user.business_id, actor=f"user:{user.id}")
    db.commit()
    return {"engaged": True, **result}


@router.post("/kill-switch/release")
def kill_switch_release(
    payload: ReleaseIn, user: AdminUser, db: DbSession
) -> dict[str, Any]:
    try:
        result = service.release_kill(
            db, business_id=user.business_id, actor=f"user:{user.id}",
            confirm=payload.confirm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/kill-switch")
def kill_switch_status(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    return {"engaged": service.kill_engaged(user.business_id)}


@router.get("/connectors")
def connector_health(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _check_rate(user.business_id)
    stub = StubConnector()
    return {
        "connectors": [
            {
                "name": stub.name,
                "display": "Stub email (test double)",
                "configured": True,
                "capabilities": [
                    "submit_email -> submission acceptance (never inbox delivery)",
                    "verify_outcome -> confirmed | unknown | failed",
                    "failure modes: ok | timeout | lost_response | rate_limit | hard_fail",
                ],
                "blind_spots": [
                    "no real delivery; no bounce/spam/complaint signals",
                    "replace with a Gmail/SMTP adapter for production",
                ],
            }
        ]
    }
