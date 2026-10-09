"""Auth: register, login, me."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from forge_db.models import Business, User, UserRole

from app import schemas
from app.core.deps import CurrentSettings, CurrentUser, DbSession
from app.core.security import (
    create_access_token,
    get_password_hash,
    verify_password,
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
    db: DbSession,
    settings: CurrentSettings,
    form: OAuth2PasswordRequestForm = Depends(),
):
    """OAuth2 password flow: ``username`` is the email address."""
    user = db.query(User).filter(User.email == form.username).first()
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is disabled",
        )
    return schemas.TokenResponse(access_token=_issue_token(user, settings))


@router.get("/me", response_model=schemas.UserOut)
def me(user: CurrentUser):
    return user
