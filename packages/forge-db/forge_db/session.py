"""Database engine, session factory, and FastAPI dependency for forge-db."""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forge_db.models import Base

__all__ = ["Base", "get_engine", "SessionLocal", "get_db"]


@lru_cache(maxsize=1)
def get_engine(database_url: str) -> Engine:
    """Create (once per process) the SQLAlchemy engine for ``database_url``."""
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        # SQLite needs check_same_thread=False for TestClient usage.
        connect_args["check_same_thread"] = False
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def SessionLocal(database_url: str) -> sessionmaker[Session]:
    """Session factory bound to the engine for ``database_url``.

    Prefer the FastAPI ``get_db`` dependency below in request handlers; use
    this directly in workers / scripts.
    """
    return sessionmaker(
        bind=get_engine(database_url),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def get_db(database_url: str) -> Generator[Session, None, None]:
    """Yield a session and close it afterwards (FastAPI ``Depends`` target)."""
    factory = SessionLocal(database_url)
    db = factory()
    try:
        yield db
    finally:
        db.close()
