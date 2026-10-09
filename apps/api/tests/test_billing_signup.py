"""Tests for the customer subscription billing flow (Stripe Checkout signup).

Covers: checkout session creation (Stripe SDK mocked), webhook signature
verification, business creation on checkout.session.completed (idempotent),
subscription cancellation sync, and the complete-signup flow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_123")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_123")
os.environ.setdefault("STRIPE_PRICE_STARTER", "price_starter_123")
os.environ.setdefault("STRIPE_PRICE_PRO", "price_pro_123")
os.environ.setdefault("STRIPE_PRICE_ENTERPRISE", "price_ent_123")

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import Base, Business, User

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_billing_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

test_settings = Settings(
DATABASE_URL="sqlite://",
    REDIS_URL="redis://127.0.0.1:9/0",
JWT_SECRET="test-secret",
WEBHOOK_SECRET="test-webhook-secret",
    STRIPE_SECRET_KEY="sk_test_123",
    STRIPE_WEBHOOK_SECRET="whsec_test_123",
    STRIPE_PRICE_STARTER="price_starter_123",
    STRIPE_PRICE_PRO="price_pro_123",
    STRIPE_PRICE_ENTERPRISE="price_ent_123",
)

app = create_app()
app.dependency_overrides[get_settings_dep] = lambda: test_settings


def _override_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db_session] = _override_db

client = TestClient(app)


def _sign(body: bytes, secret: str = "whsec_test_123") -> str:
    ts = "1700000000"
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _webhook_post(payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/v1/billing/subscription-webhook",
        content=body,
        headers={
            "Stripe-Signature": _sign(body),
            "Content-Type": "application/json",
        },
    )


def _db():
    return TestingSession()


def test_checkout_creates_session():
    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.com/pay/cs_test_123"
    fake_session.id = "cs_test_123"
    with patch("stripe.checkout.Session.create", return_value=fake_session) as mock_create:
        r = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "professional", "email": "buyer@example.com"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"] == "https://checkout.stripe.com/pay/cs_test_123"
    assert body["session_id"] == "cs_test_123"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["mode"] == "subscription"
    assert kwargs["line_items"] == [{"price": "price_pro_123", "quantity": 1}]
    assert kwargs["customer_email"] == "buyer@example.com"
    assert kwargs["metadata"] == {"plan": "professional"}


def test_checkout_rejects_bad_plan():
    r = client.post("/api/v1/billing/checkout", json={"plan": "free"})
    assert r.status_code == 422


def test_checkout_requires_configured_price(monkeypatch):
    # Temporarily blank the starter price via settings override
    s = Settings(
DATABASE_URL="sqlite://",
JWT_SECRET="test-secret",
        STRIPE_SECRET_KEY="sk_test_123",
        STRIPE_PRICE_STARTER="",
    )
    app.dependency_overrides[get_settings_dep] = lambda: s
    try:
        r = client.post("/api/v1/billing/checkout", json={"plan": "starter"})
        assert r.status_code == 503
        assert "STRIPE_PRICE_STARTER" in r.json()["detail"]
    finally:
        app.dependency_overrides[get_settings_dep] = lambda: test_settings


def test_webhook_rejects_bad_signature():
    body = json.dumps({"id": "evt_1", "type": "checkout.session.completed"}).encode()
    r = client.post(
        "/api/v1/billing/subscription-webhook",
        content=body,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_webhook_requires_signature():
    r = client.post(
        "/api/v1/billing/subscription-webhook",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_checkout_completed_creates_pending_business():
    db = _db()
    try:
        r = _webhook_post(
            {
                "id": "evt_checkout_1",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_test_abc",
                        "customer": "cus_test_abc",
                        "subscription": "sub_test_abc",
                        "customer_details": {"email": "newbiz@example.com"},
                        "metadata": {"plan": "starter"},
                    }
                },
            }
        )
        assert r.status_code == 200, r.text
        biz = db.query(Business).filter(Business.stripe_session_id == "cs_test_abc").first()
        assert biz is not None
        assert biz.subscription_status == "pending"
        assert biz.subscription_plan == "starter"
        assert biz.stripe_customer_id == "cus_test_abc"
        assert biz.stripe_subscription_id == "sub_test_abc"
    finally:
        db.close()


def test_checkout_completed_is_idempotent():
    db = _db()
    try:
        payload = {
            "id": "evt_checkout_2",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_dup",
                    "customer": "cus_dup",
                    "subscription": "sub_dup",
                    "customer_details": {"email": "dup@example.com"},
                    "metadata": {"plan": "starter"},
                }
            },
        }
        r1 = _webhook_post(payload)
        r2 = _webhook_post(payload)
        assert r1.status_code == 200 and r2.status_code == 200
        count = db.query(Business).filter(Business.stripe_session_id == "cs_test_dup").count()
        assert count == 1
    finally:
        db.close()


def test_subscription_deleted_marks_canceled():
    db = _db()
    try:
        biz = Business(
            name="Paying Co",
            slug=f"paying-co-{uuid.uuid4().hex[:8]}",
            stripe_customer_id="cus_cancel_me",
            subscription_status="active",
            subscription_plan="professional",
        )
        db.add(biz)
        db.commit()

        r = _webhook_post(
            {
                "id": "evt_sub_del_1",
                "type": "customer.subscription.deleted",
                "data": {
                    "object": {"id": "sub_x", "customer": "cus_cancel_me", "status": "canceled"}
                },
            }
        )
        assert r.status_code == 200
        db.refresh(biz)
        assert biz.subscription_status == "canceled"
    finally:
        db.close()


def test_complete_signup_creates_user_and_returns_token():
    db = _db()
    try:
        biz = Business(
            name="placeholder",
            slug=f"placeholder-{uuid.uuid4().hex[:8]}",
            stripe_customer_id="cus_signup_1",
            stripe_session_id="cs_signup_1",
            subscription_status="pending",
            subscription_plan="starter",
        )
        db.add(biz)
        db.commit()
        biz_id = biz.id

        fake_session = MagicMock()
        fake_session.payment_status = "paid"
        fake_session.customer_details.email = "founder@newco.com"
        with patch("stripe.checkout.Session.retrieve", return_value=fake_session):
            r = client.post(
                "/api/v1/billing/complete-signup",
                json={
                    "session_id": "cs_signup_1",
                    "business_name": "NewCo LLC",
                    "full_name": "Founder Name",
                    "password": "supersecret1",
                },
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["business_name"] == "NewCo LLC"
        assert body["token"]

        db.refresh(biz)
        assert biz.name == "NewCo LLC"
        assert biz.subscription_status == "active"
        assert biz.stripe_session_id is None  # single-use

        user = db.query(User).filter(User.email == "founder@newco.com").first()
        assert user is not None
        assert user.business_id == biz_id
        assert user.role.value == "owner"
    finally:
        db.close()


def test_complete_signup_rejects_unpaid():
    db = _db()
    try:
        biz = Business(
            name="placeholder",
            slug=f"placeholder-{uuid.uuid4().hex[:8]}",
            stripe_session_id="cs_unpaid_1",
            subscription_status="pending",
        )
        db.add(biz)
        db.commit()

        fake_session = MagicMock()
        fake_session.payment_status = "unpaid"
        with patch("stripe.checkout.Session.retrieve", return_value=fake_session):
            r = client.post(
                "/api/v1/billing/complete-signup",
                json={
                    "session_id": "cs_unpaid_1",
                    "business_name": "X",
                    "full_name": "Y",
                    "password": "supersecret1",
                },
            )
        assert r.status_code == 402
    finally:
        db.close()


def test_complete_signup_rejects_unknown_session():
    r = client.post(
        "/api/v1/billing/complete-signup",
        json={
            "session_id": "cs_nope",
            "business_name": "X",
            "full_name": "Y",
            "password": "supersecret1",
        },
    )
    assert r.status_code == 404


def test_complete_signup_rejects_reused_session():
    """A session ID can't be used twice (stripe_session_id cleared after use)."""
    db = _db()
    try:
        biz = Business(
            name="Used Co",
            slug=f"used-co-{uuid.uuid4().hex[:8]}",
            stripe_session_id="cs_used_1",
            subscription_status="active",  # already completed
        )
        db.add(biz)
        db.commit()

        # Simulate the "already used" state: session_id cleared → 404
        biz.stripe_session_id = None
        db.commit()

        r = client.post(
            "/api/v1/billing/complete-signup",
            json={
                "session_id": "cs_used_1",
                "business_name": "X",
                "full_name": "Y",
                "password": "supersecret1",
            },
        )
        assert r.status_code == 404
    finally:
        db.close()
