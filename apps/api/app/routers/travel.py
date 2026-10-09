"""Travel Agency workspace — Phase 1: travel CRM foundation.

Internal SaaS inside ForgeOS. One travel agency company == one
``businesses`` row; every endpoint is tenant-scoped by the caller's
``business_id`` (from the JWT). Cross-tenant IDs return 404, never 403,
so existence is not leaked. No supplier integrations, bookings, quotes,
payments, or corporate policies in Phase 1 — CRM only.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status as http_status
from sqlalchemy import func, or_

from forge_db.models import (
    TravelCustomer,
    TravelLead,
    TravelLeadStatus,
    TravelerProfile,
    TripRequest,
    TripRequestStatus,
)

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/travel", tags=["travel"])

# Allowed lead status transitions: new -> qualified -> quoted -> booked | lost.
# ``lost`` is terminal; ``booked`` is terminal (a booking record, Phase 2+,
# carries the lifecycle from here).
_LEAD_TRANSITIONS: dict[str, set[str]] = {
    "new": {"qualified", "lost"},
    "qualified": {"quoted", "lost", "new"},
    "quoted": {"booked", "lost", "qualified"},
    "booked": set(),
    "lost": {"new"},  # re-open a lost lead
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_model=schemas.TravelDashboardOut)
def travel_dashboard(user: CurrentUser, db: DbSession):
    """Workspace dashboard: lead pipeline counts, recent leads, totals."""
    counts = (
        db.query(TravelLead.status, func.count(TravelLead.id))
        .filter(TravelLead.business_id == user.business_id)
        .group_by(TravelLead.status)
        .all()
    )
    lead_counts = {s.value: 0 for s in TravelLeadStatus}
    for status, n in counts:
        lead_counts[status.value if hasattr(status, "value") else str(status)] = n
    recent = (
        scoped(db, TravelLead, user)
        .order_by(TravelLead.created_at.desc())
        .limit(10)
        .all()
    )
    customer_count = (
        db.query(func.count(TravelCustomer.id))
        .filter(TravelCustomer.business_id == user.business_id)
        .scalar()
        or 0
    )
    trip_request_count = (
        db.query(func.count(TripRequest.id))
        .filter(TripRequest.business_id == user.business_id)
        .scalar()
        or 0
    )
    return schemas.TravelDashboardOut(
        lead_counts=lead_counts,
        recent_leads=[schemas.TravelLeadOut.model_validate(l) for l in recent],
        customer_count=customer_count,
        trip_request_count=trip_request_count,
    )


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@router.get("/customers", response_model=schemas.Page[schemas.TravelCustomerOut])
def list_customers(
    user: CurrentUser,
    db: DbSession,
    q: str | None = Query(default=None, description="Search name/email/phone"),
    type: str | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, TravelCustomer, user).order_by(TravelCustomer.created_at.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                TravelCustomer.name.ilike(like),
                TravelCustomer.email.ilike(like),
                TravelCustomer.phone.ilike(like),
            )
        )
    if type in ("individual", "corporate"):
        query = query.filter(TravelCustomer.type == type)
    return paginate(query, limit, offset)


@router.post("/customers", response_model=schemas.TravelCustomerOut, status_code=201)
def create_customer(payload: schemas.TravelCustomerCreate, user: CurrentUser, db: DbSession):
    customer = TravelCustomer(business_id=user.business_id, **payload.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.get("/customers/{customer_id}", response_model=schemas.TravelCustomerOut)
def get_customer(customer_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, TravelCustomer, customer_id, user)


@router.put("/customers/{customer_id}", response_model=schemas.TravelCustomerOut)
def update_customer(
    customer_id: uuid.UUID,
    payload: schemas.TravelCustomerUpdate,
    user: CurrentUser,
    db: DbSession,
):
    customer = get_owned_or_404(db, TravelCustomer, customer_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    db.commit()
    db.refresh(customer)
    return customer


@router.delete("/customers/{customer_id}", status_code=204)
def delete_customer(customer_id: uuid.UUID, user: CurrentUser, db: DbSession):
    customer = get_owned_or_404(db, TravelCustomer, customer_id, user)
    db.delete(customer)
    db.commit()
    return None


@router.get(
    "/customers/{customer_id}/travelers",
    response_model=schemas.Page[schemas.TravelerProfileOut],
)
def list_customer_travelers(
    customer_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    """Travelers attached to one customer (customer must belong to the caller)."""
    get_owned_or_404(db, TravelCustomer, customer_id, user)
    limit, offset = clamp_pagination(limit, offset)
    query = (
        scoped(db, TravelerProfile, user)
        .filter(TravelerProfile.customer_id == customer_id)
        .order_by(TravelerProfile.full_name.asc())
    )
    return paginate(query, limit, offset)


# ---------------------------------------------------------------------------
# Travelers
# ---------------------------------------------------------------------------


@router.get("/travelers", response_model=schemas.Page[schemas.TravelerProfileOut])
def list_travelers(
    user: CurrentUser,
    db: DbSession,
    q: str | None = Query(default=None, description="Search full name"),
    customer_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, TravelerProfile, user).order_by(TravelerProfile.full_name.asc())
    if q:
        query = query.filter(TravelerProfile.full_name.ilike(f"%{q}%"))
    if customer_id:
        query = query.filter(TravelerProfile.customer_id == customer_id)
    return paginate(query, limit, offset)


@router.post("/travelers", response_model=schemas.TravelerProfileOut, status_code=201)
def create_traveler(
    payload: schemas.TravelerProfileCreate, user: CurrentUser, db: DbSession
):
    # A referenced customer must belong to the caller's business.
    if payload.customer_id:
        get_owned_or_404(db, TravelCustomer, payload.customer_id, user)
    traveler = TravelerProfile(business_id=user.business_id, **payload.model_dump())
    db.add(traveler)
    db.commit()
    db.refresh(traveler)
    return traveler


@router.get("/travelers/{traveler_id}", response_model=schemas.TravelerProfileOut)
def get_traveler(traveler_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, TravelerProfile, traveler_id, user)


@router.put("/travelers/{traveler_id}", response_model=schemas.TravelerProfileOut)
def update_traveler(
    traveler_id: uuid.UUID,
    payload: schemas.TravelerProfileUpdate,
    user: CurrentUser,
    db: DbSession,
):
    traveler = get_owned_or_404(db, TravelerProfile, traveler_id, user)
    data = payload.model_dump(exclude_unset=True)
    if data.get("customer_id"):
        get_owned_or_404(db, TravelCustomer, data["customer_id"], user)
    for field, value in data.items():
        setattr(traveler, field, value)
    db.commit()
    db.refresh(traveler)
    return traveler


@router.delete("/travelers/{traveler_id}", status_code=204)
def delete_traveler(traveler_id: uuid.UUID, user: CurrentUser, db: DbSession):
    traveler = get_owned_or_404(db, TravelerProfile, traveler_id, user)
    db.delete(traveler)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@router.get("/leads", response_model=schemas.Page[schemas.TravelLeadOut])
def list_leads(
    user: CurrentUser,
    db: DbSession,
    status: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Search destination/purpose/source"),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, TravelLead, user).order_by(TravelLead.created_at.desc())
    if status and status in _LEAD_TRANSITIONS:
        query = query.filter(TravelLead.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                TravelLead.destination.ilike(like),
                TravelLead.trip_purpose.ilike(like),
                TravelLead.source.ilike(like),
            )
        )
    return paginate(query, limit, offset)


@router.post("/leads", response_model=schemas.TravelLeadOut, status_code=201)
def create_lead(payload: schemas.TravelLeadCreate, user: CurrentUser, db: DbSession):
    data = payload.model_dump()
    if data.get("customer_id"):
        get_owned_or_404(db, TravelCustomer, data["customer_id"], user)
    if data.get("assigned_to"):
        # Assignment is informational; no cross-tenant user lookup performed.
        pass
    lead = TravelLead(business_id=user.business_id, **data)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


@router.get("/leads/{lead_id}", response_model=schemas.TravelLeadOut)
def get_lead(lead_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, TravelLead, lead_id, user)


@router.put("/leads/{lead_id}", response_model=schemas.TravelLeadOut)
def update_lead(
    lead_id: uuid.UUID,
    payload: schemas.TravelLeadUpdate,
    user: CurrentUser,
    db: DbSession,
):
    lead = get_owned_or_404(db, TravelLead, lead_id, user)
    data = payload.model_dump(exclude_unset=True)
    if data.get("customer_id"):
        get_owned_or_404(db, TravelCustomer, data["customer_id"], user)
    for field, value in data.items():
        setattr(lead, field, value)
    db.commit()
    db.refresh(lead)
    return lead


@router.post("/leads/{lead_id}/status", response_model=schemas.TravelLeadOut)
def transition_lead_status(
    lead_id: uuid.UUID,
    payload: schemas.TravelLeadStatusUpdate,
    user: CurrentUser,
    db: DbSession,
):
    """Move a lead through the pipeline. Invalid transitions are rejected (422)."""
    lead = get_owned_or_404(db, TravelLead, lead_id, user)
    current = lead.status.value if hasattr(lead.status, "value") else str(lead.status)
    target = payload.status
    if target == current:
        return lead
    allowed = _LEAD_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot transition lead from {current!r} to {target!r}. "
            f"Allowed: {sorted(allowed) or 'none'}.",
        )
    lead.status = TravelLeadStatus(target)
    db.commit()
    db.refresh(lead)
    return lead


@router.delete("/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: uuid.UUID, user: CurrentUser, db: DbSession):
    lead = get_owned_or_404(db, TravelLead, lead_id, user)
    db.delete(lead)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Trip requests
# ---------------------------------------------------------------------------


@router.get("/trip-requests", response_model=schemas.Page[schemas.TripRequestOut])
def list_trip_requests(
    user: CurrentUser,
    db: DbSession,
    status: str | None = Query(default=None),
    lead_id: uuid.UUID | None = Query(default=None),
    customer_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, TripRequest, user).order_by(TripRequest.created_at.desc())
    if status:
        query = query.filter(TripRequest.status == status)
    if lead_id:
        query = query.filter(TripRequest.lead_id == lead_id)
    if customer_id:
        query = query.filter(TripRequest.customer_id == customer_id)
    return paginate(query, limit, offset)


@router.post("/trip-requests", response_model=schemas.TripRequestOut, status_code=201)
def create_trip_request(
    payload: schemas.TripRequestCreate, user: CurrentUser, db: DbSession
):
    data = payload.model_dump()
    if data.get("lead_id"):
        get_owned_or_404(db, TravelLead, data["lead_id"], user)
    if data.get("customer_id"):
        get_owned_or_404(db, TravelCustomer, data["customer_id"], user)
    trip_request = TripRequest(business_id=user.business_id, **data)
    db.add(trip_request)
    db.commit()
    db.refresh(trip_request)
    return trip_request


@router.get("/trip-requests/{request_id}", response_model=schemas.TripRequestOut)
def get_trip_request(request_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, TripRequest, request_id, user)


@router.put("/trip-requests/{request_id}", response_model=schemas.TripRequestOut)
def update_trip_request(
    request_id: uuid.UUID,
    payload: schemas.TripRequestUpdate,
    user: CurrentUser,
    db: DbSession,
):
    trip_request = get_owned_or_404(db, TripRequest, request_id, user)
    data = payload.model_dump(exclude_unset=True)
    if data.get("lead_id"):
        get_owned_or_404(db, TravelLead, data["lead_id"], user)
    if data.get("customer_id"):
        get_owned_or_404(db, TravelCustomer, data["customer_id"], user)
    for field, value in data.items():
        setattr(trip_request, field, value)
    db.commit()
    db.refresh(trip_request)
    return trip_request


@router.delete("/trip-requests/{request_id}", status_code=204)
def delete_trip_request(request_id: uuid.UUID, user: CurrentUser, db: DbSession):
    trip_request = get_owned_or_404(db, TripRequest, request_id, user)
    db.delete(trip_request)
    db.commit()
    return None
