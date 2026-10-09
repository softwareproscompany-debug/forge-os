"""Partner portal: partner-facing API on partner tokens ONLY.

Every endpoint here depends on ``CurrentPartner`` — never ``CurrentUser``.
A partner token must never authenticate a business endpoint, and a
business token must never authenticate the portal (enforced by the
``typ`` claim in partner_deps).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from forge_db.models import Partner, PartnerTier, PartnerUser

from app import schemas
from app.core.deps import CurrentSettings, DbSession
from app.core.partner_deps import CurrentPartner, create_partner_token
from app.core.security import verify_password

router = APIRouter(prefix="/portal", tags=["partner-portal"])


def _referral_link(settings: CurrentSettings, code: str) -> str:
    base = getattr(settings, "PORTAL_BASE_URL", "") or "https://forgeos.app"
    return f"{base.rstrip('/')}/p/{code}"


@router.post("/login", response_model=schemas.PortalLoginResponse)
def portal_login(
    payload: schemas.PortalLoginRequest,
    db: DbSession,
    settings: CurrentSettings,
):
    """Exchange portal email+password for a partner-scoped JWT."""
    portal_user = (
        db.query(PartnerUser)
        .filter(PartnerUser.email == payload.email)
        .first()
    )
    if (
        portal_user is None
        or not verify_password(payload.password, portal_user.password_hash)
        or not portal_user.is_active
        or portal_user.partner_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    partner = db.get(Partner, portal_user.partner_id)
    if partner is None or partner.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Partner account is not active",
        )
    token = create_partner_token(
        secret=settings.JWT_SECRET,
        partner_user_id=portal_user.id,
        partner_id=partner.id,
        business_id=partner.business_id,
    )
    return schemas.PortalLoginResponse(access_token=token)


@router.get("/me", response_model=schemas.PortalProfileOut)
def portal_profile(
    partner: CurrentPartner, db: DbSession, settings: CurrentSettings
):
    """The caller's own partner profile. Scoped by the partner token."""
    row = db.get(Partner, partner.partner_id)
    if row is None or row.business_id != partner.business_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Partner not found"
        )
    tier_name: str | None = None
    if row.tier_id is not None:
        tier = db.get(PartnerTier, row.tier_id)
        tier_name = tier.name if tier is not None else None
    return schemas.PortalProfileOut(
        partner_id=row.id,
        business_id=row.business_id,
        email=partner.email,
        name=row.name,
        type=row.type,
        status=row.status,
        referral_code=row.referral_code,
        referral_link=_referral_link(settings, row.referral_code),
        tier_name=tier_name,
    )
