"""Partner-scoped authentication: separate identity from business users.

Partners are NEVER ``users`` rows. A partner JWT carries
``typ="partner"`` plus ``partner_id`` and ``business_id`` claims. The
``CurrentPartner`` dependency resolves it to an active ``PartnerUser``
whose linked partner is approved. Partner tokens are only honored by
the partner portal router — they must never authenticate business
endpoints (business endpoints use ``CurrentUser``, which looks up the
``users`` table and 401s on partner tokens).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from forge_db.models import Partner, PartnerUser

from app.core.config import Settings, get_settings
from app.core.deps import DbSession, get_settings_dep
from app.core.security import create_access_token, decode_access_token

PARTNER_TOKEN_TYPE = "partner"

partner_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class PartnerIdentity:
    """Resolved partner caller: portal user + linked partner + business."""

    partner_user_id: uuid.UUID
    partner_id: uuid.UUID
    business_id: uuid.UUID
    email: str


def create_partner_token(
    *,
    secret: str,
    partner_user_id: uuid.UUID,
    partner_id: uuid.UUID,
    business_id: uuid.UUID,
    expires_minutes: int = 60 * 12,
) -> str:
    """Mint a partner-scoped JWT. Distinct ``typ`` from business tokens."""
    now_payload = create_access_token(
        subject=str(partner_user_id),
        secret=secret,
        business_id=str(business_id),
        role="partner",
        expires_minutes=expires_minutes,
    )
    # create_access_token does not set typ; re-encode with the partner type.
    payload: dict[str, Any] = jwt.decode(
        now_payload, secret, algorithms=["HS256"]
    )
    payload["typ"] = PARTNER_TOKEN_TYPE
    payload["partner_id"] = str(partner_id)
    return jwt.encode(payload, secret, algorithm="HS256")


def get_current_partner(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(partner_bearer)
    ],
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> PartnerIdentity:
    """Resolve a partner Bearer <redacted> (401 on any failure)."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate partner credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or not credentials.credentials:
        raise credentials_error
    try:
        payload = decode_access_token(
            credentials.credentials, secret=settings.JWT_SECRET
        )
    except jwt.PyJWTError:
        raise credentials_error from None
    if payload.get("typ") != PARTNER_TOKEN_TYPE:
        # Business tokens (or anything else) are rejected here: the portal
        # only honors partner-scoped tokens.
        raise credentials_error
    try:
        partner_user_id = uuid.UUID(str(payload.get("sub")))
        partner_id = uuid.UUID(str(payload.get("partner_id")))
        business_id = uuid.UUID(str(payload.get("business_id")))
    except (ValueError, TypeError):
        raise credentials_error from None

    partner_user = db.get(PartnerUser, partner_user_id)
    partner = db.get(Partner, partner_id)
    if (
        partner_user is None
        or not partner_user.is_active
        or partner_user.partner_id != partner_id
        or partner is None
        or partner.business_id != business_id
        or partner.status != "approved"
    ):
        raise credentials_error
    return PartnerIdentity(
        partner_user_id=partner_user_id,
        partner_id=partner_id,
        business_id=business_id,
        email=partner_user.email,
    )


CurrentPartner = Annotated[PartnerIdentity, Depends(get_current_partner)]
