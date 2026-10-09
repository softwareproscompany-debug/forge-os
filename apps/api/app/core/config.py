"""Application settings — every environment variable from CONTRACTS.md.

All values have dev-friendly defaults matching the contract; production
overrides them via the environment (or docker-compose / k8s overlays).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -- database / queue -------------------------------------------------
    DATABASE_URL: str = "postgresql+psycopg2://forge:forge@postgres:5432/forge"
    REDIS_URL: str = "redis://redis:6379/0"

    # -- auth --------------------------------------------------------------
    JWT_SECRET: str = "dev-secret-change-me"
    JWT_EXPIRE_MINUTES: int = 1440
    WEBHOOK_SECRET: str = "dev-webhook-secret"

    # -- llm ---------------------------------------------------------------
    LLM_PROVIDER: str = "stub"  # stub | anthropic | openai_compatible
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-5-20250929"
    OPENAI_COMPAT_BASE_URL: str = ""
    OPENAI_COMPAT_API_KEY: str = ""
    OPENAI_COMPAT_MODEL: str = ""

    # -- channels ----------------------------------------------------------
    CHANNEL_MODE: str = "stub"  # stub | live
    EMAIL_PROVIDER: str = "smtp"  # smtp | sendgrid | ses
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@example.com"
    SENDGRID_API_KEY: str = ""
    SES_REGION: str = ""
    SES_ACCESS_KEY: str = ""
    SES_SECRET_KEY: str = ""
    SMS_PROVIDER: str = "twilio"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""
    SOCIAL_META_PAGE_TOKEN: str = ""
    SOCIAL_LINKEDIN_TOKEN: str = ""
    SOCIAL_X_API_KEY: str = ""
    SOCIAL_X_API_SECRET: str = ""

    # -- misc --------------------------------------------------------------
    SEED_DEMO: bool = True
    VITE_API_URL: str = "http://localhost:8000"
    CORS_ORIGINS: str = "*"  # comma-separated list; "*" = allow all (dev default)
    API_PREFIX: str = "/api/v1"

    # -- draven -------------------------------------------------------------
    # Fernet key (base64 urlsafe, 32 bytes) for encrypting per-business
    # provider secrets in draven_provider_config. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If empty, PUT /draven/provider fails closed (400) rather than storing
    # plaintext secrets.
    DRAVEN_CONFIG_KEY: str = ""


def get_settings() -> Settings:
    return Settings()
