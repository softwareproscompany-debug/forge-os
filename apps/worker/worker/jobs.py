"""ForgeOS worker jobs (arq).

Four jobs, per CONTRACTS.md:

* :func:`generate_asset` — render a prompt from the asset kind + brand kit,
  call ``forge_llm.get_provider().generate()``, run guardrails, save the
  body/tokens/cost, and move the asset to ``in_review`` (or ``approved``
  when the business has autopilot auto-approve on).
* :func:`send_message` — consent/unsubscribe gating, Jinja2 content
  rendering, provider dispatch, sent/failed bookkeeping, arq ``Retry`` on
  transient provider errors.
* :func:`campaign_tick` — every-60s cron: bulk-enroll due contacts into
  running campaigns, advance due enrollments into queued sends, honoring
  quiet hours and the per-business daily send cap.
* :func:`handle_event` — ``contact_added`` enrollment plus
  opened/clicked/converted stamping via ``provider_message_id``.

Conventions:

* One SQLAlchemy session per job invocation, committed on success and
  rolled back on failure. ``campaign_tick`` additionally commits per
  campaign so one bad campaign cannot poison the whole tick.
* ``DATABASE_URL`` / ``REDIS_URL`` problems are logged loudly and
  re-raised — never swallowed.
* Job functions are async and take the arq ``ctx`` dict as the first
  argument, so they can also be invoked directly in tests with a fake
  ``ctx`` (e.g. ``{"job_try": 1}``).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import uuid
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import jinja2
from arq import Retry
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from forge_channels import (
    SendRequest,
    get_email_provider,
    get_sms_provider,
    get_social_provider,
)
from forge_db.models import (
    ALLOWED_ASSET_TRANSITIONS,
    Asset,
    AssetKind,
    AssetStatus,
    AutopilotSettings,
    BrandKit,
    Business,
    Campaign,
    CampaignEnrollment,
    CampaignStep,
    CampaignStatus,
    Channel,
    Contact,
    EnrollmentStatus,
    Event,
    EventKind,
    GenerationLog,
    Send,
    SendStatus,
    Template,
    assert_asset_transition,
)
from forge_db.session import SessionLocal
from forge_llm import (
    GenerationRequest,
    build_brand_system_prompt,
    check_guardrails,
    default_registry,
    get_provider,
)

from worker.timeutil import (
    add_delay_hours,
    as_naive_utc,
    ensure_aware,
    in_quiet_hours,
    next_send_time,
    start_of_day,
    to_campaign_tz,
    utcnow,
)

__all__ = [
    "generate_asset",
    "send_message",
    "campaign_tick",
    "handle_event",
]

log = logging.getLogger("forgeos.worker")

#: arq ctx key arq itself populates; present in real runs, faked in tests.
_JOB_TRY = "job_try"

#: Max provider attempts for a single send before the failure is final.
_MAX_SEND_TRIES = 5

#: Prompt-registry template per asset kind. Kinds without a dedicated
#: marketing template fall back to ``email_body`` (long-form copy).
KIND_TO_PROMPT_TEMPLATE: dict[AssetKind, str] = {
    AssetKind.email_copy: "email_body",
    AssetKind.sms: "sms_body",
    AssetKind.social_post: "social_post",
    AssetKind.blog: "email_body",
    AssetKind.ad: "email_body",
    AssetKind.image_prompt: "social_post",
}

#: Asset kind -> channel label used by ``check_guardrails`` (only "email"
#: triggers the unsubscribe-hint check).
KIND_TO_CHANNEL: dict[AssetKind, str] = {
    AssetKind.email_copy: "email",
    AssetKind.sms: "sms",
    AssetKind.social_post: "social",
    AssetKind.blog: "email",
    AssetKind.ad: "email",
    AssetKind.image_prompt: "social",
}

#: Jinja2 env for send-time rendering: missing variables fail loudly.
_STRICT_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined)


# ---------------------------------------------------------------------------
# Session / config helpers
# ---------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — the worker cannot reach Postgres. "
            "Set DATABASE_URL (e.g. postgresql+psycopg2://forge:forge@postgres:5432/forge)."
        )
    return url


def _new_session() -> Session:
    """Open a DB session, logging connectivity problems loudly."""
    try:
        return SessionLocal(_database_url())()
    except OperationalError:
        log.exception(
            "Postgres is unreachable (DATABASE_URL=%r). "
            "Check that postgres is running and the URL/credentials are correct.",
            os.environ.get("DATABASE_URL", ""),
        )
        raise
    except Exception:
        log.exception("Could not open a database session")
        raise


@contextlib.contextmanager
def _job_session(job_name: str) -> Iterator[Session]:
    """Yield a session; commit on success, roll back + loudly log on error."""
    db = _new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        log.exception("job %s failed; DB transaction rolled back", job_name)
        raise
    finally:
        db.close()


def _aware(value: datetime) -> datetime:
    """``ensure_aware`` for values the caller knows are not None."""
    out = ensure_aware(value)
    assert out is not None
    return out


def _as_uuid(raw: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {what} id {raw!r}: not a UUID") from exc


def _channel_value(send: Send) -> str:
    channel = send.channel
    return channel.value if isinstance(channel, Channel) else str(channel)


# ---------------------------------------------------------------------------
# generate_asset
# ---------------------------------------------------------------------------


def _default_prompt_vars(asset: Asset, business: Business | None) -> dict[str, Any]:
    """Fallback variables so PromptRegistry rendering never fails on a
    sparse ``asset.variables`` dict. Caller-supplied variables win."""
    brand_name = business.name if business and business.name else "Forge"
    return {
        "brand_name": brand_name,
        "first_name": "there",
        "headline": asset.title,
        "intro": asset.title,
        "offer_details": asset.title,
        "cta_text": "Learn more",
        "cta_url": "",
        "unsubscribe_url": "",
        "sign_off": "Best regards",
        "message": asset.title,
        "hook": asset.title,
        "body": asset.title,
        "hashtags": "",
        "short_url": "",
    }


def _transition_asset(asset: Asset, target: AssetStatus) -> None:
    """Move ``asset`` to ``target`` through legal intermediate states.

    The transition table (``forge_db.models.ALLOWED_ASSET_TRANSITIONS``) has
    no direct ``draft -> approved`` edge, so autopilot approval walks
    ``draft -> in_review -> approved`` inside this one job — same end state
    the approval workflow describes, every step asserted.
    """
    if asset.status == target:
        return
    # BFS for a legal path from current -> target over the transition table.
    prev: dict[AssetStatus, AssetStatus | None] = {asset.status: None}
    queue: deque[AssetStatus] = deque([asset.status])
    while queue:
        current = queue.popleft()
        for src, dst in ALLOWED_ASSET_TRANSITIONS:
            if src == current and dst not in prev:
                prev[dst] = current
                queue.append(dst)
    if target not in prev:
        raise ValueError(
            f"No legal asset transition path: {asset.status.value} -> {target.value}"
        )
    path: list[AssetStatus] = []
    node: AssetStatus | None = target
    while node is not None and node != asset.status:
        path.append(node)
        node = prev[node]
    for step in reversed(path):
        assert_asset_transition(asset.status, step)
        asset.status = step


async def generate_asset(ctx: dict, asset_id: str) -> dict[str, Any]:
    """Generate copy for a draft asset via the configured LLM provider."""
    log.info("generate_asset start asset_id=%s", asset_id)
    with _job_session("generate_asset") as db:
        asset = db.get(Asset, _as_uuid(asset_id, "asset"))
        if asset is None:
            log.error("generate_asset: asset %s not found", asset_id)
            return {"ok": False, "error": f"asset {asset_id} not found"}

        # Idempotency: a previous run already produced copy for this asset.
        if asset.status in (AssetStatus.approved, AssetStatus.in_review) and (
            asset.body or ""
        ).strip():
            log.info(
                "generate_asset: asset %s already %s with body; skipping",
                asset_id,
                asset.status.value,
            )
            return {
                "ok": True,
                "skipped": True,
                "asset_id": asset_id,
                "status": asset.status.value,
            }

        brand_kit: BrandKit | None = (
            db.query(BrandKit)
            .filter(BrandKit.business_id == asset.business_id)
            .order_by(BrandKit.version.desc(), BrandKit.created_at.desc())
            .first()
        )
        business = db.get(Business, asset.business_id)
        autopilot = db.get(AutopilotSettings, asset.business_id)

        brand: dict[str, Any] = {
            "voice_description": brand_kit.voice_description if brand_kit else "",
            "tone_tags": list(brand_kit.tone_tags) if brand_kit else [],
            "icp_description": brand_kit.icp_description if brand_kit else "",
            "do_list": list(brand_kit.do_list) if brand_kit else [],
            "dont_list": list(brand_kit.dont_list) if brand_kit else [],
            "channel": KIND_TO_CHANNEL.get(asset.kind, ""),
        }

        template_name = KIND_TO_PROMPT_TEMPLATE.get(asset.kind, "email_body")
        merged_vars: dict[str, Any] = {
            **_default_prompt_vars(asset, business),
            **(asset.variables or {}),
        }
        prompt_text = default_registry.render(template_name, merged_vars)
        extra_direction = (asset.variables or {}).get("prompt")
        if extra_direction:
            prompt_text += f"\n\nAdditional direction from the requester:\n{extra_direction}"

        system_prompt = build_brand_system_prompt(brand)
        provider = get_provider()
        result = await provider.generate(
            GenerationRequest(
                prompt=prompt_text,
                system_prompt=system_prompt,
                max_tokens=800,
                temperature=0.7,
                variables=merged_vars,
            )
        )

        violations = check_guardrails(result.text, brand)
        if violations:
            # Recorded, not fatal: the copy still ships to review/approval.
            # (generation_logs has no metadata column in the v1 data model,
            # so violations ride along in the job result + logs; the api
            # workstream can add a meta jsonb column if it wants them stored.)
            log.warning(
                "generate_asset: %d guardrail violation(s) for asset %s: %s",
                len(violations),
                asset_id,
                "; ".join(violations),
            )

        asset.body = result.text
        asset.tokens_in = result.tokens_in
        asset.tokens_out = result.tokens_out
        asset.cost_usd = Decimal(str(result.cost_usd))
        asset.llm_provider = result.provider
        asset.llm_model = result.model
        if brand_kit is not None:
            asset.brand_kit_version = brand_kit.version

        target = (
            AssetStatus.approved
            if (autopilot is not None and autopilot.auto_approve)
            else AssetStatus.in_review
        )
        _transition_asset(asset, target)

        prompt_hash = hashlib.sha256(
            f"{system_prompt}\n{prompt_text}".encode("utf-8")
        ).hexdigest()
        db.add(
            GenerationLog(
                business_id=asset.business_id,
                asset_id=asset.id,
                provider=result.provider,
                model=result.model,
                prompt_hash=prompt_hash,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=Decimal(str(result.cost_usd)),
                latency_ms=result.latency_ms,
            )
        )
        db.flush()

        log.info(
            "generate_asset done asset_id=%s status=%s provider=%s/%s cost_usd=%s",
            asset_id,
            asset.status.value,
            result.provider,
            result.model,
            result.cost_usd,
        )
        return {
            "ok": True,
            "asset_id": asset_id,
            "status": asset.status.value,
            "provider": result.provider,
            "model": result.model,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": float(result.cost_usd),
            "guardrail_violations": violations,
        }


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


def _fail_send(db: Session, send: Send, error: str) -> None:
    send.status = SendStatus.failed
    send.error = error
    log.warning("send %s marked failed: %s", send.id, error)


def _contact_vars(contact: Contact) -> dict[str, Any]:
    return {
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "email": contact.email or "",
        "phone": contact.phone or "",
        **(contact.custom_fields or {}),
    }


def _render_strict(source: str, variables: dict[str, Any], label: str) -> str:
    """Render Jinja2 with StrictUndefined; missing vars raise UndefinedError."""
    try:
        return _STRICT_ENV.from_string(source).render(variables)
    except jinja2.UndefinedError as exc:
        raise jinja2.UndefinedError(
            f"{label}: missing variable — {exc}"
        ) from exc


def _resolve_send_content(
    db: Session, send: Send, contact: Contact
) -> tuple[str | None, str]:
    """Resolve (subject, body) for a send.

    Asset sends render the approved asset body over contact variables;
    template sends render the step's template; otherwise the send's own
    stored subject/body are used. Raises ValueError on missing/unapproved
    content and jinja2.UndefinedError on missing variables.
    """
    variables = _contact_vars(contact)

    if send.asset_id is not None:
        asset = db.get(Asset, send.asset_id)
        if asset is None:
            raise ValueError(f"asset {send.asset_id} not found")
        if asset.status != AssetStatus.approved:
            raise ValueError(
                f"asset {asset.id} is not approved "
                f"(status={asset.status.value}); only approved assets may be sent"
            )
        body = _render_strict(asset.body or "", variables, f"asset {asset.id}")
        return send.subject, body

    step: CampaignStep | None = (
        db.get(CampaignStep, send.step_id) if send.step_id else None
    )
    template: Template | None = (
        db.get(Template, step.template_id)
        if step is not None and step.template_id
        else None
    )
    if template is not None:
        subject = (
            _render_strict(
                template.subject_template or "", variables, f"template {template.id}"
            )
            if template.subject_template
            else None
        )
        body = _render_strict(
            template.body_template, variables, f"template {template.id}"
        )
        return subject, body

    if (send.body or "").strip():
        return send.subject, send.body
    raise ValueError(
        "send has no asset_id, no step template, and no stored body — nothing to send"
    )


def _is_transient(exc: BaseException) -> bool:
    """True for provider errors worth retrying with backoff."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
    return False


