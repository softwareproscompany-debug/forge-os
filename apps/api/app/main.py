"""ForgeOS API application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.startup_checks import run_startup_checks
from app.routers import (
    affiliates,
    alpha,
    analytics,
    assets,
    assistant,
    audit_log,
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
from app.routers import affiliate_vault as affiliate_vault_router


def create_app(settings=None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    app = FastAPI(title="ForgeOS API", version="0.1.0")

    # Fail-closed startup checks (H1 JWT guard, M2 CORS guard). These raise
    # RuntimeError on weak config instead of serving insecurely.
    origins = run_startup_checks(settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # OWASP Secure Headers: CSP, HSTS (prod only), X-Frame-Options, etc.
    # HSTS is only meaningful behind TLS; emitting it on plain-HTTP dev
    # hosts would pin browsers to HTTPS for localhost.
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_enabled=(settings.ENV == "production"),
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
        billing.subscription_router,
        templates.router,
        assets.router,
        audit_log.router,
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
        affiliate_vault_router.router,
        compliance.router,
        travel.router,
    ):
        app.include_router(router, prefix=prefix)

    # Public affiliate short-link redirect — deliberately outside /api/v1
    # and without auth (see routers/affiliates.py).
    app.include_router(affiliates.redirect_router)

    return app


app = create_app()
