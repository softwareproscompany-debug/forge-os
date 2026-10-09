"""Tests for the Card 4 autopilot planner job (apps/worker/worker/jobs.py).

* Drafting creates exactly 3 items (Tue email / Thu social / Sat SMS) with
  deterministic stub titles on the stub LLM provider.
* The email item reuses the summary's top asset when it is an email_copy
  asset, else the newest approved email_copy asset, else null.
* A second run for the same (business, week) is a no-op (idempotent skip).
* Timezone gating: the job drafts only during the business's configured
  plan window (default local Monday 06:xx; stdlib zoneinfo, UTC fallback).
* Per-tenant schedule: custom plan_day/plan_hour are honored; the biweekly
  cadence drafts at most once per ~13 days (via last_planned_at).
* ``autopilot_plan_now`` drafts immediately, ignoring the schedule, and is
  idempotent per week.

Everything runs against fresh SQLite files; ``LLM_PROVIDER=stub``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("CHANNEL_MODE", "stub")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from forge_db.models import (  # noqa: E402
    Asset,
    AssetKind,
    AssetStatus,
    AutopilotSettings,
    Base,
    BrandKit,
    Business,
    ContentPlan,
    PlanStatus,
    WeeklySummary,
)
from worker import jobs  # noqa: E402
from worker.jobs import autopilot_plan, autopilot_plan_now  # noqa: E402

UTC = timezone.utc
FAKE_CTX = {"job_try": 1}

#: A Monday (verified: 2026-10-12 is a Monday).
MONDAY = date(2026, 10, 12)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    path = tmp_path / f"autopilot_test_{uuid.uuid4().hex}.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield url
    engine.dispose()


@pytest.fixture()
def db(db_url):
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


def _fresh_session(db_url) -> Session:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    return Session(engine)


def _business(db: Session, **kw) -> Business:
    b = Business(
        name=kw.get("name", "Acme Inc"),
        slug=kw.get("slug", f"acme-{uuid.uuid4().hex[:8]}"),
        timezone=kw.get("timezone", "UTC"),
    )
    db.add(b)
    db.flush()
    return b


def _brand_kit(db: Session, business: Business, **kw) -> BrandKit:
    bk = BrandKit(
        business_id=business.id,
        name=kw.get("name", "Main kit"),
        voice_description=kw.get("voice_description", "playful but professional"),
        version=kw.get("version", 1),
    )
    db.add(bk)
    db.flush()
    return bk


def _summary(db: Session, business: Business, **kw) -> WeeklySummary:
    s = WeeklySummary(
        business_id=business.id,
        week_start=kw.get("week_start", MONDAY - timedelta(days=7)),
        top_assets=kw.get("top_assets", []),
        bottom_assets=[],
        best_channel_per_segment={},
        recommendation=kw.get(
            "recommendation",
            "Double down on the welcome email: it converted 3x better than average.",
        ),
    )
    db.add(s)
    db.flush()
    return s


def _asset(db: Session, business: Business, **kw) -> Asset:
    a = Asset(
        business_id=business.id,
        kind=kw.get("kind", AssetKind.email_copy),
        title=kw.get("title", "Welcome email"),
        body=kw.get("body", "Hi there!"),
        status=kw.get("status", AssetStatus.approved),
    )
    db.add(a)
    db.flush()
    return a


def _plan(db_url, business_id) -> ContentPlan | None:
    session = _fresh_session(db_url)
    try:
        return (
            session.query(ContentPlan)
            .filter(ContentPlan.business_id == business_id)
            .one_or_none()
        )
    finally:
        session.close()


def _run_job(db_url, when: datetime, monkeypatch):
    monkeypatch.setattr(jobs, "utcnow", lambda: when)
    import asyncio

    return asyncio.run(autopilot_plan(FAKE_CTX))


def _monday_6am_utc() -> datetime:
    return datetime(2026, 10, 12, 6, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------


class TestDrafting:
    def test_drafts_three_items_on_monday_6am(self, db, db_url, monkeypatch):
        business = _business(db)
        _brand_kit(db, business)
        _summary(db, business)
        email_asset = _asset(db, business, kind=AssetKind.email_copy)
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["ok"] is True
        assert result["drafted"] == 1

        plan = _plan(db_url, business.id)
        assert plan is not None
        assert plan.status == PlanStatus.draft
        assert plan.week_start == MONDAY

        items = plan.items
        assert len(items) == 3
        assert [i["kind"] for i in items] == ["email_copy", "social_post", "sms"]
        assert [i["channel"] for i in items] == ["email", "social", "sms"]
        assert [i["day"] for i in items] == [1, 3, 5]
        assert items[0]["title"] == "Autopilot: weekly email — 2026-10-12"
        assert items[1]["title"] == "Autopilot: weekly social — 2026-10-12"
        assert items[2]["title"] == "Autopilot: weekly SMS — 2026-10-12"
        # The summary recommendation feeds the social brief (first 200 chars).
        assert "Double down on the welcome email" in items[1]["brief"]
        # Newest approved email_copy asset is reused on the email item.
        assert items[0]["asset_id"] == str(email_asset.id)
        assert "asset_id" not in items[1]
        assert "asset_id" not in items[2]

    def test_email_item_prefers_summary_top_asset(self, db, db_url, monkeypatch):
        """The summary's top asset wins when it is an email_copy asset."""
        business = _business(db)
        older = _asset(db, business, kind=AssetKind.email_copy, title="Older")
        newer = _asset(db, business, kind=AssetKind.email_copy, title="Newer")
        _summary(
            db,
            business,
            top_assets=[
                {
                    "asset_id": str(older.id),
                    "title": older.title,
                    "kind": "email_copy",
                    "delivered": 100,
                    "converted": 9,
                    "conversion_rate": 0.09,
                }
            ],
        )
        db.commit()

        _run_job(db_url, _monday_6am_utc(), monkeypatch)
        plan = _plan(db_url, business.id)
        assert plan.items[0]["asset_id"] == str(older.id)
        assert newer.id is not None  # sanity: newer existed and was skipped

    def test_summary_top_asset_wrong_kind_falls_back_to_newest_email(
        self, db, db_url, monkeypatch
    ):
        """A top asset of another kind must not leak into the email item."""
        business = _business(db)
        social = _asset(db, business, kind=AssetKind.social_post, title="Top social")
        email = _asset(db, business, kind=AssetKind.email_copy, title="Only email")
        _summary(
            db,
            business,
            top_assets=[
                {
                    "asset_id": str(social.id),
                    "title": social.title,
                    "kind": "social_post",
                    "delivered": 500,
                    "converted": 20,
                    "conversion_rate": 0.04,
                }
            ],
        )
        db.commit()

        _run_job(db_url, _monday_6am_utc(), monkeypatch)
        plan = _plan(db_url, business.id)
        assert plan.items[0]["asset_id"] == str(email.id)

    def test_email_item_asset_id_null_without_approved_email(
        self, db, db_url, monkeypatch
    ):
        business = _business(db)
        _asset(db, business, kind=AssetKind.sms, title="Only SMS")
        db.commit()

        _run_job(db_url, _monday_6am_utc(), monkeypatch)
        plan = _plan(db_url, business.id)
        assert "asset_id" not in plan.items[0]

    def test_idempotent_second_run_skips(self, db, db_url, monkeypatch):
        business = _business(db)
        db.commit()

        first = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert first["drafted"] == 1
        second = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert second["drafted"] == 0
        assert second["skipped_exists"] == 1

        session = _fresh_session(db_url)
        try:
            count = (
                session.query(ContentPlan)
                .filter(ContentPlan.business_id == business.id)
                .count()
            )
        finally:
            session.close()
        assert count == 1

    def test_existing_approved_plan_also_skips(self, db, db_url, monkeypatch):
        business = _business(db)
        db.add(
            ContentPlan(
                business_id=business.id,
                week_start=MONDAY,
                status=PlanStatus.approved,
                items=[],
            )
        )
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["drafted"] == 0
        assert result["skipped_exists"] == 1


