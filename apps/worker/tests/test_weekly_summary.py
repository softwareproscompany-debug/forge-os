"""Tests for the Card 5 weekly evidence summary job (apps/worker).

Covers, against a fresh SQLite database per test (``forge_db`` is
sqlite-portable by design):

* computation: top/bottom asset ordering by conversion rate (deterministic
  tiebreaks), best channel per segment (incl. multi-tag contacts and the
  ``untagged`` bucket)
* the deterministic stub recommendation paragraph
* idempotent skip on re-run (no duplicate row for the same week)
* timezone gating: pure ``_is_summary_moment`` / ``_summary_week_start`` /
  ``_business_tz`` checks, incl. non-UTC businesses and bad tz names
* the full ``weekly_summary`` job: per-business loop, bad-business
  isolation, DB-unavailable path
* Origination wiring: ``generate_asset`` still succeeds with a summary
  present; ``_with_weekly_evidence`` appends the evidence paragraph

No network anywhere: ``LLM_PROVIDER=stub``.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
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
    Business,
    Channel,
    Contact,
    Send,
    SendStatus,
    WeeklySummary,
)

from worker import jobs  # noqa: E402
from worker.jobs import (  # noqa: E402
    _business_tz,
    _is_summary_moment,
    _stub_recommendation,
    _summarize_business_if_due,
    _summary_week_start,
    _with_weekly_evidence,
    generate_asset,
    weekly_summary,
)

UTC = timezone.utc
FAKE_CTX = {"job_try": 1}

#: A Sunday 23:30 UTC — inside the summary window for tz="UTC" businesses.
SUNDAY_2330 = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)

#: A send timestamp inside the [now-7d, now) window (naive, like SQLite).
IN_WINDOW = datetime(2026, 10, 8, 12, 0, 0)


# ---------------------------------------------------------------------------
# DB fixture: fresh SQLite file per test, wired via DATABASE_URL
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    path = tmp_path / f"summary_test_{uuid.uuid4().hex}.db"
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


def _business(db: Session, **kw) -> Business:
    b = Business(
        name=kw.get("name", "Acme Inc"),
        slug=kw.get("slug", f"acme-{uuid.uuid4().hex[:8]}"),
        timezone=kw.get("timezone", "UTC"),
    )
    db.add(b)
    db.flush()
    return b


def _asset(db: Session, business: Business, title: str, kind: AssetKind) -> Asset:
    a = Asset(
        business_id=business.id,
        kind=kind,
        title=title,
        variables={},
        status=AssetStatus.approved,
        body="body",
    )
    db.add(a)
    db.flush()
    return a


def _contact(db: Session, business: Business, tags: list[str] | None = None) -> Contact:
    c = Contact(
        business_id=business.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        tags=list(tags) if tags else [],
    )
    db.add(c)
    db.flush()
    return c


def _send(
    db: Session,
    business: Business,
    contact: Contact,
    asset: Asset | None = None,
    channel: Channel = Channel.email,
    status: SendStatus = SendStatus.delivered,
    converted: bool = False,
) -> Send:
    s = Send(
        business_id=business.id,
        contact_id=contact.id,
        asset_id=asset.id if asset else None,
        channel=channel,
        to_address=contact.email or "x@example.com",
        subject="",
        body="hello",
        status=status,
        sent_at=IN_WINDOW,
        converted_at=IN_WINDOW if converted else None,
        created_at=IN_WINDOW,
    )
    db.add(s)
    db.flush()
    return s


def _engagement_fixture(db: Session, business: Business) -> dict:
    """Assets+sends with known engagement; returns the created assets."""
    a = _asset(db, business, "Launch email", AssetKind.email_copy)
    b = _asset(db, business, "Promo SMS", AssetKind.sms)
    c = _asset(db, business, "Teaser social", AssetKind.social_post)
    d = _asset(db, business, "Blog recap", AssetKind.blog)
    e = _asset(db, business, "Old ad", AssetKind.ad)
    f = _asset(db, business, "Draft never sent", AssetKind.email_copy)
    g = _asset(db, business, "Queued only", AssetKind.sms)

    contact = _contact(db, business)
    plan = [(a, 10, 9), (b, 10, 5), (c, 10, 5), (d, 10, 1), (e, 10, 0)]
    for asset, delivered, converted in plan:
        for i in range(delivered):
            _send(db, business, contact, asset, converted=(i < converted))
    # f: no sends at all -> excluded. g: sends but none delivered -> excluded.
    for _ in range(3):
        _send(db, business, contact, g, status=SendStatus.queued)
    db.commit()
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f, "g": g}


# ---------------------------------------------------------------------------
# timezone gating (pure helpers)
# ---------------------------------------------------------------------------


class TestGating:
    def test_due_sunday_2300_utc(self):
        local = SUNDAY_2330.astimezone(_business_tz("UTC"))
        assert _is_summary_moment(local) is True
        assert _summary_week_start(local) == date(2026, 10, 5)  # last Monday

    def test_not_due_other_hours(self):
        assert _is_summary_moment(datetime(2026, 10, 11, 22, 30, tzinfo=UTC)) is False
        assert _is_summary_moment(datetime(2026, 10, 11, 0, 5, tzinfo=UTC)) is False

    def test_not_due_other_weekdays(self):
        assert _is_summary_moment(datetime(2026, 10, 12, 23, 30, tzinfo=UTC)) is False  # Monday
        assert _is_summary_moment(datetime(2026, 10, 10, 23, 30, tzinfo=UTC)) is False  # Saturday

    def test_non_utc_business_timezone(self):
        # Monday 2026-10-12 04:30 UTC == Sunday 23:30 America/Chicago (CDT).
        now = datetime(2026, 10, 12, 4, 30, tzinfo=UTC)
        local = now.astimezone(_business_tz("America/Chicago"))
        assert local.weekday() == 6 and local.hour == 23
        assert _is_summary_moment(local) is True
        assert _summary_week_start(local) == date(2026, 10, 5)

    def test_bad_timezone_falls_back_to_utc(self):
        tz = _business_tz("Not/AZone")
        assert str(tz) == "UTC"
        local = SUNDAY_2330.astimezone(tz)
        assert _is_summary_moment(local) is True


# ---------------------------------------------------------------------------
# computation
# ---------------------------------------------------------------------------


class TestComputation:
    async def test_top_bottom_ordering_and_shape(self, db):
        b = _business(db)
        assets = _engagement_fixture(db, b)

        created = await _summarize_business_if_due(db, b, SUNDAY_2330)
        assert created is True
        db.commit()

        row = db.query(WeeklySummary).one()
        assert row.business_id == b.id
        assert row.week_start == date(2026, 10, 5)

        top, bottom = row.top_assets, row.bottom_assets
        for entry in top + bottom:
            assert set(entry) == {
                "asset_id",
                "title",
                "kind",
                "delivered",
                "converted",
                "conversion_rate",
            }
            assert isinstance(entry["asset_id"], str)

        # Top: A (0.9), then the B/C tie at 0.5 ordered by asset id.
        assert top[0]["asset_id"] == str(assets["a"].id)
        assert top[0]["conversion_rate"] == 0.9
        assert top[0]["delivered"] == 10 and top[0]["converted"] == 9
        assert top[0]["kind"] == "email_copy"
        assert {top[1]["asset_id"], top[2]["asset_id"]} == {
            str(assets["b"].id),
            str(assets["c"].id),
        }
        assert top[1]["conversion_rate"] == top[2]["conversion_rate"] == 0.5
        assert top[1]["asset_id"] < top[2]["asset_id"]  # tiebreak: asset id

        # Bottom: E (0.0), D (0.1), then the B/C tie loser by asset id.
        assert bottom[0]["asset_id"] == str(assets["e"].id)
        assert bottom[0]["conversion_rate"] == 0.0
        assert bottom[1]["asset_id"] == str(assets["d"].id)
        assert bottom[1]["conversion_rate"] == 0.1
        assert bottom[2]["conversion_rate"] == 0.5
        assert bottom[2]["asset_id"] == min(str(assets["b"].id), str(assets["c"].id))

        # Excluded: no sends (f), no delivered sends (g).
        ids = {e["asset_id"] for e in top + bottom}
        assert str(assets["f"].id) not in ids
        assert str(assets["g"].id) not in ids

    async def test_segments_best_channel(self, db):
        b = _business(db)
        c1 = _contact(db, b, ["vip"])
        c2 = _contact(db, b, ["vip"])
        c3 = _contact(db, b, ["new"])
        c4 = _contact(db, b)  # no tags -> untagged
        c5 = _contact(db, b, ["vip", "new"])  # multi-tag: counts in both

        # vip: email 4 delivered / 3 converted (0.75); sms 1/0 (0.0)
        _send(db, b, c1, channel=Channel.email, converted=True)
        _send(db, b, c1, channel=Channel.email, converted=True)
        _send(db, b, c1, channel=Channel.sms)
        _send(db, b, c2, channel=Channel.email)
        _send(db, b, c5, channel=Channel.email, converted=True)
        # new: sms 2/2 (1.0); email 1/1 (1.0) -> tie broken by delivered desc
        _send(db, b, c3, channel=Channel.sms, converted=True)
        _send(db, b, c3, channel=Channel.sms, converted=True)
        # untagged: email 1/0 (0.0)
        _send(db, b, c4, channel=Channel.email)
        db.commit()

        created = await _summarize_business_if_due(db, b, SUNDAY_2330)
        assert created is True
        db.commit()

        segments = db.query(WeeklySummary).one().best_channel_per_segment
        assert segments == {
            "new": {"channel": "sms", "conversion_rate": 1.0, "delivered": 2},
            "untagged": {"channel": "email", "conversion_rate": 0.0, "delivered": 1},
            "vip": {"channel": "email", "conversion_rate": 0.75, "delivered": 4},
        }

    async def test_sends_outside_window_ignored(self, db):
        b = _business(db)
        a = _asset(db, b, "Launch email", AssetKind.email_copy)
        contact = _contact(db, b)
        old = Send(
            business_id=b.id,
            contact_id=contact.id,
            asset_id=a.id,
            channel=Channel.email,
            to_address=contact.email,
            subject="",
            body="old",
            status=SendStatus.delivered,
            converted_at=datetime(2026, 9, 1, 12, 0, 0),
            created_at=datetime(2026, 9, 1, 12, 0, 0),  # 40 days ago
        )
        db.add(old)
        db.commit()

        await _summarize_business_if_due(db, b, SUNDAY_2330)
        db.commit()
        row = db.query(WeeklySummary).one()
        assert row.top_assets == [] and row.bottom_assets == []
        assert row.best_channel_per_segment == {}

    async def test_idempotent_skip_on_rerun(self, db):
        b = _business(db)
        _engagement_fixture(db, b)

        assert await _summarize_business_if_due(db, b, SUNDAY_2330) is True
        db.commit()
        # Second call for the same week is a no-op (unique constraint backstop).
        assert await _summarize_business_if_due(db, b, SUNDAY_2330) is False
        db.commit()
        assert db.query(WeeklySummary).count() == 1

    async def test_not_due_no_row(self, db):
        b = _business(db)
        _engagement_fixture(db, b)
        monday = datetime(2026, 10, 12, 23, 30, tzinfo=UTC)
        assert await _summarize_business_if_due(db, b, monday) is False
        db.commit()
        assert db.query(WeeklySummary).count() == 0


# ---------------------------------------------------------------------------
# recommendation paragraph
# ---------------------------------------------------------------------------


class TestRecommendation:
    def test_stub_template(self):
        top = [
            {
                "asset_id": "a",
                "title": "Launch email",
                "kind": "email_copy",
                "delivered": 10,
                "converted": 9,
                "conversion_rate": 0.9,
            }
        ]
        bottom = [
            {
                "asset_id": "e",
                "title": "Old ad",
                "kind": "ad",
                "delivered": 10,
                "converted": 0,
                "conversion_rate": 0.0,
            }
        ]
        segments = {"vip": {"channel": "email", "conversion_rate": 0.75, "delivered": 4}}
        totals = {"email": (14, 12), "sms": (3, 2)}
        assert _stub_recommendation(top, bottom, segments, totals) == (
            "Top performer: 'Launch email' (email_copy) converted at 90.0% "
            "of delivered sends. Weakest: 'Old ad' at 0.0%. "
            "Best channel for 'vip' is email. "
            "Do more of this: email_copy via email."
        )

    def test_stub_empty_data(self):
        text = _stub_recommendation([], [], {}, {})
        assert "No delivered sends" in text

    async def test_job_uses_stub_template_on_stub_provider(self, db):
        b = _business(db)
        _engagement_fixture(db, b)
        await _summarize_business_if_due(db, b, SUNDAY_2330)
        db.commit()
        row = db.query(WeeklySummary).one()
        assert row.recommendation.startswith("Top performer: 'Launch email'")


# ---------------------------------------------------------------------------
# the job itself
# ---------------------------------------------------------------------------


class TestWeeklySummaryJob:
    async def test_job_creates_per_business(self, db, monkeypatch):
        b1 = _business(db)
        b2 = _business(db, timezone="America/Chicago")
        _engagement_fixture(db, b1)
        monkeypatch.setattr(jobs, "utcnow", lambda: SUNDAY_2330)

        result = await weekly_summary(FAKE_CTX)

        assert result["ok"] is True
        assert result["businesses_processed"] == 2
        # b2's local time is Sunday 18:30 CDT -> not its moment yet.
        assert result["summaries_created"] == 1
        rows = db.query(WeeklySummary).all()
        assert [r.business_id for r in rows] == [b1.id]

    async def test_job_continues_past_bad_business(self, db, monkeypatch):
        b1 = _business(db)
        b2 = _business(db)
        db.commit()  # the job opens its own session; it only sees committed rows
        monkeypatch.setattr(jobs, "utcnow", lambda: SUNDAY_2330)
        orig = jobs._summarize_business_if_due

        async def flaky(db_, business, now):
            if business.id == b1.id:
                raise RuntimeError("boom")
            return await orig(db_, business, now)

        monkeypatch.setattr(jobs, "_summarize_business_if_due", flaky)
        result = await weekly_summary(FAKE_CTX)

        assert result["ok"] is True
        assert result["businesses_processed"] == 2
        assert result["summaries_created"] == 1

    async def test_job_noop_outside_window(self, db, monkeypatch):
        _business(db)
        db.commit()  # the job opens its own session; it only sees committed rows
        # Real current time is (almost surely) not Sunday 23:00 UTC for a
        # tz=UTC business; the job must be a cheap no-op, never a crash.
        result = await weekly_summary(FAKE_CTX)
        assert result["ok"] is True
        assert result["businesses_processed"] == 1
        assert result["summaries_created"] == 0

    async def test_job_database_unavailable(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "")
        result = await weekly_summary(FAKE_CTX)
        assert result["ok"] is False
        assert result["error"] == "database unavailable"


# ---------------------------------------------------------------------------
# Origination wiring
# ---------------------------------------------------------------------------


class TestOriginationWiring:
    def test_with_weekly_evidence_appends(self, db):
        b = _business(db)
        assert _with_weekly_evidence("draft", db, b.id) == "draft"

        db.add(
            WeeklySummary(
                business_id=b.id,
                week_start=date(2026, 10, 5),
                top_assets=[],
                bottom_assets=[],
                best_channel_per_segment={},
                recommendation="Do more email.",
            )
        )
        db.commit()
        out = _with_weekly_evidence("draft", db, b.id)
        assert out == "draft\n\nLast week's evidence — let it shape this draft:\nDo more email."

    def test_blank_recommendation_ignored(self, db):
        b = _business(db)
        db.add(
            WeeklySummary(
                business_id=b.id,
                week_start=date(2026, 10, 5),
                top_assets=[],
                bottom_assets=[],
                best_channel_per_segment={},
                recommendation="   ",
            )
        )
        db.commit()
        assert _with_weekly_evidence("draft", db, b.id) == "draft"

    async def test_generate_asset_with_summary_still_succeeds(self, db, db_url):
        b = _business(db)
        db.add(
            AutopilotSettings(
                business_id=b.id,
                auto_approve=False,
                daily_send_cap=500,
                quiet_hours_start=22,
                quiet_hours_end=8,
            )
        )
        db.add(
            WeeklySummary(
                business_id=b.id,
                week_start=date(2026, 10, 5),
                top_assets=[],
                bottom_assets=[],
                best_channel_per_segment={},
                recommendation="Do more email.",
            )
        )
        asset = Asset(
            business_id=b.id,
            kind=AssetKind.email_copy,
            title="October promo",
            variables={"offer_details": "50% off everything"},
            status=AssetStatus.draft,
        )
        db.add(asset)
        db.commit()
        db.close()

        result = await generate_asset(FAKE_CTX, str(asset.id))

        assert result["ok"] is True
        assert result["status"] == "in_review"
        assert result["provider"] == "stub"
