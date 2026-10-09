"""Request-scoped dependencies: settings, DB session, auth, tenant scoping."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import Annotated, Any, TypeVar

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from forge_db.models import Base, User
from forge_db.session import get_db as _forge_get_db

from app.core.config import Settings, get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

T = TypeVar("T", bound=Base)


@lru_cache(maxsize=1)
def cached_settings() -> Settings:
    return get_settings()


def get_settings_dep() -> Settings:
    return cached_settings()


def get_db_session(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> Any:
    """Yield a SQLAlchemy session bound to the configured DATABASE_URL."""
    yield from _forge_get_db(settings.DATABASE_URL)


DbSession = Annotated[Session, Depends(get_db_session)]
CurrentSettings = Annotated[Settings, Depends(get_settings_dep)]


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: DbSession,
    settings: CurrentSettings,
) -> User:
    """Resolve the JWT bearer token to an active ``User`` (401 on failure)."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
    except jwt.PyJWTError:
        raise credentials_error from None
    if not user_id:
        raise credentials_error
    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        raise credentials_error from None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        raise credentials_error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: str) -> Callable[[User], Coroutine[Any, Any, User]]:
    """Dependency factory: allow only users whose role is in ``roles`` (403)."""

    async def _check(user: CurrentUser) -> User:
        if user.role.value not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(roles)}",
            )
        return user

    return _check


# ---------------------------------------------------------------------------
# Tenant scoping — every query on a tenant table filters by JWT business_id.
# ---------------------------------------------------------------------------


def scoped(db: Session, model: type[T], user: User):
    """Base query for ``model`` restricted to the caller's business."""
    return db.query(model).filter(model.business_id == user.business_id)  # type: ignore[attr-defined]


def get_owned_or_404(db: Session, model: type[T], obj_id: uuid.UUID, user: User) -> T:
    """Fetch one row of a ``business_id``-scoped table or raise 404.

    The 404 (rather than 403) is deliberate: it does not reveal whether a row
    exists in another tenant.
    """
    obj = scoped(db, model, user).filter(model.id == obj_id).first()  # type: ignore[attr-defined]
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{model.__name__} not found",
        )
    return obj


def paginate(query, limit: int, offset: int) -> dict[str, Any]:
    total = query.count()
    items = query.limit(limit).offset(offset).all()
    return {"items": items, "total": total}


def clamp_pagination(limit: int = 50, offset: int = 0) -> tuple[int, int]:
    return max(1, min(limit, 500)), max(0, offset)
