"""Auth: register, login, me."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from forge_db.models import Business, User, UserRole
from forge_db.audit import log_action

from app import schemas
from app.core.deps import CurrentSettings, CurrentUser, DbSession
from app.core.rate_limit import check_login_rate_limit, client_ip
from app.core.security import (
    create_access_token,
    get_password_hash,
    verify_and_update,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "business"


def _unique_slug(db: DbSession, base: str) -> str:
    slug, suffix = base, 1
    while db.query(Business).filter(Business.slug == slug).first() is not None:
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def _issue_token(user: User, settings: CurrentSettings) -> str:
    return create_access_token(
        subject=str(user.id),
        secret=settings.JWT_SECRET,
        business_id=str(user.business_id),
        role=user.role.value,
        expires_minutes=settings.JWT_EXPIRE_MINUTES,
    )


@router.post("/register", response_model=schemas.RegisterResponse, status_code=201)
def register(payload: schemas.RegisterRequest, db: DbSession, settings: CurrentSettings):
    """Create a business + its first (owner) user and return a JWT."""
    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    business = Business(name=payload.business_name, slug=_unique_slug(db, _slugify(payload.business_name)))
    db.add(business)
    db.flush()
    user = User(
        email=payload.email,
        password_hash=get_password_hash(payload.password),
        full_name=payload.full_name,
        business_id=business.id,
        role=UserRole.owner,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return schemas.RegisterResponse(token=_issue_token(user, settings), user=user)


@router.post("/login", response_model=schemas.TokenResponse)
def login(
    request: Request,
    db: DbSession,
    settings: CurrentSettings,
    form: OAuth2PasswordRequestForm = Depends(),
):
    """OAuth2 password flow: ``username`` is the email address.

    Throttled per-IP (5/min) and per-account (10/hr); 429 + Retry-After
    when exceeded. Successful logins transparently re-hash legacy
    (low-round) password hashes to current OWASP parameters.
    """
    check_login_rate_limit(
        request,
        redis_url=settings.REDIS_URL,
        account=form.username,
        scope="login",
    )
    user = db.query(User).filter(User.email == form.username).first()
    verified, new_hash = (
        verify_and_update(form.password, user.password_hash)
        if user is not None
        else (False, None)
    )
    if user is None or not verified:
        log_action(
            db,
            action="auth.login",
            actor_email=form.username,
            business_id=user.business_id if user is not None else None,
            details={"result": "failure"},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if new_hash is not None:
        # Transparent migration: old pbkdf2 rounds -> 600k, no flag day.
        user.password_hash = new_hash
        db.commit()
    if not user.is_active:
        log_action(
            db,
            action="auth.login",
            actor_id=str(user.id),
            actor_email=user.email,
            business_id=user.business_id,
            details={"result": "failure", "reason": "disabled"},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is disabled",
        )
    log_action(
        db,
        action="auth.login",
        actor_id=str(user.id),
        actor_email=user.email,
        business_id=user.business_id,
        details={"result": "success"},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return schemas.TokenResponse(access_token=_issue_token(user, settings))


@router.get("/me", response_model=schemas.UserOut)
def me(user: CurrentUser):
    return user


@router.post("/logout")
def logout(request: Request, user: CurrentUser, db: DbSession):
    """Record a logout in the audit log.

    Tokens are stateless JWTs — there is nothing server-side to revoke;
    the client discards its token. This endpoint exists so the audit
    trail captures session end.
    """
    log_action(
        db,
        action="auth.logout",
        actor_id=str(user.id),
        actor_email=user.email,
        business_id=user.business_id,
        details={"result": "success"},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "ok"}
    return user
