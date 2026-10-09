#!/usr/bin/env python3
"""
ForgeOS demo seed — idempotent, dependency-light.

Creates the Acme Demo Co tenant with an owner user, brand kit, contacts,
templates, an approved asset set, a running campaign, autopilot settings,
and a handful of sends + engagement events.

Requirements: psycopg2-binary ONLY (no ORM, no app imports — runs standalone).
Usage:
    DATABASE_URL=postgresql+psycopg2://forge:forge@localhost:5432/forge python3 infra/seed.py
    # or inside compose:
    make seed

Every insert is guarded by an existence check, so re-running is a no-op.
"""
from __future__ import annotations

import base64
from datetime import date, timedelta
import hashlib
import hmac
import json
import os
import sys
import urllib.parse

try:
    import psycopg2 as _psycopg2  # noqa: F401  (lazy import; see connect())
    _HAVE_PSYCOPG2 = True
except ImportError:
    _HAVE_PSYCOPG2 = False

DEMO_EMAIL = "demo@forgeos.local"
DEMO_PASSWORD = "demo1234"
BUSINESS_SLUG = "acme-demo"

DEMO2_EMAIL = "demo2@forgeos.local"
DEMO2_PASSWORD = "demo1234"
BETA_BUSINESS_SLUG = "beta-demo"


# --------------------------------------------------------------------------
# Password hashing: passlib's pbkdf2_sha256 when available, otherwise pure
# hashlib producing the IDENTICAL modular-crypt format so passlib.verify()
# (used by the api workstream per CONTRACTS.md) accepts the hash:
#   $pbkdf2-sha256$<rounds>$<salt>$<checksum>
# salt/checksum are passlib "ab64" (base64 with '+' -> '.', padding stripped).
# --------------------------------------------------------------------------
def _ab64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=").replace("+", ".")


def hash_password(password: str, rounds: int = 29000) -> str:
    try:
        from passlib.hash import pbkdf2_sha256  # type: ignore

        return pbkdf2_sha256.using(rounds=rounds).hash(password)
    except ImportError:
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds, dklen=32)
        return f"$pbkdf2-sha256${rounds}${_ab64(salt)}${_ab64(dk)}"


def verify_selftest() -> None:
    """Sanity check: pure-python hash must verify against itself."""
    h = hash_password("selftest")
    assert h.startswith("$pbkdf2-sha256$29000$"), f"bad format: {h[:30]}"
    parts = h.split("$")
    assert len(parts) == 5, f"expected 5 $-segments, got {len(parts)}"
    rounds, salt_b64, chk_b64 = parts[2], parts[3], parts[4]

    def unab64(s: str) -> bytes:
        return base64.b64decode(s.replace(".", "+") + "=" * (-len(s) % 4))

    dk = hashlib.pbkdf2_hmac("sha256", b"selftest", unab64(salt_b64), int(rounds), dklen=32)
    assert hmac.compare_digest(dk, unab64(chk_b64)), "hash does not verify"
    print("[seed] password-hash self-test OK")


def connect():
    if not _HAVE_PSYCOPG2:
        sys.exit("psycopg2 is required: pip install psycopg2-binary")
    import psycopg2

    url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://forge:forge@localhost:5432/forge")
    # psycopg2 doesn't understand the SQLAlchemy "+psycopg2" driver suffix.
    url = url.replace("postgresql+psycopg2://", "postgresql://", 1).replace(
        "postgres+psycopg2://", "postgres://", 1
    )
    parts = urllib.parse.urlparse(url)
    return psycopg2.connect(
        host=parts.hostname or "localhost",
        port=parts.port or 5432,
        user=urllib.parse.unquote(parts.username or "forge"),
        password=urllib.parse.unquote(parts.password or "forge"),
        dbname=(parts.path or "/forge").lstrip("/"),
    )


def get_id(cur, table: str, where: str, params) -> str | None:
    cur.execute(f"SELECT id FROM {table} WHERE {where} LIMIT 1", params)
    row = cur.fetchone()
    return str(row[0]) if row else None