def _retry_delay(job_try: int) -> timedelta:
    """Exponential backoff: 60s, 120s, 240s, 480s — capped at one hour."""
    return timedelta(seconds=min(60 * (2 ** max(job_try - 1, 0)), 3600))


async def send_message(ctx: dict, send_id: str) -> dict[str, Any]:
    """Send one queued message through the configured channel provider."""
    job_try = int(ctx.get(_JOB_TRY, 1) or 1)
    log.info("send_message start send_id=%s try=%d", send_id, job_try)
    db = _new_session()
    try:
        send = db.get(Send, _as_uuid(send_id, "send"))
        if send is None:
            log.error("send_message: send %s not found", send_id)
            return {"ok": False, "error": f"send {send_id} not found"}

        if send.status in (SendStatus.sent, SendStatus.delivered):
            log.info("send_message: send %s already %s; skipping", send_id, send.status.value)
            return {"ok": True, "skipped": True, "send_id": send_id}

        contact = db.get(Contact, send.contact_id)
        if contact is None:
            _fail_send(db, send, "contact not found")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": "contact not found"}

        # Gating: unsubscribed blocks everything; email/sms need consent.
        channel = _channel_value(send)
        if contact.unsubscribed:
            _fail_send(db, send, "unsubscribed: contact opted out of all messaging")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": "unsubscribed"}
        if channel == Channel.email.value and not contact.consent_email:
            _fail_send(db, send, "no consent: contact has not granted email consent")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": "no consent (email)"}
        if channel == Channel.sms.value and not contact.consent_sms:
            _fail_send(db, send, "no consent: contact has not granted sms consent")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": "no consent (sms)"}
        if channel == Channel.email.value and not (contact.email or "").strip():
            _fail_send(db, send, "contact has no email address")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": "no email address"}
        if channel == Channel.sms.value and not (contact.phone or "").strip():
            _fail_send(db, send, "contact has no phone number")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": "no phone number"}

        try:
            subject, body = _resolve_send_content(db, send, contact)
        except (jinja2.UndefinedError, ValueError) as exc:
            _fail_send(db, send, f"content error: {exc}")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": str(exc)}

        if channel == Channel.email.value:
            provider = get_email_provider()
        elif channel == Channel.sms.value:
            provider = get_sms_provider()
        elif channel == Channel.social.value:
            network = str((contact.custom_fields or {}).get("network", "meta"))
            provider = get_social_provider(network)
        else:
            _fail_send(db, send, f"unknown channel {channel!r}")
            db.commit()
            return {"ok": False, "send_id": send_id, "error": f"unknown channel {channel!r}"}

        # Stub providers record into dev_outbox via this metadata.
        request = SendRequest(
            channel=channel,
            to=send.to_address,
            subject=subject,
            body=body,
            metadata={
                "db": db,
                "business_id": send.business_id,
                "send_id": str(send.id),
            },
        )
        send.status = SendStatus.sending
        db.flush()
        try:
            result = await provider.send(request)
        except Exception as exc:
            _fail_send(db, send, f"{type(exc).__name__}: {exc}")
            db.commit()
            if _is_transient(exc) and job_try < _MAX_SEND_TRIES:
                delay = _retry_delay(job_try)
                log.warning(
                    "send_message: transient provider error on send %s "
                    "(try %d/%d); retrying in %s",
                    send_id,
                    job_try,
                    _MAX_SEND_TRIES,
                    delay,
                )
                raise Retry(defer=delay) from exc
            log.error(
                "send_message: send %s failed permanently after try %d: %s",
                send_id,
                job_try,
                exc,
            )
            return {"ok": False, "send_id": send_id, "error": f"{type(exc).__name__}: {exc}"}

        send.status = SendStatus.sent
        send.provider_message_id = result.provider_message_id
        send.sent_at = utcnow()
        send.subject = subject
        send.body = body
        send.error = None
        db.commit()
        log.info(
            "send_message: send %s sent via %s (provider_message_id=%s)",
            send_id,
            provider.name,
            result.provider_message_id,
        )
        return {
            "ok": True,
            "send_id": send_id,
            "provider": provider.name,
            "provider_message_id": result.provider_message_id,
        }
    except Exception:
        db.rollback()
        log.exception("send_message: unexpected failure for send %s", send_id)
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# campaign_tick
# ---------------------------------------------------------------------------


