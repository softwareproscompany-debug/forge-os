"""Template CRUD + Jinja2 preview rendering."""

from __future__ import annotations

import uuid

import jinja2
from fastapi import APIRouter, HTTPException, Query, status

from forge_db.models import Template

from app import schemas
from app.core.deps import (
    CurrentUser,
    DbSession,
    clamp_pagination,
    get_owned_or_404,
    paginate,
    scoped,
)

router = APIRouter(prefix="/templates", tags=["templates"])

_jinja_env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)


def check_template_syntax(source: str | None) -> None:
    """Fail fast on invalid Jinja2 at write time (syntax only, not variables)."""
    if source is None:
        return
    try:
        _jinja_env.parse(source)
    except jinja2.TemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid template: {exc}",
        ) from exc


def render_template_string(source: str | None, variables: dict) -> str | None:
    """Render a Jinja2 template; 422 on missing variables or bad syntax."""
    if source is None:
        return None
    try:
        return _jinja_env.from_string(source).render(**variables)
    except jinja2.UndefinedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Missing template variable: {exc}",
        ) from exc
    except jinja2.TemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid template: {exc}",
        ) from exc


@router.get("", response_model=schemas.Page[schemas.TemplateOut])
def list_templates(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    limit, offset = clamp_pagination(limit, offset)
    query = scoped(db, Template, user).order_by(Template.created_at.desc())
    return paginate(query, limit, offset)


@router.post("", response_model=schemas.TemplateOut, status_code=201)
def create_template(
    payload: schemas.TemplateCreate, user: CurrentUser, db: DbSession
):
    # Fail fast on invalid Jinja2 at creation time (syntax only).
    check_template_syntax(payload.body_template)
    template = Template(business_id=user.business_id, **payload.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.get("/{template_id}", response_model=schemas.TemplateOut)
def get_template(template_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return get_owned_or_404(db, Template, template_id, user)


@router.put("/{template_id}", response_model=schemas.TemplateOut)
def update_template(
    template_id: uuid.UUID,
    payload: schemas.TemplateUpdate,
    user: CurrentUser,
    db: DbSession,
):
    template = get_owned_or_404(db, Template, template_id, user)
    updates = payload.model_dump(exclude_unset=True)
    if "body_template" in updates:
        check_template_syntax(updates["body_template"])
    for field, value in updates.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: uuid.UUID, user: CurrentUser, db: DbSession):
    template = get_owned_or_404(db, Template, template_id, user)
    db.delete(template)
    db.commit()
    return None


@router.post("/{template_id}/preview", response_model=schemas.TemplatePreviewResponse)
def preview_template(
    template_id: uuid.UUID,
    payload: schemas.TemplatePreviewRequest,
    user: CurrentUser,
    db: DbSession,
):
    """Render subject + body with the supplied variables (422 on missing var)."""
    template = get_owned_or_404(db, Template, template_id, user)
    return schemas.TemplatePreviewResponse(
        subject=render_template_string(template.subject_template, payload.variables),
        body=render_template_string(template.body_template, payload.variables) or "",
    )
