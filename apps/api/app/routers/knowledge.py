"""Knowledge-base document CRUD (OS Knowledge screen).

Plain text store for business context (company facts, FAQs, policies,
product notes). Wiring documents into Draven's prompt context is a
later step — this is the store + API.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from forge_db.models import KnowledgeDoc

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("", response_model=schemas.Page[schemas.KnowledgeDocOut])
def list_docs(
    user: CurrentUser,
    db: DbSession,
    q: str | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, KnowledgeDoc, user).order_by(KnowledgeDoc.created_at.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(
            (KnowledgeDoc.title.ilike(like)) | (KnowledgeDoc.content.ilike(like))
        )
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.KnowledgeDocOut, status_code=201)
def create_doc(
    payload: schemas.KnowledgeDocCreate, user: CurrentUser, db: DbSession
):
    doc = KnowledgeDoc(business_id=user.business_id, **payload.model_dump())
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/{doc_id}", response_model=schemas.KnowledgeDocOut)
def get_doc(doc_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, KnowledgeDoc, doc_id, user)


@router.put("/{doc_id}", response_model=schemas.KnowledgeDocOut)
def update_doc(
    doc_id: uuid.UUID,
    payload: schemas.KnowledgeDocUpdate,
    user: CurrentUser,
    db: DbSession,
):
    doc = get_owned_or_404(db, KnowledgeDoc, doc_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    db.commit()
    db.refresh(doc)
    return doc


@router.delete("/{doc_id}", status_code=204)
def delete_doc(doc_id: uuid.UUID, user: CurrentUser, db: DbSession):
    doc = get_owned_or_404(db, KnowledgeDoc, doc_id, user)
    db.delete(doc)
    db.commit()
    return None