def _count_todays_sends(db: Session, business_id: uuid.UUID, day_start: Any) -> int:
    """Sends created since ``day_start`` (naive UTC) that count against the
    daily cap. Queued sends count too: they were admitted by this tick and
    will convert to sent within seconds."""
    return (
        db.query(func.count(Send.id))
        .filter(
            Send.business_id == business_id,
            Send.created_at >= day_start,
            Send.status.in_(
                [
                    SendStatus.queued,
                    SendStatus.sending,
                    SendStatus.sent,
                    SendStatus.delivered,
                ]
            ),
        )
        .scalar()
        or 0
    )


def _bulk_enroll(db: Session, campaign: Campaign, now: Any) -> int:
    """Enroll every non-unsubscribed contact not already enrolled.

    Used when step 0 has no trigger_event and the campaign start passed.
    Returns the number of new enrollments.
    """
    enrolled_ids = {
        row[0]
        for row in db.query(CampaignEnrollment.contact_id)
        .filter(CampaignEnrollment.campaign_id == campaign.id)
        .all()
    }
    query = db.query(Contact).filter(
        Contact.business_id == campaign.business_id,
        Contact.unsubscribed.is_(False),
    )
    if enrolled_ids:
        query = query.filter(~Contact.id.in_(enrolled_ids))
    new_rows = [
        CampaignEnrollment(
            campaign_id=campaign.id,
            contact_id=contact.id,
            current_step=0,
            status=EnrollmentStatus.active,
            next_run_at=ensure_aware(campaign.starts_at),
        )
        for contact in query.all()
    ]
    db.add_all(new_rows)
    if new_rows:
        log.info(
            "campaign_tick: bulk-enrolled %d contact(s) into campaign %s",
            len(new_rows),
            campaign.id,
        )
    return len(new_rows)


