"""ForgeOS API application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import (
    analytics,
    assets,
    auth,
    autopilot,
    brand_kits,
    businesses,
    campaigns,
    contacts,
    events,
    interview,
    ops,
    outbox,
    templates,
    webhooks,
)


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
    ):
        app.include_router(router, prefix=prefix)

    return app


app = create_app()
