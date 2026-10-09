"""Growth Engine P1: business-side partner management.

Tenant-scoped via ``business_id``; owner/admin only for writes.
Partners are inbound (they promote us, we pay them) — never the
outbound affiliate tables.
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_db.models import (
    Partner,
    PartnerApplication,
    PartnerTier,
    PartnerUser,
)

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    require_role,
    scoped,
)
from app.core.security import get_password_hash

router = APIRouter(prefix="/partners", tags=["partners"])

_owner_admin = require_role("owner", "admin")


def _referral_code() -> str:
    return f"p-{secrets.token_hex(6)}"


def _unique_referral_code(db: DbSession, business_id: uuid.UUID) -> str:
    for _ in range(8):
        code = _referral_code()
        exists = (
            db.query(Partner.id)
            .filter(
                Partner.business_id == business_id,
                Partner.referral_code == code,
            )
            .first()
        )
        if not exists:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not generate a unique referral code",
    )


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------


@router.get("", response_model=schemas.Page[schemas.PartnerOut])
def list_partners(
    user: CurrentUser,
    db: DbSession,
    status: str | None = Query(default=None),
    type: str | None = Query(default=None),  # noqa: A002 - query param name
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Partner, user).order_by(Partner.created_at.desc())
    if status:
        query = query.filter(Partner.status == status)
    if type:
        query = query.filter(Partner.type == type)
    return paginate(query, limit, offset)


@router.post(
    "",
    response_model=schemas.PartnerOut,
    status_code=201,
    dependencies=[Depends(_owner_admin)],
)
def create_partner(
    payload: schemas.PartnerCreate, user: CurrentUser, db: DbSession
):
    if payload.tier_id is not None:
        get_owned_or_404(db, PartnerTier, payload.tier_id, user)
    partner = Partner(
        business_id=user.business_id,
        referral_code=_unique_referral_code(db, user.business_id),
        **payload.model_dump(),
    )
    db.add(partner)
    db.commit()
    db.refresh(partner)
    return partner


@router.get("/{partner_id}", response_model=schemas.PartnerOut)
def get_partner(partner_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, Partner, partner_id, user)


@router.put(
    "/{partner_id}",
    response_model=schemas.PartnerOut,
    dependencies=[Depends(_owner_admin)],
)
def update_partner(
    partner_id: uuid.UUID,
    payload: schemas.PartnerUpdate,
    user: CurrentUser,
    db: DbSession,
):
    partner = get_owned_or_404(db, Partner, partner_id, user)
    data = payload.model_dump(exclude_unset=True)
    if data.get("tier_id") is not None:
        get_owned_or_404(db, PartnerTier, data["tier_id"], user)
    for field, value in data.items():
        setattr(partner, field, value)
    db.commit()
    db.refresh(partner)
    return partner


@router.post(
    "/{partner_id}/portal-user",
    response_model=schemas.PartnerUserOut,
    status_code=201,
    dependencies=[Depends(_owner_admin)],
)
def create_portal_user(
    partner_id: uuid.UUID,
    payload: schemas.PartnerUserCreate,
    user: CurrentUser,
    db: DbSession,
):
    """Create a portal login for an approved partner. External identity —
    never a ``users`` row."""
    partner = get_owned_or_404(db, Partner, partner_id, user)
    if partner.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Partner must be approved before creating a portal login",
        )
    if payload.partner_id != partner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="partner_id in body must match the path",
        )
    existing = (
        db.query(PartnerUser).filter(PartnerUser.email == payload.email).first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A portal login already exists for that email",
        )
    portal_user = PartnerUser(
        partner_id=partner_id,
        email=payload.email,
        password_hash=get_password_hash(payload.password),
    )
    db.add(portal_user)
    db.commit()
    db.refresh(portal_user)
    return portal_user


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

applications_router = APIRouter(
    prefix="/partner-applications", tags=["partner-applications"]
)


@applications_router.post(
    "/apply/{business_id}",
    response_model=schemas.PartnerApplicationOut,
    status_code=201,
)
def submit_application(
    business_id: uuid.UUID,
    payload: schemas.PartnerApplicationCreate,
    db: DbSession,
):
    """Public application endpoint — no auth. The business_id in the path
    scopes the application to the right tenant."""
    application = PartnerApplication(
        business_id=business_id,
        type=payload.type,
        form_data=payload.form_data,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


@applications_router.get(
    "", response_model=schemas.Page[schemas.PartnerApplicationOut]
)
def list_applications(
    user: CurrentUser,
    db: DbSession,
    status: str | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, PartnerApplication, user).order_by(
        PartnerApplication.created_at.desc()
    )
    if status:
        query = query.filter(PartnerApplication.status == status)
    return paginate(query, limit, offset)


@applications_router.post(
    "/{application_id}/approve",
    response_model=schemas.PartnerApplicationOut,
    dependencies=[Depends(_owner_admin)],
)
def approve_application(
    application_id: uuid.UUID, user: CurrentUser, db: DbSession
):
    application = get_owned_or_404(db, PartnerApplication, application_id, user)
    if application.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application is already {application.status}",
        )
    name = ""
    if isinstance(application.form_data, dict):
        name = str(application.form_data.get("name") or "").strip()
    partner = Partner(
        business_id=user.business_id,
        type=application.type,
        name=name or f"Partner {application.id.hex[:8]}",
        email=str(application.form_data.get("email"))
        if isinstance(application.form_data, dict)
        and application.form_data.get("email")
        else None,
        status="approved",
        referral_code=_unique_referral_code(db, user.business_id),
    )
    db.add(partner)
    db.flush()
    application.status = "approved"
    application.partner_id = partner.id
    application.reviewed_by = user.id
    db.commit()
    db.refresh(application)
    return application


@applications_router.post(
    "/{application_id}/reject",
    response_model=schemas.PartnerApplicationOut,
    dependencies=[Depends(_owner_admin)],
)
def reject_application(
    application_id: uuid.UUID, user: CurrentUser, db: DbSession
):
    application = get_owned_or_404(db, PartnerApplication, application_id, user)
    if application.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application is already {application.status}",
        )
    application.status = "rejected"
    application.reviewed_by = user.id
    db.commit()
    db.refresh(application)
    return application


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

tiers_router = APIRouter(prefix="/partner-tiers", tags=["partner-tiers"])


@tiers_router.get("", response_model=schemas.Page[schemas.PartnerTierOut])
def list_tiers(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, PartnerTier, user).order_by(PartnerTier.created_at)
    return paginate(query, limit, offset)


@tiers_router.post(
    "",
    response_model=schemas.PartnerTierOut,
    status_code=201,
    dependencies=[Depends(_owner_admin)],
)
def create_tier(
    payload: schemas.PartnerTierCreate, user: CurrentUser, db: DbSession
):
    tier = PartnerTier(business_id=user.business_id, **payload.model_dump())
    db.add(tier)
    db.commit()
    db.refresh(tier)
    return tier


@tiers_router.put(
    "/{tier_id}",
    response_model=schemas.PartnerTierOut,
    dependencies=[Depends(_owner_admin)],
)
def update_tier(
    tier_id: uuid.UUID,
    payload: schemas.PartnerTierUpdate,
    user: CurrentUser,
    db: DbSession,
):
    tier = get_owned_or_404(db, PartnerTier, tier_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tier, field, value)
    db.commit()
    db.refresh(tier)
    return tier


@tiers_router.delete("/{tier_id}", status_code=204, dependencies=[Depends(_owner_admin)])
def delete_tier(tier_id: uuid.UUID, user: CurrentUser, db: DbSession):
    tier = get_owned_or_404(db, PartnerTier, tier_id, user)
    in_use = (
        scoped(db, Partner, user).filter(Partner.tier_id == tier_id).count()
    )
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tier is assigned to {in_use} partner(s)",
        )
    db.delete(tier)
    db.commit()