def _create_send(
    db: Session, campaign: Campaign, step: CampaignStep, enrollment: CampaignEnrollment
) -> Send:
    """Create the queued ``sends`` row for one enrollment step.

    Subject/body are rendered later by ``send_message`` (StrictUndefined),
    so this stores empty placeholders plus the template/asset references
    carried by the step.
    """
    contact = db.get(Contact, enrollment.contact_id)
    if contact is None:
        raise ValueError(f"contact {enrollment.contact_id} for enrollment vanished")
    channel = step.channel.value if isinstance(step.channel, Channel) else str(step.channel)
    if channel == Channel.email.value:
        to_address = contact.email or ""
    elif channel == Channel.sms.value:
        to_address = contact.phone or ""
    else:  # social: handle from custom fields, else fall back to email/phone
        to_address = (
            str((contact.custom_fields or {}).get("handle") or "")
            or contact.email
            or contact.phone
            or ""
        )
    send = Send(
        business_id=campaign.business_id,
        campaign_id=campaign.id,
        step_id=step.id,
        contact_id=contact.id,
        channel=step.channel,
        asset_id=step.asset_id,
        to_address=to_address,
        subject="",
        body="",
        status=SendStatus.queued,
        meta={"campaign_step_position": step.position},
    )
    db.add(send)
    db.flush()  # assign id so it can be enqueued below
    return send


