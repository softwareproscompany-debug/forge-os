"""Event ingestion: persist the event row, enqueue ``handle_event``.

Like ``/assets/generate``, enqueueing is best-effort — the event row is
always persisted and a Redis outage surfaces as ``job_id: null`` + warning.
"""

from __future__ import annotations

from fastapi import APIRouter

from forge_db.models import Contact, Event

from app import schemas
from app.core.deps import CurrentSettings, CurrentUser, DbSession, get_owned_or_404
from app.core.queue import enqueue_job

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=schemas.EventResponse, status_code=201)
async def create_event(
    payload: schemas.EventCreate,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
):
    if payload.contact_id is not None:
        get_owned_or_404(db, Contact, payload.contact_id, user)

    event = Event(
        business_id=user.business_id,
        contact_id=payload.contact_id,
        kind=payload.kind,
        payload=dict(payload.payload),
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    job_id = await enqueue_job(settings.REDIS_URL, "handle_event", str(event.id))
    warning = (
        None
        if job_id is not None
        else "Redis unreachable: event recorded, handle_event job not queued"
    )
    return schemas.EventResponse(event_id=event.id, job_id=job_id, warning=warning)
