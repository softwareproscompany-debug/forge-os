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

    conn.commit()
    print("[seed] done. Summary:")
    for line in created:
        print("  ", line)
    print(f"[seed] demo login: {DEMO_EMAIL} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
