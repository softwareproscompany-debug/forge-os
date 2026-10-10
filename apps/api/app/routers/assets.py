"""Asset lifecycle: generate, list, approval state machine, versions.

Approval transitions are enforced server-side::

    draft -> in_review -> approved | rejected
    rejected -> draft                      (rework via ``submit``)

Only ``approved`` assets may be attached to campaign steps that launch.
``POST /assets/generate`` creates the asset row in ``draft`` and enqueues the
``generate_asset`` worker job; when Redis is unreachable the asset is still
created and the response carries ``job_id: null`` + a ``warning`` (never 500).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status

from forge_db.audit import log_action
from forge_db.models import (
    ApprovalDecision,

    Asset,
    AssetApproval,
    AssetKind,
    AssetStatus,
    Template,
    User,
    assert_asset_transition,
)

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
from app.core.queue import enqueue_job
from app.core.rate_limit import client_ip, quota_limited

router = APIRouter(prefix="/assets", tags=["assets"])


@router.post("/generate", response_model=schemas.AssetGenerateResponse, status_code=201)
@quota_limited("generation")
async def generate_asset(
    payload: schemas.AssetGenerateRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
):
    """Create a ``draft`` asset and enqueue the ``generate_asset`` worker job."""
    template_id: uuid.UUID | None = None
    if payload.template_id is not None:
        get_owned_or_404(db, Template, payload.template_id, user)
        template_id = payload.template_id

    asset = Asset(
        business_id=user.business_id,
        kind=AssetKind(payload.kind),
        title=payload.title,
        body=payload.prompt,  # worker overwrites with generated copy
        variables=dict(payload.variables),
        status=AssetStatus.draft,
        created_by=user.id,
        is_affiliate_content=payload.is_affiliate_content,
    )
    if template_id is not None:
        asset.variables = {**asset.variables, "_template_id": str(template_id)}
    db.add(asset)
    db.commit()
    db.refresh(asset)

    job_id = await enqueue_job(settings.REDIS_URL, "generate_asset", str(asset.id))
    warning = (
        None
        if job_id is not None
        else "Redis unreachable: asset created in draft, generation job not queued"
    )
    return schemas.AssetGenerateResponse(
        asset_id=asset.id, job_id=job_id, warning=warning
    )


@router.get("", response_model=schemas.Page[schemas.AssetOut])
def list_assets(
    user: CurrentUser,
    db: DbSession,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Asset, user)
    if status is not None:
        try:
            query = query.filter(Asset.status == AssetStatus(status))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown asset status: {status}",
            )
    if kind is not None:
        try:
            query = query.filter(Asset.kind == AssetKind(kind))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown asset kind: {kind}",
            )
    query = query.order_by(Asset.created_at.desc())
    return paginate(query, limit, offset)


@router.get("/{asset_id}", response_model=schemas.AssetOut)
def get_asset(asset_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, Asset, asset_id, user)


@router.patch("/{asset_id}", response_model=schemas.AssetOut)
def update_asset(
    asset_id: uuid.UUID,
    payload: schemas.AssetUpdateRequest,
    user: CurrentUser,
    db: DbSession,
):
    """Update asset metadata (currently just the affiliate-content flag).

    Does not touch the approval state machine — use submit/approve/reject
    for status changes.
    """
    asset = get_owned_or_404(db, Asset, asset_id, user)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(asset, field, value)
    db.commit()
    db.refresh(asset)
    return asset


def _transition(
    db: DbSession,
    asset: Asset,
    target: AssetStatus,
    reviewer: User | None = None,
    note: str | None = None,
    reason: str | None = None,
) -> Asset:
    try:
        assert_asset_transition(asset.status, target)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    asset.status = target
    reviewer_id = reviewer.id if reviewer is not None else None
    if target is AssetStatus.approved:
        asset.approved_by = reviewer_id
        asset.rejection_reason = None
        db.add(
            AssetApproval(
                asset_id=asset.id,
                reviewer_id=reviewer_id,
                decision=ApprovalDecision.approved,
                note=note,
            )
        )
    elif target is AssetStatus.rejected:
        asset.rejection_reason = reason
        db.add(
            AssetApproval(
                asset_id=asset.id,
                reviewer_id=reviewer_id,
                decision=ApprovalDecision.rejected,
                note=reason,
            )
        )
    db.commit()
    db.refresh(asset)
    return asset


@router.post("/{asset_id}/submit", response_model=schemas.AssetOut)
def submit_asset(asset_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Move ``draft -> in_review`` (or rework ``rejected -> draft``)."""
    asset = get_owned_or_404(db, Asset, asset_id, user)
    if asset.status is AssetStatus.rejected:
        # Rework: back to draft; the author resubmits from there.
        return _transition(db, asset, AssetStatus.draft)
    return _transition(db, asset, AssetStatus.in_review)


@router.post("/{asset_id}/approve", response_model=schemas.AssetOut)
def approve_asset(
    asset_id: uuid.UUID,
    payload: schemas.AssetApprovalRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
):
    """Move ``in_review -> approved`` and record the approval."""
    asset = get_owned_or_404(db, Asset, asset_id, user)
    result = _transition(db, asset, AssetStatus.approved, reviewer=user, note=payload.note)
    log_action(
        db,
        action="asset.approve",
        actor_id=str(user.id),
        actor_email=user.email,
        business_id=user.business_id,
        resource_type="asset",
        resource_id=str(asset.id),
        details={"kind": asset.kind.value if asset.kind else None},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return result


@router.post("/{asset_id}/reject", response_model=schemas.AssetOut)
def reject_asset(
    asset_id: uuid.UUID,
    payload: schemas.AssetRejectRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
):
    """Move ``in_review -> rejected`` and record the rejection reason."""
    asset = get_owned_or_404(db, Asset, asset_id, user)
    result = _transition(db, asset, AssetStatus.rejected, reviewer=user, reason=payload.reason)
    log_action(
        db,
        action="asset.reject",
        actor_id=str(user.id),
        actor_email=user.email,
        business_id=user.business_id,
        resource_type="asset",
        resource_id=str(asset.id),
        details={"reason": payload.reason},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return result


@router.get("/{asset_id}/versions", response_model=schemas.Page[schemas.AssetOut])
def asset_versions(asset_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Return the full version lineage (root + descendants, ordered by version)."""
    asset = get_owned_or_404(db, Asset, asset_id, user)

    # Walk up to the lineage root.
    root = asset
    seen = {root.id}
    while root.parent_asset_id is not None and root.parent_asset_id not in seen:
        parent = (
            scoped(db, Asset, user).filter(Asset.id == root.parent_asset_id).first()
        )
        if parent is None:
            break
        seen.add(parent.id)
        root = parent

    # Collect all descendants breadth-first.
    lineage = [root]
    queue = [root]
    while queue:
        current = queue.pop(0)
        children = (
            scoped(db, Asset, user)
            .filter(Asset.parent_asset_id == current.id)
            .order_by(Asset.version.asc())
            .all()
        )
        for child in children:
            if child.id not in seen:
                seen.add(child.id)
                lineage.append(child)
                queue.append(child)

    lineage.sort(key=lambda a: (a.version, a.created_at))
    return {"items": lineage, "total": len(lineage)}