async def _tick_campaign(
    db: Session,
    campaign: Campaign,
    now: datetime,
    redis: Any,
    summary: dict[str, int],
) -> None:
    """Run one campaign's enrollment + advancement for this tick."""
    steps = sorted(campaign.steps, key=lambda s: s.position)
    if not steps:
        log.warning("campaign_tick: campaign %s is running with no steps; skipping", campaign.id)
        return

    settings = db.get(AutopilotSettings, campaign.business_id)
    quiet_start = settings.quiet_hours_start if settings else 22
    quiet_end = settings.quiet_hours_end if settings else 8
    daily_cap = settings.daily_send_cap if settings else 500
    local_now = to_campaign_tz(now, campaign.timezone)

    # (a) Bulk enrollment: no trigger on step 0 and the start time passed.
    first_step = steps[0]
    starts_at = ensure_aware(campaign.starts_at)
    if not first_step.trigger_event and starts_at is not None and starts_at <= _aware(now):
        summary["enrolled"] += _bulk_enroll(db, campaign, now)
        # Sessions are created with autoflush=False: make the new enrollments
        # visible to the due-query below in this same tick.
        db.flush()

    # (b) Advance due enrollments.
    due = (
        db.query(CampaignEnrollment)
        .filter(
            CampaignEnrollment.campaign_id == campaign.id,
            CampaignEnrollment.status == EnrollmentStatus.active,
            CampaignEnrollment.next_run_at.isnot(None),
            CampaignEnrollment.next_run_at <= as_naive_utc(now),
        )
        .all()
    )
    for enrollment in due:
        if enrollment.current_step >= len(steps):
            enrollment.status = EnrollmentStatus.completed
            enrollment.next_run_at = None
            continue

        # Quiet hours: push to the end of the window, skip this tick.
        if in_quiet_hours(local_now, quiet_start, quiet_end):
            pushed = next_send_time(local_now, quiet_start, quiet_end)
            enrollment.next_run_at = pushed.astimezone(timezone.utc)
            summary["skipped_quiet"] += 1
            log.debug(
                "campaign_tick: enrollment %s in quiet hours; next_run_at -> %s",
                enrollment.id,
                enrollment.next_run_at,
            )
            continue

        # Daily cap: skip the rest of this campaign for this tick.
        day_start_utc = as_naive_utc(start_of_day(local_now))
        if _count_todays_sends(db, campaign.business_id, day_start_utc) >= daily_cap:
            summary["skipped_cap"] += 1
            log.warning(
                "campaign_tick: business %s hit daily send cap %d; "
                "skipping campaign %s for this tick",
                campaign.business_id,
                daily_cap,
                campaign.id,
            )
            break

        step = steps[enrollment.current_step]
        send = _create_send(db, campaign, step, enrollment)
        enrollment.current_step += 1
        if enrollment.current_step >= len(steps):
            enrollment.status = EnrollmentStatus.completed
            enrollment.next_run_at = None
        else:
            # The delay belongs to the *next* step (drip spacing).
            next_step = steps[enrollment.current_step]
            enrollment.next_run_at = add_delay_hours(
                _aware(now), next_step.delay_hours
            )
        summary["sent"] += 1

        if redis is not None:
            try:
                await redis.enqueue_job("send_message", str(send.id))
            except Exception:
                log.exception(
                    "campaign_tick: Redis enqueue failed for send %s "
                    "(send stays queued; will be picked up next tick)",
                    send.id,
                )
                summary["enqueue_errors"] += 1
        else:
            log.debug(
                "campaign_tick: no redis in ctx; send %s left queued", send.id
            )


