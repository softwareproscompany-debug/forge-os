"""Unit + integration tests for the ForgeOS worker (apps/worker).

* Pure-function tests for quiet-hours windowing and delay scheduling.
* Job tests run the real arq job functions directly with a fake ``ctx``
  dict against a fresh SQLite database per test (``forge_db`` is
  sqlite-portable by design).
* No network anywhere: ``LLM_PROVIDER=stub`` and ``CHANNEL_MODE=stub``,
  so the stub LLM provider and stub channel providers (dev outbox) are
  used throughout.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from arq import Retry
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("CHANNEL_MODE", "stub")

from forge_db.models import (  # noqa: E402
    Asset,
    AssetKind,
    AssetStatus,
    AutopilotSettings,
    Base,
    BrandKit,
    Business,
    Campaign,
    CampaignEnrollment,
    CampaignStep,
    CampaignStatus,
    Channel,
    Contact,
    DevOutbox,
    EnrollmentStatus,
    Event,
    EventKind,
    GenerationLog,
    Send,
    SendStatus,
    Template,
)

from worker import jobs  # noqa: E402
from worker.jobs import (  # noqa: E402
    _tick_campaign,
    campaign_tick,
    generate_asset,
    handle_event,
    send_message,
)
from worker.settings import WorkerSettings  # noqa: E402
from worker.timeutil import (  # noqa: E402
    add_delay_hours,
    in_quiet_hours,
    next_send_time,
)

UTC = timezone.utc
FAKE_CTX = {"job_try": 1}


# ---------------------------------------------------------------------------
# DB fixture: fresh SQLite file per test, wired via DATABASE_URL
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    path = tmp_path / f"worker_test_{uuid.uuid4().hex}.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield url
    engine.dispose()


@pytest.fixture()
def db(db_url):
    # expire_on_commit=False keeps PKs/attributes readable after commit,
    # so tests can hand ids to jobs after closing this session.
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


def _business(db: Session, **kw) -> Business:
    b = Business(
        name=kw.get("name", "Acme Inc"),
        slug=kw.get("slug", f"acme-{uuid.uuid4().hex[:8]}"),
        timezone=kw.get("timezone", "UTC"),
    )
    db.add(b)
    db.flush()
    return b


def _autopilot(db: Session, business: Business, **kw) -> AutopilotSettings:
    s = AutopilotSettings(
        business_id=business.id,
        auto_approve=kw.get("auto_approve", False),
        daily_send_cap=kw.get("daily_send_cap", 500),
        quiet_hours_start=kw.get("quiet_hours_start", 22),
        quiet_hours_end=kw.get("quiet_hours_end", 8),
    )
    db.add(s)
    db.flush()
    return s


def _contact(db: Session, business: Business, **kw) -> Contact:
    c = Contact(
        business_id=business.id,
        email=kw.get("email", "ada@example.com"),
        phone=kw.get("phone", "+15551234567"),
        first_name=kw.get("first_name", "Ada"),
        last_name=kw.get("last_name", "Lovelace"),
        consent_email=kw.get("consent_email", True),
        consent_sms=kw.get("consent_sms", True),
        unsubscribed=kw.get("unsubscribed", False),
        custom_fields=kw.get("custom_fields", {}),
    )
    db.add(c)
    db.flush()
    return c


def _campaign(db: Session, business: Business, **kw) -> Campaign:
    c = Campaign(
        business_id=business.id,
        name=kw.get("name", "Drip"),
        status=kw.get("status", CampaignStatus.running),
        starts_at=kw.get("starts_at"),
        timezone=kw.get("timezone", "UTC"),
    )
    db.add(c)
    db.flush()
    return c


def _step(
    db: Session,
    campaign: Campaign,
    position: int = 0,
    channel: Channel = Channel.email,
    template: Template | None = None,
    delay_hours: int = 24,
    trigger_event: str | None = None,
) -> CampaignStep:
    s = CampaignStep(
        campaign_id=campaign.id,
        position=position,
        channel=channel,
        template_id=template.id if template else None,
        delay_hours=delay_hours,
        trigger_event=trigger_event,
    )
    db.add(s)
    db.flush()
    return s


def _template(db: Session, business: Business, **kw) -> Template:
    t = Template(
        business_id=business.id,
        name=kw.get("name", "Welcome"),
        channel=kw.get("channel", Channel.email),
        subject_template=kw.get("subject_template", "Hello {{ first_name }}"),
        body_template=kw.get("body_template", "Hi {{ first_name }}, welcome aboard!"),
    )
    db.add(t)
    db.flush()
    return t


def _fresh_session(db_url) -> Session:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    return Session(engine)


# ---------------------------------------------------------------------------
# timeutil: quiet hours
# ---------------------------------------------------------------------------


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 9, hour, minute, tzinfo=UTC)


class TestQuietHours:
    def test_overnight_window(self):
        # 22 -> 8: evening and early morning are quiet, midday is not.
        assert in_quiet_hours(_dt(23, 30), 22, 8) is True
        assert in_quiet_hours(_dt(0, 0), 22, 8) is True
        assert in_quiet_hours(_dt(7, 59), 22, 8) is True
        assert in_quiet_hours(_dt(8, 0), 22, 8) is False
        assert in_quiet_hours(_dt(12, 0), 22, 8) is False
        assert in_quiet_hours(_dt(21, 59), 22, 8) is False
        assert in_quiet_hours(_dt(22, 0), 22, 8) is True

    def test_daytime_window(self):
        assert in_quiet_hours(_dt(9, 0), 9, 17) is True
        assert in_quiet_hours(_dt(12, 0), 9, 17) is True
        assert in_quiet_hours(_dt(16, 59), 9, 17) is True
        assert in_quiet_hours(_dt(17, 0), 9, 17) is False
        assert in_quiet_hours(_dt(8, 59), 9, 17) is False
        assert in_quiet_hours(_dt(23, 0), 9, 17) is False

    def test_equal_start_end_means_no_quiet_hours(self):
        for h in (0, 8, 12, 22):
            assert in_quiet_hours(_dt(h), 8, 8) is False


class TestNextSendTime:
    def test_overnight_evening_pushes_to_next_morning(self):
        got = next_send_time(_dt(23, 30), 22, 8)
        assert got == datetime(2026, 10, 10, 8, 0, tzinfo=UTC)

    def test_overnight_morning_pushes_to_same_morning(self):
        got = next_send_time(_dt(7, 30), 22, 8)
        assert got == datetime(2026, 10, 9, 8, 0, tzinfo=UTC)

    def test_outside_quiet_hours_returns_now(self):
        now = _dt(12, 0)
        assert next_send_time(now, 22, 8) == now

    def test_daytime_window(self):
        got = next_send_time(_dt(10, 0), 9, 17)
        assert got == datetime(2026, 10, 9, 17, 0, tzinfo=UTC)

    def test_boundary_is_strictly_after_now(self):
        now = datetime(2026, 10, 9, 7, 59, 59, tzinfo=UTC)
        assert next_send_time(now, 22, 8) > now


class TestAddDelayHours:
    def test_adds_hours(self):
        now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
        assert add_delay_hours(now, 24) == now + timedelta(hours=24)
        assert add_delay_hours(now, 0) == now


# ---------------------------------------------------------------------------
# generate_asset
# ---------------------------------------------------------------------------


class TestGenerateAsset:
    def _asset(self, db, business, **kw):
        a = Asset(
            business_id=business.id,
            kind=kw.get("kind", AssetKind.email_copy),
            title=kw.get("title", "October promo"),
            variables=kw.get("variables", {"offer_details": "50% off everything"}),
            status=AssetStatus.draft,
        )
        db.add(a)
        db.commit()
        return a

    async def test_auto_approve_false_leaves_in_review(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, auto_approve=False)
        db.add(BrandKit(business_id=b.id, name="Default", voice_description="Plainspoken"))
        asset = self._asset(db, b)
        db.close()

        result = await generate_asset(FAKE_CTX, str(asset.id))

        assert result["ok"] is True
        assert result["status"] == "in_review"
        assert result["provider"] == "stub"
        fresh = _fresh_session(db_url)
        try:
            row = fresh.get(Asset, asset.id)
            assert row.status == AssetStatus.in_review
            assert row.body  # copy was saved
            assert row.tokens_in > 0 and row.tokens_out > 0
            assert row.llm_provider == "stub"
            assert row.brand_kit_version == 1
            logs = fresh.query(GenerationLog).filter(GenerationLog.asset_id == asset.id).all()
            assert len(logs) == 1
            assert logs[0].prompt_hash
        finally:
            fresh.close()

    async def test_auto_approve_true_approves(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, auto_approve=True)
        asset = self._asset(db, b, kind=AssetKind.sms)
        db.close()

        result = await generate_asset(FAKE_CTX, str(asset.id))

        assert result["ok"] is True
        assert result["status"] == "approved"
        fresh = _fresh_session(db_url)
        try:
            assert fresh.get(Asset, asset.id).status == AssetStatus.approved
        finally:
            fresh.close()

    async def test_idempotent_second_run_skips(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, auto_approve=False)
        asset = self._asset(db, b)
        db.close()

        first = await generate_asset(FAKE_CTX, str(asset.id))
        second = await generate_asset(FAKE_CTX, str(asset.id))

        assert first["ok"] is True and not first.get("skipped")
        assert second["ok"] is True and second.get("skipped") is True
        fresh = _fresh_session(db_url)
        try:
            logs = fresh.query(GenerationLog).filter(GenerationLog.asset_id == asset.id).all()
            assert len(logs) == 1  # no duplicate generation log
        finally:
            fresh.close()

    async def test_missing_asset_returns_error(self, db):
        result = await generate_asset(FAKE_CTX, str(uuid.uuid4()))
        assert result["ok"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    def _queued_send(self, db, business, contact, step, **kw):
        s = Send(
            business_id=business.id,
            campaign_id=step.campaign_id,
            step_id=step.id,
            contact_id=contact.id,
            channel=step.channel,
            to_address=kw.get("to_address", contact.email or ""),
            subject="",
            body="",
            status=SendStatus.queued,
        )
        db.add(s)
        db.commit()
        return s

    async def test_happy_path_renders_and_records_outbox(self, db, db_url):
        b = _business(db)
        _autopilot(db, b)
        contact = _contact(db, b, first_name="Ada")
        tmpl = _template(db, b)
        camp = _campaign(db, b)
        step = _step(db, camp, template=tmpl)
        send = self._queued_send(db, b, contact, step)
        db.close()

        result = await send_message(FAKE_CTX, str(send.id))

        assert result["ok"] is True
        assert result["provider_message_id"].startswith("stub-")
        fresh = _fresh_session(db_url)
        try:
            row = fresh.get(Send, send.id)
            assert row.status == SendStatus.sent
            assert row.sent_at is not None
            assert row.body == "Hi Ada, welcome aboard!"
            assert row.subject == "Hello Ada"
            outbox = fresh.query(DevOutbox).all()
            assert len(outbox) == 1
            assert outbox[0].to_address == "ada@example.com"
            assert outbox[0].channel == "email"
        finally:
            fresh.close()

    async def test_email_without_consent_fails(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b, consent_email=False)
        tmpl = _template(db, b)
        camp = _campaign(db, b)
        step = _step(db, camp, template=tmpl)
        send = self._queued_send(db, b, contact, step)
        db.close()

        result = await send_message(FAKE_CTX, str(send.id))

        assert result["ok"] is False
        assert "consent" in result["error"]
        fresh = _fresh_session(db_url)
        try:
            row = fresh.get(Send, send.id)
            assert row.status == SendStatus.failed
            assert "consent" in (row.error or "")
            assert fresh.query(DevOutbox).count() == 0
        finally:
            fresh.close()

    async def test_sms_without_sms_consent_fails_but_email_consent_irrelevant(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b, consent_email=True, consent_sms=False)
        tmpl = _template(db, b, channel=Channel.sms, subject_template=None,
                         body_template="Hi {{ first_name }}!")
        camp = _campaign(db, b)
        step = _step(db, camp, channel=Channel.sms, template=tmpl)
        send = self._queued_send(db, b, contact, step, to_address=contact.phone or "")
        db.close()

        result = await send_message(FAKE_CTX, str(send.id))
        assert result["ok"] is False
        assert "consent" in result["error"]

    async def test_unsubscribed_blocks_send(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b, unsubscribed=True)
        tmpl = _template(db, b)
        camp = _campaign(db, b)
        step = _step(db, camp, template=tmpl)
        send = self._queued_send(db, b, contact, step)
        db.close()

        result = await send_message(FAKE_CTX, str(send.id))
        assert result["ok"] is False
        assert result["error"] == "unsubscribed"
        fresh = _fresh_session(db_url)
        try:
            assert fresh.get(Send, send.id).status == SendStatus.failed
        finally:
            fresh.close()

    async def test_missing_template_variable_fails_clearly(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b)
        tmpl = _template(db, b, body_template="Hi {{ favorite_color }}!")
        camp = _campaign(db, b)
        step = _step(db, camp, template=tmpl)
        send = self._queued_send(db, b, contact, step)
        db.close()

        result = await send_message(FAKE_CTX, str(send.id))

        assert result["ok"] is False
        assert "favorite_color" in result["error"]
        fresh = _fresh_session(db_url)
        try:
            row = fresh.get(Send, send.id)
            assert row.status == SendStatus.failed
            assert "favorite_color" in (row.error or "")
        finally:
            fresh.close()

    async def test_unapproved_asset_refused(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b)
        camp = _campaign(db, b)
        asset = Asset(business_id=b.id, kind=AssetKind.email_copy, title="Draft",
                      body="Hi {{ first_name }}", status=AssetStatus.draft)
        db.add(asset)
        db.flush()
        step = CampaignStep(campaign_id=camp.id, position=0, channel=Channel.email,
                            asset_id=asset.id, delay_hours=0)
        db.add(step)
        db.flush()
        send = Send(business_id=b.id, campaign_id=camp.id, step_id=step.id,
                    contact_id=contact.id, channel=Channel.email,
                    asset_id=asset.id, to_address=contact.email or "",
                    subject="", body="", status=SendStatus.queued)
        db.add(send)
        db.commit()
        db.close()

        result = await send_message(FAKE_CTX, str(send.id))
        assert result["ok"] is False
        assert "not approved" in result["error"]

    async def test_transient_provider_error_raises_retry(self, db, db_url, monkeypatch):
        b = _business(db)
        contact = _contact(db, b)
        tmpl = _template(db, b)
        camp = _campaign(db, b)
        step = _step(db, camp, template=tmpl)
        send = self._queued_send(db, b, contact, step)
        db.close()

        class FlakyProvider:
            name = "flaky"

            async def send(self, req):
                raise httpx.TimeoutException("upstream timed out")

            async def parse_webhook(self, payload):
                return {}

        monkeypatch.setattr(jobs, "get_email_provider", lambda: FlakyProvider())

        with pytest.raises(Retry):
            await send_message({"job_try": 1}, str(send.id))

        fresh = _fresh_session(db_url)
        try:
            row = fresh.get(Send, send.id)
            assert row.status == SendStatus.failed  # recorded before the retry
            assert "TimeoutException" in (row.error or "")
        finally:
            fresh.close()

    async def test_transient_error_on_last_try_is_final(self, db, db_url, monkeypatch):
        b = _business(db)
        contact = _contact(db, b)
        tmpl = _template(db, b)
        camp = _campaign(db, b)
        step = _step(db, camp, template=tmpl)
        send = self._queued_send(db, b, contact, step)
        db.close()

        class FlakyProvider:
            name = "flaky"

            async def send(self, req):
                raise httpx.ConnectError("connection refused")

            async def parse_webhook(self, payload):
                return {}

        monkeypatch.setattr(jobs, "get_email_provider", lambda: FlakyProvider())

        result = await send_message({"job_try": 5}, str(send.id))
        assert result["ok"] is False  # no Retry raised on the final try


# ---------------------------------------------------------------------------
# campaign_tick
# ---------------------------------------------------------------------------


class TestCampaignTick:
    async def test_bulk_enroll_and_advance(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, quiet_hours_start=0, quiet_hours_end=0)  # no quiet window
        c1 = _contact(db, b, email="a@example.com")
        c2 = _contact(db, b, email="b@example.com")
        _contact(db, b, email="unsub@example.com", unsubscribed=True)
        camp = _campaign(db, b, starts_at=datetime.now(UTC) - timedelta(hours=1))
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl, delay_hours=0)
        _step(db, camp, position=1, template=tmpl, delay_hours=48)
        db.commit()
        db.close()

        result = await campaign_tick({"redis": None})

        assert result["ok"] is True
        assert result["enrolled"] == 2  # unsubscribed contact excluded
        assert result["sent"] == 2
        fresh = _fresh_session(db_url)
        try:
            enrollments = fresh.query(CampaignEnrollment).all()
            assert len(enrollments) == 2
            for e in enrollments:
                assert e.current_step == 1  # advanced past step 0
                assert e.status == EnrollmentStatus.active
                # next_run_at ~= now + 48h (step 1's delay_hours)
                aware_next = e.next_run_at.replace(tzinfo=UTC)  # sqlite round-trips naive
                delta = (aware_next - datetime.now(UTC)).total_seconds()
                assert 47 * 3600 < delta < 49 * 3600
            sends = fresh.query(Send).all()
            assert len(sends) == 2
            assert all(s.status == SendStatus.queued for s in sends)
        finally:
            fresh.close()

    async def test_trigger_step_skips_bulk_enroll(self, db, db_url):
        b = _business(db)
        _autopilot(db, b)
        _contact(db, b)
        camp = _campaign(db, b, starts_at=datetime.now(UTC) - timedelta(hours=1))
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl, trigger_event="contact_added")
        db.commit()
        db.close()

        result = await campaign_tick({"redis": None})
        assert result["enrolled"] == 0
        fresh = _fresh_session(db_url)
        try:
            assert fresh.query(CampaignEnrollment).count() == 0
        finally:
            fresh.close()

    async def test_daily_cap_blocks_sends(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, daily_send_cap=1, quiet_hours_start=0, quiet_hours_end=0)
        contact = _contact(db, b)
        camp = _campaign(db, b, starts_at=datetime.now(UTC) - timedelta(hours=1))
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl)
        # One send already went out today -> cap reached.
        db.add(Send(business_id=b.id, contact_id=contact.id, channel=Channel.email,
                    to_address=contact.email or "", subject="s", body="b",
                    status=SendStatus.sent, sent_at=datetime.now(UTC)))
        db.commit()
        db.close()

        result = await campaign_tick({"redis": None})

        assert result["ok"] is True
        assert result["enrolled"] == 1  # enrollment still happens
        assert result["sent"] == 0
        assert result["skipped_cap"] == 1
        fresh = _fresh_session(db_url)
        try:
            assert fresh.query(Send).count() == 1  # no new send rows
            enr = fresh.query(CampaignEnrollment).one()
            assert enr.current_step == 0  # not advanced
        finally:
            fresh.close()

    async def test_quiet_hours_push_next_run_at(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, quiet_hours_start=22, quiet_hours_end=8)
        contact = _contact(db, b)
        camp = _campaign(db, b, starts_at=datetime.now(UTC) - timedelta(hours=2),
                         timezone="UTC")
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl)
        enr = CampaignEnrollment(
            campaign_id=camp.id, contact_id=contact.id, current_step=0,
            status=EnrollmentStatus.active,
            next_run_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(enr)
        db.commit()
        enr_id = enr.id
        db.close()

        # 23:30 UTC is inside the 22->8 quiet window.
        now = datetime(2026, 10, 9, 23, 30, tzinfo=UTC)
        fresh = _fresh_session(db_url)
        try:
            campaign = fresh.get(Campaign, camp.id)
            summary = {"campaigns": 0, "enrolled": 0, "sent": 0,
                       "skipped_quiet": 0, "skipped_cap": 0, "enqueue_errors": 0}
            await _tick_campaign(fresh, campaign, now, None, summary)
            fresh.commit()
            assert summary["skipped_quiet"] == 1
            assert summary["sent"] == 0
            updated = fresh.get(CampaignEnrollment, enr_id)
            # sqlite round-trips datetimes naive; the job pushed to 08:00 UTC.
            assert updated.next_run_at == datetime(2026, 10, 10, 8, 0)
            assert fresh.query(Send).count() == 0
        finally:
            fresh.close()

    async def test_enrollment_completes_after_last_step(self, db, db_url):
        b = _business(db)
        _autopilot(db, b, quiet_hours_start=0, quiet_hours_end=0)
        contact = _contact(db, b)
        camp = _campaign(db, b, starts_at=datetime.now(UTC) - timedelta(hours=1))
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl)  # single step
        db.add(CampaignEnrollment(
            campaign_id=camp.id, contact_id=contact.id, current_step=0,
            status=EnrollmentStatus.active,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
        db.commit()
        db.close()

        await campaign_tick({"redis": None})

        fresh = _fresh_session(db_url)
        try:
            enr = fresh.query(CampaignEnrollment).one()
            assert enr.status == EnrollmentStatus.completed
            assert enr.next_run_at is None
            assert fresh.query(Send).count() == 1
        finally:
            fresh.close()


# ---------------------------------------------------------------------------
# handle_event
# ---------------------------------------------------------------------------


class TestHandleEvent:
    async def test_contact_added_enrolls_into_trigger_campaigns(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b)
        camp = _campaign(db, b)
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl, trigger_event="contact_added")
        other = _campaign(db, b, name="No trigger")
        _step(db, other, position=0, template=tmpl)
        event = Event(business_id=b.id, contact_id=contact.id,
                      kind=EventKind.contact_added.value, payload={})
        db.add(event)
        db.commit()
        db.close()

        result = await handle_event({}, str(event.id))

        assert result["ok"] is True
        assert result["enrolled"] == 1
        fresh = _fresh_session(db_url)
        try:
            enrollments = fresh.query(CampaignEnrollment).all()
            assert len(enrollments) == 1
            assert enrollments[0].campaign_id == camp.id
            assert enrollments[0].next_run_at is not None
        finally:
            fresh.close()

    async def test_contact_added_is_idempotent(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b)
        camp = _campaign(db, b)
        tmpl = _template(db, b)
        _step(db, camp, position=0, template=tmpl, trigger_event="contact_added")
        for _ in range(2):
            db.add(Event(business_id=b.id, contact_id=contact.id,
                         kind=EventKind.contact_added.value, payload={}))
        db.commit()
        events = db.query(Event).all()
        db.close()

        for event in events:
            await handle_event({}, str(event.id))

        fresh = _fresh_session(db_url)
        try:
            assert fresh.query(CampaignEnrollment).count() == 1
        finally:
            fresh.close()

    async def test_email_opened_stamps_send(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b)
        send = Send(business_id=b.id, contact_id=contact.id, channel=Channel.email,
                    to_address=contact.email or "", subject="s", body="b",
                    status=SendStatus.sent, provider_message_id="pm-123")
        db.add(send)
        db.flush()
        event = Event(business_id=b.id, contact_id=contact.id,
                      kind=EventKind.email_opened.value,
                      payload={"provider_message_id": "pm-123"})
        db.add(event)
        db.commit()
        db.close()

        result = await handle_event({}, str(event.id))
        assert result["ok"] is True

        fresh = _fresh_session(db_url)
        try:
            assert fresh.get(Send, send.id).opened_at is not None
        finally:
            fresh.close()

    async def test_converted_stamps_send(self, db, db_url):
        b = _business(db)
        contact = _contact(db, b)
        send = Send(business_id=b.id, contact_id=contact.id, channel=Channel.email,
                    to_address=contact.email or "", subject="s", body="b",
                    status=SendStatus.sent, provider_message_id="pm-456")
        db.add(send)
        db.flush()
        event = Event(business_id=b.id, contact_id=contact.id, kind="converted",
                      payload={"provider_message_id": "pm-456"})
        db.add(event)
        db.commit()
        db.close()

        await handle_event({}, str(event.id))

        fresh = _fresh_session(db_url)
        try:
            assert fresh.get(Send, send.id).converted_at is not None
        finally:
            fresh.close()

    async def test_unknown_kind_ignored(self, db):
        b = _business(db)
        event = Event(business_id=b.id, kind="something_new", payload={})
        db.add(event)
        db.commit()
        db.close()

        result = await handle_event({}, str(event.id))
        assert result["ok"] is True
        assert result["ignored"] is True


# ---------------------------------------------------------------------------
# WorkerSettings wiring
# ---------------------------------------------------------------------------


class TestWorkerSettings:
    def test_settings_shape(self):
        assert {f.__name__ for f in WorkerSettings.functions} == {
            "generate_asset",
            "send_message",
            "campaign_tick",
            "handle_event",
        }
        assert len(WorkerSettings.cron_jobs) == 1
        cron_job = WorkerSettings.cron_jobs[0]
        assert cron_job.coroutine is campaign_tick
        # every minute of the hour
        assert cron_job.minute == set(range(60))
        assert WorkerSettings.queue_name == "forge"
        assert WorkerSettings.redis_settings.host  # parsed from REDIS_URL
