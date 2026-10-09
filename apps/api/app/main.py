"""ForgeOS API application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import (
    affiliates,
    alpha,
    analytics,
    assets,
    assistant,
    auth,
    autopilot,
    billing,
    brand_kits,
    businesses,
    campaigns,
    compliance,
    contacts,
    draven,
    events,
    interview,
    knowledge,
    market_intel,
    meetings,
    ops,
    outbox,
    partner_portal,
    partners,
    pipeline,
    templates,
    travel,
    webhooks,
)
from app.routers import settings as settings_router


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="ForgeOS API", version="0.1.0")

    origins = (
        ["*"]
        if settings.CORS_ORIGINS.strip() == "*"
        else [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    prefix = settings.API_PREFIX
    for router in (
        auth.router,
        businesses.router,
        brand_kits.router,
        contacts.router,
        pipeline.router,
        meetings.router,
        knowledge.router,
        partners.router,
        partners.applications_router,
        partners.tiers_router,
        partner_portal.router,
        billing.router,
        templates.router,
        assets.router,
        campaigns.router,
        autopilot.router,
        analytics.router,
        outbox.router,
        ops.router,
        webhooks.router,
        events.router,
        interview.router,
        affiliates.router,
        alpha.router,
        assistant.router,
        draven.router,
        market_intel.router,
        settings_router.router,
        compliance.router,
        travel.router,
    ):
        app.include_router(router, prefix=prefix)

    # Public affiliate short-link redirect — deliberately outside /api/v1
    # and without auth (see routers/affiliates.py).
    app.include_router(affiliates.redirect_router)

    return app


app = create_app()