async def campaign_tick(ctx: dict) -> dict[str, Any]:
    """Every-60s cron: enroll + advance all running campaigns."""
    now = utcnow()
    summary: dict[str, int] = {
        "campaigns": 0,
        "enrolled": 0,
        "sent": 0,
        "skipped_quiet": 0,
        "skipped_cap": 0,
        "enqueue_errors": 0,
    }
    redis = ctx.get("redis")  # arq pool in real runs; None in unit tests
    try:
        db = _new_session()
    except Exception:
        log.error("campaign_tick: database unavailable; tick aborted (no silent pass)")
        return {"ok": False, "error": "database unavailable", **summary}

    try:
        campaigns = (
            db.query(Campaign)
            .filter(Campaign.status == CampaignStatus.running)
            .all()
        )
        for campaign in campaigns:
            summary["campaigns"] += 1
            try:
                await _tick_campaign(db, campaign, now, redis, summary)
                db.commit()
            except Exception:
                db.rollback()
                log.exception(
                    "campaign_tick: campaign %s failed; rolled back (other campaigns unaffected)",
                    campaign.id,
                )
        log.info("campaign_tick done: %s", summary)
        return {"ok": True, **summary}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# handle_event
# ---------------------------------------------------------------------------


def _handle_contact_added(db: Session, event: Event) -> dict[str, Any]:
    if event.contact_id is None:
        log.warning("handle_event: contact_added with no contact_id; ignoring")
        return {"ok": True, "ignored": True, "reason": "no contact_id"}
    contact = db.get(Contact, event.contact_id)
    if contact is None:
        log.warning("handle_event: contact_added for unknown contact %s", event.contact_id)
        return {"ok": True, "ignored": True, "reason": "unknown contact"}

    now = utcnow()
    enrolled = 0
    campaigns = (
        db.query(Campaign)
        .filter(
            Campaign.business_id == event.business_id,
            Campaign.status == CampaignStatus.running,
        )
        .all()
    )
    for campaign in campaigns:
        steps = sorted(campaign.steps, key=lambda s: s.position)
        if not steps or steps[0].trigger_event != EventKind.contact_added.value:
            continue
        already = (
            db.query(CampaignEnrollment.id)
            .filter(
                CampaignEnrollment.campaign_id == campaign.id,
                CampaignEnrollment.contact_id == contact.id,
            )
            .first()
        )
        if already:
            continue
        db.add(
            CampaignEnrollment(
                campaign_id=campaign.id,
                contact_id=contact.id,
                current_step=0,
                status=EnrollmentStatus.active,
                next_run_at=now,
            )
        )
        enrolled += 1
        log.info(
            "handle_event: enrolled contact %s into campaign %s (contact_added trigger)",
            contact.id,
            campaign.id,
        )
    return {"ok": True, "enrolled": enrolled}


