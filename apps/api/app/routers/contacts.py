"""Contact CRUD + consent management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query

from forge_db.models import Contact

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=schemas.Page[schemas.ContactOut])
def list_contacts(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Contact, user).order_by(Contact.created_at.desc())
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.ContactOut, status_code=201)
def create_contact(
    payload: schemas.ContactCreate, user: CurrentUser, db: DbSession
):
    contact = Contact(business_id=user.business_id, **payload.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


@router.get("/{contact_id}", response_model=schemas.ContactOut)
def get_contact(contact_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, Contact, contact_id, user)


@router.put("/{contact_id}", response_model=schemas.ContactOut)
def update_contact(
    contact_id: uuid.UUID,
    payload: schemas.ContactUpdate,
    user: CurrentUser,
    db: DbSession,
):
    contact = get_owned_or_404(db, Contact, contact_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    db.commit()
    db.refresh(contact)
    return contact


@router.delete("/{contact_id}", status_code=204)
def delete_contact(contact_id: uuid.UUID, user: CurrentUser, db: DbSession):
    contact = get_owned_or_404(db, Contact, contact_id, user)
    db.delete(contact)
    db.commit()
    return None


@router.post("/{contact_id}/consent", response_model=schemas.ContactOut)
def set_consent(
    contact_id: uuid.UUID,
    payload: schemas.ConsentRequest,
    user: CurrentUser,
    db: DbSession,
):
    """Grant or revoke email/SMS consent (timestamps recorded on grant)."""
    contact = get_owned_or_404(db, Contact, contact_id, user)
    now = datetime.now(timezone.utc)
    if payload.channel == "email":
        contact.consent_email = payload.granted
        contact.consent_email_at = now if payload.granted else None
    else:
        contact.consent_sms = payload.granted
        contact.consent_sms_at = now if payload.granted else None
    db.commit()
    db.refresh(contact)
    return contact