# ---------------------------------------------------------------------------
# timezone gating
# ---------------------------------------------------------------------------


class TestTimezoneGating:
    def test_chicago_business_drafts_at_06_00_local(self, db, db_url, monkeypatch):
        """America/Chicago is UTC-5 in October: 11:00 UTC == 06:00 local."""
        business = _business(db, timezone="America/Chicago")
        db.commit()

        result = _run_job(
            db_url, datetime(2026, 10, 12, 11, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 1
        assert _plan(db_url, business.id) is not None

    def test_chicago_business_skips_at_07_00_local(self, db, db_url, monkeypatch):
        business = _business(db, timezone="America/Chicago")
        db.commit()

        result = _run_job(
            db_url, datetime(2026, 10, 12, 12, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 0
        assert result["skipped_not_due"] == 1
        assert _plan(db_url, business.id) is None

    def test_sunday_06_00_local_is_not_a_plan_moment(
        self, db, db_url, monkeypatch
    ):
        """2026-10-11 is a Sunday; 11:00 UTC == Sunday 06:00 in Chicago."""
        business = _business(db, timezone="America/Chicago")
        db.commit()

        result = _run_job(
            db_url, datetime(2026, 10, 11, 11, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 0
        assert result["skipped_not_due"] == 1

    def test_unknown_timezone_falls_back_to_utc(self, db, db_url, monkeypatch):
        business = _business(db, timezone="Not/AZone")
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["drafted"] == 1
        assert _plan(db_url, business.id) is not None


# ---------------------------------------------------------------------------
# per-tenant schedule
# ---------------------------------------------------------------------------


def _settings(db: Session, business: Business, **kw) -> AutopilotSettings:
    s = AutopilotSettings(
        business_id=business.id,
        plan_day=kw.get("plan_day", 0),
        plan_hour=kw.get("plan_hour", 6),
        plan_cadence=kw.get("plan_cadence", "weekly"),
        last_planned_at=kw.get("last_planned_at"),
    )
    db.add(s)
    db.flush()
    return s


def _settings_row(db_url, business_id) -> AutopilotSettings | None:
    session = _fresh_session(db_url)
    try:
        return session.get(AutopilotSettings, business_id)
    finally:
        session.close()


class TestPerTenantSchedule:
    def test_custom_day_and_hour_are_honored(self, db, db_url, monkeypatch):
        """plan_day=Wednesday, plan_hour=14 (UTC): the default Monday
        06:00 moment must NOT draft; Wednesday 14:00 must."""
        business = _business(db)
        _settings(db, business, plan_day=2, plan_hour=14)
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["drafted"] == 0
        assert result["skipped_not_due"] == 1
        assert _plan(db_url, business.id) is None

        # 2026-10-14 is a Wednesday.
        result = _run_job(
            db_url, datetime(2026, 10, 14, 14, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 1
        plan = _plan(db_url, business.id)
        assert plan is not None
        # week_start stays the Monday of the draft week, whatever the day.
        assert plan.week_start == MONDAY
        # The draft stamps last_planned_at for the biweekly gate.
        assert _settings_row(db_url, business.id).last_planned_at is not None

    def test_custom_hour_wrong_hour_skips(self, db, db_url, monkeypatch):
        business = _business(db)
        _settings(db, business, plan_day=0, plan_hour=9)
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["drafted"] == 0
        assert result["skipped_not_due"] == 1

    def test_biweekly_skips_within_gap(self, db, db_url, monkeypatch):
        """A draft 7 days ago blocks this week's draft on biweekly."""
        business = _business(db)
        _settings(
            db,
            business,
            plan_cadence="biweekly",
            last_planned_at=datetime(2026, 10, 12, 6, 5, tzinfo=UTC),
        )
        db.commit()

        result = _run_job(
            db_url, datetime(2026, 10, 19, 6, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 0
        assert result["skipped_cadence"] == 1
        assert _plan(db_url, business.id) is None

    def test_biweekly_drafts_after_gap(self, db, db_url, monkeypatch):
        """A draft 14 days ago allows this week's draft on biweekly."""
        business = _business(db)
        _settings(
            db,
            business,
            plan_cadence="biweekly",
            last_planned_at=datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
        )
        db.commit()

        result = _run_job(
            db_url, datetime(2026, 10, 19, 6, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 1
        plan = _plan(db_url, business.id)
        assert plan is not None
        assert plan.week_start == date(2026, 10, 19)

    def test_weekly_ignores_last_planned_at(self, db, db_url, monkeypatch):
        """The weekly cadence drafts every plan moment regardless of the
        last draft stamp."""
        business = _business(db)
        _settings(
            db,
            business,
            plan_cadence="weekly",
            last_planned_at=datetime(2026, 10, 12, 6, 5, tzinfo=UTC),
        )
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["drafted"] == 1

    def test_biweekly_first_draft_has_no_stamp(self, db, db_url, monkeypatch):
        """A biweekly business that never drafted is due at its first
        plan moment."""
        business = _business(db)
        _settings(db, business, plan_cadence="biweekly")
        db.commit()

        result = _run_job(db_url, _monday_6am_utc(), monkeypatch)
        assert result["drafted"] == 1


# ---------------------------------------------------------------------------
# autopilot_plan_now (manual trigger)
# ---------------------------------------------------------------------------


def _run_now(db_url, business_id, when: datetime, monkeypatch, created_by=None):
    monkeypatch.setattr(jobs, "utcnow", lambda: when)
    return asyncio.run(autopilot_plan_now(FAKE_CTX, str(business_id), created_by))


class TestRunNow:
    def test_run_now_drafts_outside_schedule(self, db, db_url, monkeypatch):
        """Friday 2026-10-16 15:30 UTC is no plan moment — the manual
        trigger drafts anyway."""
        business = _business(db)
        db.commit()

        result = _run_now(
            db_url, business.id, datetime(2026, 10, 16, 15, 30, tzinfo=UTC),
            monkeypatch,
        )
        assert result["ok"] is True
        assert result["created"] is True

        plan = _plan(db_url, business.id)
        assert plan is not None
        assert plan.week_start == MONDAY  # Monday of the draft week
        assert plan.status == PlanStatus.draft
        assert plan.created_by is None
        assert len(plan.items) == 3

    def test_run_now_records_creator_and_stamps_settings(
        self, db, db_url, monkeypatch
    ):
        business = _business(db)
        _settings(db, business, plan_cadence="biweekly")
        db.commit()
        creator = uuid.uuid4()

        result = _run_now(
            db_url,
            business.id,
            datetime(2026, 10, 16, 15, 30, tzinfo=UTC),
            monkeypatch,
            created_by=str(creator),
        )
        assert result["created"] is True

        session = _fresh_session(db_url)
        try:
            plan = (
                session.query(ContentPlan)
                .filter(ContentPlan.business_id == business.id)
                .one()
            )
            assert plan.created_by == creator
            settings = session.get(AutopilotSettings, business.id)
            assert settings.last_planned_at is not None
        finally:
            session.close()

    def test_run_now_is_idempotent_same_week(self, db, db_url, monkeypatch):
        business = _business(db)
        db.commit()
        when = datetime(2026, 10, 16, 15, 30, tzinfo=UTC)

        first = _run_now(db_url, business.id, when, monkeypatch)
        assert first["created"] is True
        second = _run_now(db_url, business.id, when, monkeypatch)
        assert second["created"] is False
        assert second["plan_id"] == first["plan_id"]

        session = _fresh_session(db_url)
        try:
            count = (
                session.query(ContentPlan)
                .filter(ContentPlan.business_id == business.id)
                .count()
            )
        finally:
            session.close()
        assert count == 1

    def test_run_now_unknown_business(self, db_url, monkeypatch):
        result = _run_now(
            db_url,
            uuid.uuid4(),
            datetime(2026, 10, 16, 15, 30, tzinfo=UTC),
            monkeypatch,
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_run_now_counts_toward_biweekly_gap(self, db, db_url, monkeypatch):
        """A manual draft stamps last_planned_at, so the next cron plan
        moment inside the gap is skipped on the biweekly cadence."""
        business = _business(db)
        _settings(db, business, plan_cadence="biweekly")
        db.commit()

        _run_now(
            db_url, business.id, datetime(2026, 10, 16, 15, 30, tzinfo=UTC),
            monkeypatch,
        )
        result = _run_job(
            db_url, datetime(2026, 10, 19, 6, 0, tzinfo=UTC), monkeypatch
        )
        assert result["drafted"] == 0
        assert result["skipped_cadence"] == 1