def _handle_engagement(db: Session, event: Event) -> dict[str, Any]:
    payload = event.payload or {}
    provider_message_id = payload.get("provider_message_id")
    if not provider_message_id:
        log.warning(
            "handle_event: %s event without provider_message_id in payload; ignoring",
            event.kind,
        )
        return {"ok": True, "ignored": True, "reason": "no provider_message_id"}
    send = (
        db.query(Send)
        .filter(
            Send.business_id == event.business_id,
            Send.provider_message_id == str(provider_message_id),
        )
        .first()
    )
    if send is None:
        log.warning(
            "handle_event: %s for unknown provider_message_id %r",
            event.kind,
            provider_message_id,
        )
        return {"ok": True, "ignored": True, "reason": "unknown provider_message_id"}

    stamp = ensure_aware(event.created_at) or utcnow()
    if event.kind == EventKind.email_opened.value:
        send.opened_at = stamp
    elif event.kind == EventKind.email_clicked.value:
        send.clicked_at = stamp
    elif event.kind == "converted":
        send.converted_at = stamp
    log.info(
        "handle_event: stamped %s on send %s (provider_message_id=%s)",
        event.kind,
        send.id,
        provider_message_id,
    )
    return {"ok": True, "send_id": str(send.id), "kind": event.kind}


async def handle_event(ctx: dict, event_id: str) -> dict[str, Any]:
    """Process one row from the ``events`` table."""
    log.info("handle_event start event_id=%s", event_id)
    with _job_session("handle_event") as db:
        event = db.get(Event, _as_uuid(event_id, "event"))
        if event is None:
            log.error("handle_event: event %s not found", event_id)
            return {"ok": False, "error": f"event {event_id} not found"}

        kind = event.kind
        if kind == EventKind.contact_added.value:
            return _handle_contact_added(db, event)
        if kind in (
            EventKind.email_opened.value,
            EventKind.email_clicked.value,
            "converted",
        ):
            return _handle_engagement(db, event)
        log.info("handle_event: unknown event kind %r; logged and ignored", kind)
        return {"ok": True, "ignored": True, "kind": kind}