# --------------------------------------------------------------------------
# Beta Demo Co — second demo tenant, visibly through all five FORGE layers
# on first boot: completed Card-0 interview -> interview-drafted brand kit
# (Card 1) -> stub-generated approved assets (Card 2) -> running campaign
# (Card 3) -> weekly summary + draft content plan (Cards 4/5).
# Idempotent: every insert is guarded, re-running is a no-op.
# --------------------------------------------------------------------------
def seed_beta(cur, mark) -> None:
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    next_monday = today + timedelta(days=7 - today.weekday())

    # ---- business ------------------------------------------------------
    beta_business_id = get_id(cur, "businesses", "slug = %s", (BETA_BUSINESS_SLUG,))
    if not beta_business_id:
        cur.execute(
            "INSERT INTO businesses (name, slug, timezone) VALUES (%s, %s, %s) RETURNING id",
            ("Beta Demo Co", BETA_BUSINESS_SLUG, "America/Chicago"),
        )
        beta_business_id = str(cur.fetchone()[0])
        mark("beta/business beta-demo", True)
    else:
        mark("beta/business beta-demo", False)

    # ---- owner user ----------------------------------------------------
    beta_user_id = get_id(cur, "users", "email = %s", (DEMO2_EMAIL,))
    if not beta_user_id:
        cur.execute(
            """INSERT INTO users (email, password_hash, full_name, business_id, role, is_active)
               VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id""",
            (
                DEMO2_EMAIL, hash_password(DEMO2_PASSWORD), "Beta Demo Owner",
                beta_business_id, "owner",
            ),
        )
        beta_user_id = str(cur.fetchone()[0])
        mark(f"beta/user {DEMO2_EMAIL}", True)
    else:
        mark(f"beta/user {DEMO2_EMAIL}", False)

    # ---- brand kit (drafted from the Card-0 interview below) ------------
    beta_kit_id = get_id(
        cur, "brand_kits", "business_id = %s AND name = %s",
        (beta_business_id, "Beta Interview Kit"),
    )
    if not beta_kit_id:
        cur.execute(
            """INSERT INTO brand_kits
               (business_id, name, voice_description, tone_tags, primary_color, secondary_color,
                fonts, icp_description, do_list, dont_list, version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING id""",
            (
                beta_business_id,
                "Beta Interview Kit",
                "Plain-spoken, handy, neighborly. Short sentences. "
                "Talks like a mechanic who respects your wallet.",
                json.dumps(["warm", "practical", "straightforward"]),
                None, None, json.dumps({}),
                "Bike commuters in the metro area who'd rather repair than replace.",
                json.dumps(["Mention the free 30-point tune-up", "Use the customer's first name"]),
                json.dumps(["No all-caps", "No 'game-changer'", "No fake urgency"]),
            ),
        )
        beta_kit_id = str(cur.fetchone()[0])
        mark("beta/brand_kit Beta Interview Kit", True)
    else:
        mark("beta/brand_kit Beta Interview Kit", False)

    # ---- interview session (Card 0, completed) --------------------------
    interview_answers = [
        ("What does your business sell, and to whom?",
         "Beta Bike Works repairs and sells refurbished bikes to daily commuters "
         "in the metro area — people who'd rather repair than replace."),
        ("What is the offer you are proudest of?",
         "A free 30-point tune-up with any repair. It gets people in the door "
         "and keeps their bikes safe."),
        ("What five marketing tasks do you actually do each week?",
         "A weekly newsletter, Instagram posts, SMS service reminders, asking "
         "happy customers for referrals, and following up on reviews."),
        ("Where do your customers hang out?",
         "Instagram and local Facebook cycling groups."),
        ("Which three brands' marketing do you admire, and why?",
         "Rivendell — plain-spoken expertise. REI — genuinely helpful guides. "
         "The local coffee shop down the street — a warm, regulars-first voice."),
        ("What words or phrases should never be used?",
         "'synergy', 'game-changer', and all-caps shouting."),
        ("What was the one campaign that flopped, and what went wrong?",
         "A 40%-off blast email with no segmentation. I sent it to everyone at "
         "once — no targeting, no follow-up. Lesson: fewer people, more relevance."),
    ]
    answers_json = [
        {"question": q, "answer": a, "followup": False}
        for q, a in interview_answers
    ]
    draft_brand_kit = {
        "name": "Beta Interview Kit",
        "business": "Beta Bike Works — repairs and refurbished bikes for commuters.",
        "audience": "Bike commuters in the metro area who'd rather repair than replace.",
        "voice": "Plain-spoken, handy, neighborly. Short sentences. "
                 "Talks like a mechanic who respects your wallet.",
        "never_say": ["synergy", "game-changer", "all-caps shout"],
        "offers": ["Free 30-point tune-up with any repair"],
    }
    isid = get_id(
        cur, "interview_sessions", "business_id = %s AND status = %s",
        (beta_business_id, "completed"),
    )
    if not isid:
        cur.execute(
            """INSERT INTO interview_sessions
               (business_id, user_id, status, current_index, answers,
                draft_brand_kit, brand_kit_id, completed_at)
               VALUES (%s,%s,'completed',7,%s,%s,%s,NOW()) RETURNING id""",
            (
                beta_business_id, beta_user_id,
                json.dumps(answers_json), json.dumps(draft_brand_kit), beta_kit_id,
            ),
        )
        mark("beta/interview_session completed", True)
    else:
        mark("beta/interview_session completed", False)

    # ---- contacts (6, mixed consent) ------------------------------------
    beta_contacts = [
        # first, last, email, phone, source, tags, consent_email, consent_sms, unsubscribed
        ("Mia", "Nguyen", "mia@example.com", "+15551240001", "webform", ["commuter"], True, True, False),
        ("Noah", "Carter", "noah@example.com", "+15551240002", "import", ["commuter"], True, False, False),
        ("Olivia", "Diaz", "olivia@example.com", "+15551240003", "referral", ["vip"], True, True, False),
        ("Pete", "Okafor", "pete@example.com", "+15551240004", "import", ["commuter"], True, False, False),
        ("Quinn", "Lindqvist", "quinn@example.com", "+15551240005", "webform", ["vip"], False, True, False),
        ("Ruth", "Moore", "ruth@example.com", "+15551240006", "import", [], False, False, True),
    ]
    beta_contact_info: dict[str, dict] = {}
    for first, last, email, phone, source, tags, ce, cs, unsub in beta_contacts:
        cid = get_id(
            cur, "contacts", "business_id = %s AND email = %s", (beta_business_id, email)
        )
        if not cid:
            cur.execute(
                """INSERT INTO contacts
                   (business_id, email, phone, first_name, last_name, source, tags,
                    consent_email, consent_email_at, consent_sms, consent_sms_at,
                    unsubscribed, custom_fields)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                           CASE WHEN %s THEN NOW() END,
                           %s,
                           CASE WHEN %s THEN NOW() END,
                           %s,%s) RETURNING id""",
                (
                    beta_business_id, email, phone, first, last, source, json.dumps(tags),
                    ce, ce, cs, cs,
                    unsub, json.dumps({}),
                ),
            )
            cid = str(cur.fetchone()[0])
            mark(f"beta/contact {email}", True)
        else:
            mark(f"beta/contact {email}", False)
        beta_contact_info[email] = {"id": cid, "first": first, "tags": tags}

    # ---- templates (email + sms) -----------------------------------------
    beta_templates = [
        (
            "Beta welcome email", "email",
            "Your free tune-up is waiting, {{ first_name | default('friend') }}",
            "Hi {{ first_name | default('there') }},\n\n"
            "Welcome to Beta Bike Works. Every repair comes with a free 30-point "
            "tune-up — because a commuter's bike should never leave the shop "
            "less safe than it arrived.\n\n"
            "Book your first service this week and the tune-up is on us.\n\n"
            "— The Beta Bike Works crew",
            ["first_name"],
        ),
        (
            "Beta nudge SMS", "sms", None,
            "Hi {{ first_name }}, Beta Bike Works: your free 30-point tune-up "
            "coupon expires Sunday. Book: {{ booking_url }} Reply STOP to opt out.",
            ["first_name", "booking_url"],
        ),
    ]
    beta_template_ids: dict[str, str] = {}
    for name, channel, subject, body, variables in beta_templates:
        tid = get_id(
            cur, "templates", "business_id = %s AND name = %s", (beta_business_id, name)
        )
        if not tid:
            cur.execute(
                """INSERT INTO templates (business_id, name, channel, subject_template, body_template, variables)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (beta_business_id, name, channel, subject, body, json.dumps(variables)),
            )
            tid = str(cur.fetchone()[0])
            mark(f"beta/template {name}", True)
        else:
            mark(f"beta/template {name}", False)
        beta_template_ids[name] = tid

    # ---- approved assets (3, stub-generated) ------------------------------
    beta_assets = [
        ("email_copy", "Beta welcome — tune-up email",
         "Hi {{ first_name }},\n\nWelcome to Beta Bike Works — every repair comes "
         "with a free 30-point tune-up. Book this week, it's on us.",
         {"first_name": "Mia"}),
        ("sms", "Beta welcome — tune-up SMS",
         "Hi {{ first_name }}, Beta Bike Works: your free 30-point tune-up coupon "
         "expires Sunday: {{ booking_url }}",
         {"first_name": "Mia", "booking_url": "https://beta.example/book"}),
        ("social_post", "Beta welcome — tune-up social",
         "Commuters: free 30-point tune-up with any repair at Beta Bike Works. "
         "Keep the bike you have. #BikeCommute",
         {}),
    ]
    beta_asset_ids: dict[str, str] = {}
    beta_asset_titles: dict[str, str] = {}
    for kind, title, body, variables in beta_assets:
        aid = get_id(
            cur, "assets", "business_id = %s AND title = %s", (beta_business_id, title)
        )
        if not aid:
            cur.execute(
                """INSERT INTO assets
                   (business_id, kind, title, body, variables, version, status,
                    brand_kit_version, created_by, approved_by,
                    cost_usd, tokens_in, tokens_out, llm_provider, llm_model)
                   VALUES (%s,%s,%s,%s,%s,1,'approved',1,%s,%s,0,0,0,'stub','stub')
                   RETURNING id""",
                (beta_business_id, kind, title, body, json.dumps(variables),
                 beta_user_id, beta_user_id),
            )
            aid = str(cur.fetchone()[0])
            mark(f"beta/asset {title}", True)
        else:
            mark(f"beta/asset {title}", False)
        beta_asset_ids[kind] = aid
        beta_asset_titles[kind] = title

    # ---- campaign + steps -------------------------------------------------
    beta_campaign_id = get_id(
        cur, "campaigns", "business_id = %s AND name = %s",
        (beta_business_id, "Beta Welcome Series"),
    )
    if not beta_campaign_id:
        cur.execute(
            """INSERT INTO campaigns
               (business_id, name, description, status, autopilot, created_by, starts_at, timezone)
               VALUES (%s,%s,%s,'running',FALSE,%s,NOW() - INTERVAL '2 days','America/Chicago')
               RETURNING id""",
            (
                beta_business_id, "Beta Welcome Series",
                "Demo drip for Beta Bike Works: welcome email, 48h SMS nudge, social announcement.",
                beta_user_id,
            ),
        )
        beta_campaign_id = str(cur.fetchone()[0])
        mark("beta/campaign Beta Welcome Series", True)
    else:
        mark("beta/campaign Beta Welcome Series", False)

    beta_steps = [
        # position, channel, template_name, asset_kind, delay_hours
        (0, "email", "Beta welcome email", "email_copy", 0),
        (1, "sms", "Beta nudge SMS", "sms", 48),
        (2, "social", None, "social_post", 24),
    ]
    beta_step_ids: dict[int, str] = {}
    for position, channel, tname, akind, delay_hours in beta_steps:
        sid = get_id(
            cur, "campaign_steps", "campaign_id = %s AND position = %s",
            (beta_campaign_id, position),
        )
        if not sid:
            cur.execute(
                """INSERT INTO campaign_steps
                   (campaign_id, position, channel, template_id, asset_id, delay_hours, trigger_event)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    beta_campaign_id, position, channel,
                    beta_template_ids[tname] if tname else None,
                    beta_asset_ids[akind], delay_hours, None,
                ),
            )
            sid = str(cur.fetchone()[0])
            mark(f"beta/campaign_step {position} ({channel})", True)
        else:
            mark(f"beta/campaign_step {position} ({channel})", False)
        beta_step_ids[position] = sid

    # ---- autopilot settings (manual approval to start) ---------------------
    cur.execute(
        "SELECT business_id FROM autopilot_settings WHERE business_id = %s", (beta_business_id,)
    )
    if not cur.fetchone():
        cur.execute(
            """INSERT INTO autopilot_settings
               (business_id, auto_approve, require_approval_for_channels,
                daily_send_cap, quiet_hours_start, quiet_hours_end)
               VALUES (%s, FALSE, %s, 500, 22, 8)""",
            (beta_business_id, json.dumps([])),
        )
        mark("beta/autopilot_settings", True)
    else:
        mark("beta/autopilot_settings", False)

    # ---- sends (8) + engagement events --------------------------------------
    beta_sends = [
        # email, channel, step_pos, asset_kind, status, days_ago, opened, clicked, converted
        ("mia@example.com", "email", 0, "email_copy", "delivered", 3, True, True, True),
        ("noah@example.com", "email", 0, "email_copy", "delivered", 2, True, True, False),
        ("olivia@example.com", "email", 0, "email_copy", "delivered", 2, True, False, False),
        ("pete@example.com", "email", 0, "email_copy", "delivered", 1, False, False, False),
        ("quinn@example.com", "email", 0, "email_copy", "sent", 0, False, False, False),
        ("mia@example.com", "sms", 1, "sms", "delivered", 2, False, False, True),
        ("olivia@example.com", "sms", 1, "sms", "delivered", 1, True, False, False),
        ("noah@example.com", "social", 2, "social_post", "delivered", 1, True, True, False),
    ]
    beta_bodies = {
        "email": ("Your free tune-up is waiting",
                  "Hi — thanks for joining Beta Bike Works. Every repair comes with a free 30-point tune-up..."),
        "sms": (None,
                "Beta Bike Works: your free 30-point tune-up coupon expires Sunday. "
                "Book: https://beta.example/book Reply STOP to opt out."),
        "social": (None,
                   "Commuters: free 30-point tune-up with any repair at Beta Bike Works. #BikeCommute"),
    }
    for idx, (email, channel, pos, akind, status, days_ago, opened, clicked, converted) in enumerate(beta_sends):
        info = beta_contact_info[email]
        existing = get_id(
            cur, "sends",
            "business_id = %s AND campaign_id = %s AND contact_id = %s AND channel = %s",
            (beta_business_id, beta_campaign_id, info["id"], channel),
        )
        if existing:
            mark(f"beta/send -> {email} ({channel})", False)
            continue
        base = timedelta(days=days_ago)
        was_sent = status in ("sent", "delivered")
        subject, body = beta_bodies[channel]
        cur.execute(
            """INSERT INTO sends
               (business_id, campaign_id, step_id, contact_id, channel, asset_id,
                to_address, subject, body, status, provider_message_id,
                scheduled_for, sent_at, opened_at, clicked_at, converted_at, meta)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       NOW() - %s,
                       CASE WHEN %s THEN NOW() - %s + INTERVAL '5 minutes' END,
                       CASE WHEN %s THEN NOW() - %s + INTERVAL '1 hour' END,
                       CASE WHEN %s THEN NOW() - %s + INTERVAL '2 hours' END,
                       CASE WHEN %s THEN NOW() - %s + INTERVAL '3 hours' END,
                       %s) RETURNING id""",
            (
                beta_business_id, beta_campaign_id, beta_step_ids[pos], info["id"],
                channel, beta_asset_ids[akind],
                email, subject, body,
                status, f"stub-beta-{idx:02d}",
                base, was_sent, base, opened, base, clicked, base, converted, base,
                json.dumps({"seed": True, "business": "beta-demo"}),
            ),
        )
        send_id = str(cur.fetchone()[0])
        mark(f"beta/send -> {email} ({channel}, {status})", True)
        if channel == "email" and opened:
            cur.execute(
                "INSERT INTO events (business_id, contact_id, kind, payload) VALUES (%s,%s,'email_opened',%s)",
                (beta_business_id, info["id"], json.dumps({"send_id": send_id})),
            )
        if channel == "email" and clicked:
            cur.execute(
                "INSERT INTO events (business_id, contact_id, kind, payload) VALUES (%s,%s,'email_clicked',%s)",
                (beta_business_id, info["id"],
                 json.dumps({"send_id": send_id, "url": "https://beta.example/book"})),
            )
        if converted:
            cur.execute(
                "INSERT INTO events (business_id, contact_id, kind, payload) VALUES (%s,%s,'converted',%s)",
                (beta_business_id, info["id"], json.dumps({"send_id": send_id})),
            )

    # ---- weekly summary (Card 5) — computed from the seeded sends ----------
    per_asset: dict[str, dict] = {}
    for _email, _ch, _pos, akind, status, _d, opened, _clicked, converted in beta_sends:
        st = per_asset.setdefault(akind, {"delivered": 0, "opened": 0, "converted": 0})
        if status == "delivered":
            st["delivered"] += 1
            st["opened"] += 1 if opened else 0
            st["converted"] += 1 if converted else 0

    def asset_entry(akind: str) -> dict:
        st = per_asset.get(akind, {"delivered": 0, "converted": 0})
        delivered, converted = st["delivered"], st["converted"]
        return {
            "asset_id": beta_asset_ids[akind],
            "title": beta_asset_titles[akind],
            "kind": akind,
            "delivered": delivered,
            "converted": converted,
            "conversion_rate": round(converted / delivered, 3) if delivered else 0.0,
        }

    ranked = sorted(
        (asset_entry(k) for k in beta_asset_ids),
        key=lambda e: (e["converted"], e["conversion_rate"]),
        reverse=True,
    )
    top_assets = ranked[:3]
    bottom_assets = list(reversed(ranked))[:2]

    channels_order = ["email", "sms", "social"]
    best_channel_per_segment = {}
    for segment, members in {
        "commuter": ["mia@example.com", "noah@example.com", "pete@example.com"],
        "vip": ["olivia@example.com", "quinn@example.com"],
    }.items():
        best = None
        for channel in channels_order:
            delivered = converted = opened = 0
            for (email, ch, _pos, _ak, status, _d, op, _cl, cv) in beta_sends:
                if ch != channel or email not in members or status != "delivered":
                    continue
                delivered += 1
                opened += 1 if op else 0
                converted += 1 if cv else 0
            conv_rate = round(converted / delivered, 3) if delivered else 0.0
            open_rate = round(opened / delivered, 3) if delivered else 0.0
            score = (conv_rate, open_rate, delivered)
            if best is None or score > best[0]:
                best = (score, {
                    "channel": channel,
                    "delivered": delivered,
                    "converted": converted,
                    "conversion_rate": conv_rate,
                    "open_rate": open_rate,
                })
        best_channel_per_segment[segment] = best[1]

    sms_entry = next(e for e in top_assets if e["kind"] == "sms")
    email_entry = next(e for e in top_assets if e["kind"] == "email_copy")
    recommendation = (
        f"SMS beat email for the commuter segment: {sms_entry['conversion_rate']:.0%} "
        f"conversion on {sms_entry['delivered']} delivered sends vs "
        f"{email_entry['conversion_rate']:.0%} on {email_entry['delivered']} delivered emails. "
        f"The free 30-point tune-up offer drove the only conversions, so keep it "
        f"in every subject line and move short reminders to SMS. Social got clicks "
        f"but no conversions — repurpose that slot for review asks next week. "
        f"Sample is small (8 sends), so treat this as direction, not proof."
    )

    summary_id = get_id(
        cur, "weekly_summaries", "business_id = %s AND week_start = %s",
        (beta_business_id, last_monday),
    )
    if not summary_id:
        cur.execute(
            """INSERT INTO weekly_summaries
               (business_id, week_start, top_assets, bottom_assets,
                best_channel_per_segment, recommendation)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                beta_business_id, last_monday,
                json.dumps(top_assets), json.dumps(bottom_assets),
                json.dumps(best_channel_per_segment), recommendation,
            ),
        )
        mark(f"beta/weekly_summary {last_monday}", True)
    else:
        mark(f"beta/weekly_summary {last_monday}", False)

    # ---- content plan (Card 4) — draft for next week ------------------------
    plan_items = [
        {"day": 1, "channel": "email",
         "brief": "Welcome email for new signups: lead with the free 30-point "
                  "tune-up offer and a photo of a finished commuter rebuild.",
         "asset_id": beta_asset_ids["email_copy"]},
        {"day": 3, "channel": "social",
         "brief": "Instagram post: before/after of a commuter bike refresh, "
                  "plain-spoken caption, no hashtag spam.",
         "asset_id": beta_asset_ids["social_post"]},
        {"day": 5, "channel": "sms",
         "brief": "SMS reminder: tune-up coupon expires Sunday; include the "
                  "booking link.",
         "asset_id": beta_asset_ids["sms"]},
    ]
    plan_id = get_id(
        cur, "content_plans", "business_id = %s AND week_start = %s AND status = %s",
        (beta_business_id, next_monday, "draft"),
    )
    if not plan_id:
        cur.execute(
            """INSERT INTO content_plans
               (business_id, week_start, status, items, created_by)
               VALUES (%s,%s,'draft',%s,%s) RETURNING id""",
            (beta_business_id, next_monday, json.dumps(plan_items), beta_user_id),
        )
        mark(f"beta/content_plan {next_monday}", True)
    else:
        mark(f"beta/content_plan {next_monday}", False)


def main() -> None:
    verify_selftest()
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()
    created: list[str] = []

    def mark(label: str, was_created: bool) -> None:
        created.append(f"{'created' if was_created else 'exists '} {label}")

    # ---- business ---------------------------------------------------------
    business_created = False
    business_id = get_id(cur, "businesses", "slug = %s", (BUSINESS_SLUG,))
    if not business_id:
        cur.execute(
            "INSERT INTO businesses (name, slug, timezone) VALUES (%s, %s, %s) RETURNING id",
            ("Acme Demo Co", BUSINESS_SLUG, "America/Chicago"),
        )
        business_id = str(cur.fetchone()[0])
        business_created = True
    mark(f"business {BUSINESS_SLUG}", business_created)

    # ---- owner user -------------------------------------------------------
    user_id = get_id(cur, "users", "email = %s", (DEMO_EMAIL,))
    if not user_id:
        cur.execute(
            """INSERT INTO users (email, password_hash, full_name, business_id, role, is_active)
               VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id""",
            (DEMO_EMAIL, hash_password(DEMO_PASSWORD), "Demo Owner", business_id, "owner"),
        )
        user_id = str(cur.fetchone()[0])
        mark(f"user {DEMO_EMAIL}", True)
    else:
        mark(f"user {DEMO_EMAIL}", False)

    # ---- brand kit --------------------------------------------------------
    brand_kit_id = get_id(cur, "brand_kits", "business_id = %s AND name = %s", (business_id, "Acme Default"))
    if not brand_kit_id:
        cur.execute(
            """INSERT INTO brand_kits
               (business_id, name, voice_description, tone_tags, primary_color, secondary_color,
                fonts, icp_description, do_list, dont_list, version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING id""",
            (
                business_id,
                "Acme Default",
                "Friendly, confident, and plain-spoken. Short sentences. No hype, no jargon.",
                json.dumps(["warm", "direct", "playful"]),
                "#1D4ED8",
                "#F59E0B",
                json.dumps({"heading": "Inter", "body": "Inter"}),
                "Busy small-business owners (10-50 staff) who want marketing that runs itself.",
                json.dumps(["Use the customer's first name", "One clear call to action", "Mention the 30-day guarantee"]),
                json.dumps(["No all-caps subject lines", "No fake urgency", "No emojis in email subjects"]),
            ),
        )
        brand_kit_id = str(cur.fetchone()[0])
        mark("brand_kit Acme Default", True)
    else:
        mark("brand_kit Acme Default", False)

    # ---- contacts (12, mixed consent) --------------------------------------
    contacts = [
        # first, last, email, phone, source, tags, consent_email, consent_sms, unsubscribed
        ("Alice", "Nguyen", "alice@example.com", "+15551230001", "import", ["newsletter"], True, True, False),
        ("Bob", "Carter", "bob@example.com", "+15551230002", "import", [], True, False, False),
        ("Carla", "Diaz", "carla@example.com", "+15551230003", "webform", [], False, True, False),
        ("Dan", "Okafor", "dan@example.com", "+15551230004", "import", ["lead"], True, False, False),
        ("Eva", "Lindqvist", "eva@example.com", "+15551230005", "webform", [], False, False, False),
        ("Frank", "Moore", "frank@example.com", "+15551230006", "import", [], True, True, False),
        ("Grace", "Kim", "grace@example.com", "+15551230007", "referral", [], True, False, False),
        ("Henry", "Patel", "henry@example.com", "+15551230008", "referral", [], False, True, False),
        ("Irene", "Rossi", "irene@example.com", "+15551230009", "import", ["vip"], True, True, False),
        ("Jack", "Turner", "jack@example.com", "+15551230010", "webform", [], True, False, False),
        ("Karen", "White", "karen@example.com", "+15551230011", "import", [], True, False, True),
        ("Leo", "Garcia", "leo@example.com", "+15551230012", "webform", ["lead"], True, False, False),
    ]
    contact_ids: dict[str, str] = {}
    for first, last, email, phone, source, tags, ce, cs, unsub in contacts:
        cid = get_id(cur, "contacts", "business_id = %s AND email = %s", (business_id, email))
        if not cid:
            cur.execute(
                """INSERT INTO contacts
                   (business_id, email, phone, first_name, last_name, source, tags,
                    consent_email, consent_email_at, consent_sms, consent_sms_at,
                    unsubscribed, custom_fields)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                           CASE WHEN %s THEN NOW() END,
                           %s,
                           CASE WHEN %s THEN NOW() END,
                           %s,%s) RETURNING id""",
                (
                    business_id, email, phone, first, last, source, json.dumps(tags),
                    ce, ce, cs, cs,
                    unsub, json.dumps({}),
                ),
            )
            cid = str(cur.fetchone()[0])
            mark(f"contact {email}", True)
        else:
            mark(f"contact {email}", False)
        contact_ids[email] = cid

    # ---- templates (3) -----------------------------------------------------
    templates = [
        (
            "Welcome email", "email",
            "Welcome to Acme, {{ first_name | default('friend') }} — let's get started",
            "Hi {{ first_name | default('there') }},\n\n"
            "Thanks for joining Acme Demo Co. Here's what happens next:\n\n"
            "1. Tell us about your goals (2 minutes).\n"
            "2. We draft your first campaign — you approve every word.\n"
            "3. You launch and watch the analytics roll in.\n\n"
            "Questions? Just reply to this email.\n\n— The Acme team",
            ["first_name"],
        ),
        (
            "Nudge SMS", "sms", None,
            "Hi {{ first_name }}, quick nudge from Acme — your spring offer ends Friday. "
            "Tap to claim: {{ offer_url }} Reply STOP to opt out.",
            ["first_name", "offer_url"],
        ),
        (
            "Launch social", "social", None,
            "Spring launch is LIVE. 30 days, zero guesswork, marketing that runs itself. "
            "#SmallBusiness #MarketingAutomation",
            [],
        ),
    ]
    template_ids: dict[str, str] = {}
    for name, channel, subject, body, variables in templates:
        tid = get_id(cur, "templates", "business_id = %s AND name = %s", (business_id, name))
        if not tid:
            cur.execute(
                """INSERT INTO templates (business_id, name, channel, subject_template, body_template, variables)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (business_id, name, channel, subject, body, json.dumps(variables)),
            )
            tid = str(cur.fetchone()[0])
            mark(f"template {name}", True)
        else:
            mark(f"template {name}", False)
        template_ids[name] = tid

    # ---- approved assets (3) -----------------------------------------------
    assets = [
        ("email_copy", "Spring launch — welcome email",
         "Hi {{ first_name }},\n\nSpring is here and so is your launch plan...",
         {"first_name": "Alice"}),
        ("sms", "Spring launch — nudge SMS",
         "Hi {{ first_name }}, your spring offer ends Friday: {{ offer_url }}",
         {"first_name": "Alice", "offer_url": "https://acme.example/offer"}),
        ("social_post", "Spring launch — announcement",
         "Spring launch is LIVE. 30 days, zero guesswork.",
         {}),
    ]
    asset_ids: dict[str, str] = {}
    for kind, title, body, variables in assets:
        aid = get_id(cur, "assets", "business_id = %s AND title = %s", (business_id, title))
        if not aid:
            cur.execute(
                """INSERT INTO assets
                   (business_id, kind, title, body, variables, version, status,
                    brand_kit_version, created_by, approved_by,
                    cost_usd, tokens_in, tokens_out, llm_provider, llm_model)
                   VALUES (%s,%s,%s,%s,%s,1,'approved',1,%s,%s,0,0,0,'stub','stub')
                   RETURNING id""",
                (business_id, kind, title, body, json.dumps(variables), user_id, user_id),
            )
            aid = str(cur.fetchone()[0])
            mark(f"asset {title}", True)
        else:
            mark(f"asset {title}", False)
        asset_ids[kind] = aid

    # ---- campaign + steps ---------------------------------------------------
    campaign_id = get_id(cur, "campaigns", "business_id = %s AND name = %s", (business_id, "Spring Launch Drip"))
    if not campaign_id:
        cur.execute(
            """INSERT INTO campaigns
               (business_id, name, description, status, autopilot, created_by, starts_at, timezone)
               VALUES (%s,%s,%s,'running',FALSE,%s,NOW() - INTERVAL '2 days','America/Chicago')
               RETURNING id""",
            (business_id, "Spring Launch Drip",
             "Demo drip: welcome email, 48h wait, SMS nudge, social announcement.", user_id),
        )
        campaign_id = str(cur.fetchone()[0])
        mark("campaign Spring Launch Drip", True)
    else:
        mark("campaign Spring Launch Drip", False)

    steps = [
        # position, channel, template, asset_kind, delay_hours, trigger_event
        (0, "email", "Welcome email", "email_copy", 0, None),
        (1, "sms", "Nudge SMS", "sms", 48, None),
        (2, "social", "Launch social", "social_post", 24, None),
    ]
    step_ids: dict[int, str] = {}
    for position, channel, tname, akind, delay_hours, trigger in steps:
        sid = get_id(
            cur, "campaign_steps", "campaign_id = %s AND position = %s", (campaign_id, position)
        )
        if not sid:
            cur.execute(
                """INSERT INTO campaign_steps
                   (campaign_id, position, channel, template_id, asset_id, delay_hours, trigger_event)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (campaign_id, position, channel, template_ids[tname], asset_ids[akind], delay_hours, trigger),
            )
            sid = str(cur.fetchone()[0])
            mark(f"campaign_step {position} ({channel})", True)
        else:
            mark(f"campaign_step {position} ({channel})", False)
        step_ids[position] = sid

    # ---- autopilot settings --------------------------------------------------
    cur.execute("SELECT business_id FROM autopilot_settings WHERE business_id = %s", (business_id,))
    if not cur.fetchone():
        cur.execute(
            """INSERT INTO autopilot_settings
               (business_id, auto_approve, require_approval_for_channels,
                daily_send_cap, quiet_hours_start, quiet_hours_end)
               VALUES (%s, FALSE, %s, 500, 22, 8)""",
            (business_id, json.dumps(["sms"])),
        )
        mark("autopilot_settings", True)
    else:
        mark("autopilot_settings", False)

    # ---- sends (5) + engagement events ---------------------------------------
    step0 = step_ids[0]
    sends = [
        # email, status, opened, clicked
        ("alice@example.com", "delivered", True, True),
        ("bob@example.com", "delivered", True, False),
        ("dan@example.com", "delivered", False, False),
        ("frank@example.com", "sent", False, False),
        ("grace@example.com", "delivered", False, False),
    ]
    for email, status, opened, clicked in sends:
        cid = contact_ids[email]
        existing = get_id(
            cur, "sends",
            "business_id = %s AND campaign_id = %s AND contact_id = %s AND channel = 'email'",
            (business_id, campaign_id, cid),
        )
        if existing:
            mark(f"send -> {email}", False)
            continue
        cur.execute(
            """INSERT INTO sends
               (business_id, campaign_id, step_id, contact_id, channel, asset_id,
                to_address, subject, body, status, provider_message_id,
                scheduled_for, sent_at, opened_at, clicked_at, meta)
               VALUES (%s,%s,%s,%s,'email',%s,%s,%s,%s,%s,%s,
                       NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day',
                       CASE WHEN %s THEN NOW() - INTERVAL '20 hours' END,
                       CASE WHEN %s THEN NOW() - INTERVAL '18 hours' END, %s)
               RETURNING id""",
            (
                business_id, campaign_id, step0, cid, asset_ids["email_copy"],
                email, "Welcome to Acme — let's get started",
                "Hi — thanks for joining Acme Demo Co...",
                status, f"stub-{email.split('@')[0]}-001",
                opened, clicked, json.dumps({"seed": True}),
            ),
        )
        send_id = str(cur.fetchone()[0])
        mark(f"send -> {email} ({status})", True)
        if opened:
            cur.execute(
                "INSERT INTO events (business_id, contact_id, kind, payload) VALUES (%s,%s,'email_opened',%s)",
                (business_id, cid, json.dumps({"send_id": send_id})),
            )
        if clicked:
            cur.execute(
                "INSERT INTO events (business_id, contact_id, kind, payload) VALUES (%s,%s,'email_clicked',%s)",
                (business_id, cid, json.dumps({"send_id": send_id, "url": "https://acme.example/offer"})),
            )

    seed_beta(cur, mark)

    conn.commit()
    print("[seed] done. Summary:")
    for line in created:
        print("  ", line)
    print(f"[seed] demo login: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"[seed] beta  login: {DEMO2_EMAIL} / {DEMO2_PASSWORD}  (Beta Demo Co)")


if __name__ == "__main__":
    main()
