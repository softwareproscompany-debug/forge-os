"""Viator affiliate service — import products and generate draft ads.

Shared by the Draven/swarm tools (``viator.product_import``,
``viator.generate_ads``) and the REST endpoints. Everything created here
is local and draft-only: affiliate programs/links are internal records,
ad assets are born ``draft`` and flagged for compliance review. Nothing
is published or posted externally.

Idempotency: importing the same Viator product twice reuses the existing
program (matched on the product code stored in ``notes``) instead of
creating a duplicate.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from forge_db.models import (
    AffiliateLink,
    AffiliateProgram,
    Asset,
    AssetKind,
    AssetStatus,
    ComplianceIssue,
)

from app.affiliate.connectors.viator import ViatorConnector, connector_for_business

#: FTC disclosure line appended to every generated ad. The compliance
#: engine still scans the copy and flags anything questionable for review.
FTC_DISCLOSURE = "#ad — I earn a commission if you book through this link."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str, product_code: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]
    base = base or "tour"
    return f"viator-{base}-{product_code.lower()}"[:128]


def _product_code_of(program: AffiliateProgram) -> str | None:
    """Recover the Viator product code from the program's notes marker."""
    notes = program.notes or ""
    m = re.search(r"viator_product_code:([A-Za-z0-9_-]+)", notes)
    return m.group(1) if m else None


def import_products(
    db: Any,
    business_id: uuid.UUID,
    products: list[dict[str, Any]],
    actor_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Persist Viator products as affiliate programs + links.

    Skips products already imported (matched by product code). Returns
    per-product summaries ``{product_code, program_id, link_id, slug,
    imported: bool}``.
    """
    existing: dict[str, AffiliateProgram] = {}
    for p in (
        db.query(AffiliateProgram)
        .filter(
            AffiliateProgram.business_id == business_id,
            AffiliateProgram.network == "viator",
        )
        .all()
    ):
        code = _product_code_of(p)
        if code:
            existing[code] = p

    out: list[dict[str, Any]] = []
    for prod in products:
        code = str(prod.get("productCode") or "").strip()
        if not code:
            continue
        program = existing.get(code)
        if program is None:
            title = (prod.get("title") or "Viator experience").strip()[:255]
            program = AffiliateProgram(
                business_id=business_id,
                name=title,
                network="viator",
                website_url=(prod.get("productUrl") or "")[:1024] or None,
                default_commission_pct=Decimal("8"),
                status="active",
                notes=f"Imported from Viator Partner API. viator_product_code:{code}",
            )
            db.add(program)
            db.flush()  # assign id for the link FK
            existing[code] = program
            imported = True
        else:
            imported = False

        link = (
            db.query(AffiliateLink)
            .filter(
                AffiliateLink.business_id == business_id,
                AffiliateLink.program_id == program.id,
            )
            .first()
        )
        if link is None:
            destination = (prod.get("productUrl") or "").strip()
            if not destination.startswith(("http://", "https://")):
                # Without a valid destination there is nothing trackable to
                # promote — skip the link but keep the program record.
                db.flush()
                out.append(
                    {
                        "product_code": code,
                        "program_id": str(program.id),
                        "link_id": None,
                        "slug": None,
                        "imported": imported,
                    }
                )
                continue
            slug = _slugify(prod.get("title") or "", code)
            # Ensure slug uniqueness within the business.
            suffix = 2
            candidate = slug
            while (
                db.query(AffiliateLink)
                .filter(
                    AffiliateLink.business_id == business_id,
                    AffiliateLink.slug == candidate,
                )
                .first()
                is not None
            ):
                candidate = f"{slug[:120]}-{suffix}"
                suffix += 1
            link = AffiliateLink(
                business_id=business_id,
                program_id=program.id,
                label=(prod.get("title") or code).strip()[:255],
                slug=candidate,
                destination_url=destination[:2048],
                utm_source="forgeos",
                utm_medium="affiliate",
                utm_campaign="viator",
                is_active=True,
            )
            db.add(link)
            db.flush()
        out.append(
            {
                "product_code": code,
                "program_id": str(program.id),
                "link_id": str(link.id),
                "slug": link.slug,
                "imported": imported,
            }
        )
    db.commit()
    return out


def _build_ad_copy(
    product: dict[str, Any], short_link_hint: str | None
) -> tuple[str, str]:
    """Deterministic draft ad copy from Viator product fields.

    Returns (headline, body). No LLM involved — honest, reviewable, free.
    """
    title = (product.get("title") or "Unforgettable experience").strip()
    desc = (product.get("description") or "").strip()
    snippet = desc[:220] + ("…" if len(desc) > 220 else "")
    price = product.get("price")
    currency = product.get("currency") or "USD"
    rating = product.get("rating")
    reviews = product.get("review_count")

    headline = f"Experience it: {title[:90]}"
    lines = [snippet] if snippet else []
    facts: list[str] = []
    if price is not None:
        facts.append(f"from {price:,.2f} {currency}")
    if rating:
        stars = f"★ {rating:.1f}"
        if reviews:
            stars += f" ({reviews:,} reviews)"
        facts.append(stars)
    if facts:
        lines.append(" · ".join(facts))
    cta = short_link_hint or product.get("productUrl") or ""
    if cta:
        lines.append(f"Book here: {cta}")
    lines.append(FTC_DISCLOSURE)
    return headline, "\n".join(lines)


def generate_ads(
    db: Any,
    business_id: uuid.UUID,
    program_ids: list[uuid.UUID] | None,
    actor_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Create draft ad assets for imported Viator products.

    Each ad is born ``draft`` with ``is_affiliate_content=True`` and is
    scanned by the compliance checker; violations become
    ``compliance_issues`` rows for human review. Never auto-publishes.
    When ``program_ids`` is None, targets Viator programs that have no ad
    asset yet.
    """
    q = db.query(AffiliateProgram).filter(
        AffiliateProgram.business_id == business_id,
        AffiliateProgram.network == "viator",
        AffiliateProgram.status == "active",
    )
    if program_ids:
        q = q.filter(AffiliateProgram.id.in_(program_ids))
    programs = q.all()

    from app.compliance import check_text

    out: list[dict[str, Any]] = []
    for program in programs:
        code = _product_code_of(program)
        # Skip if a draft/in-review ad already exists for this program.
        already = (
            db.query(Asset)
            .filter(
                Asset.business_id == business_id,
                Asset.kind == AssetKind.ad,
                Asset.status.in_([AssetStatus.draft, AssetStatus.in_review]),
                Asset.title.like(f"%{program.name[:40]}%"),
            )
            .first()
        )
        link = (
            db.query(AffiliateLink)
            .filter(
                AffiliateLink.business_id == business_id,
                AffiliateLink.program_id == program.id,
                AffiliateLink.is_active.is_(True),
            )
            .order_by(AffiliateLink.created_at.asc())
            .first()
        )
        short_hint = f"/r/{link.slug}" if link else None
        product = {
            "title": program.name,
            "description": (program.notes or "").replace(
                f"viator_product_code:{code}", ""
            ).strip()
            if code
            else "",
            "productUrl": program.website_url,
        }
        headline, body = _build_ad_copy(product, short_hint)

        asset = Asset(
            business_id=business_id,
            kind=AssetKind.ad,
            title=f"Ad: {program.name[:200]}",
            body=body,
            variables={
                "headline": headline,
                "program_id": str(program.id),
                "link_id": str(link.id) if link else None,
                "source": "viator",
                "viator_product_code": code,
            },
            status=AssetStatus.draft,
            created_by=actor_id,
            is_affiliate_content=True,
        )
        db.add(asset)
        db.flush()

        # Compliance scan — flag for human review, never auto-edit.
        result = check_text(body, content_type="ad", pack_id=None)
        violations = [v.to_dict() for v in result.violations]
        issue_id = None
        if violations:
            worst = "low"
            for v in violations:
                if v.get("severity") == "high":
                    worst = "high"
                    break
                if v.get("severity") == "medium":
                    worst = "medium"
            issue = ComplianceIssue(
                business_id=business_id,
                source="api_check",
                pack_id=result.pack_id,
                content_type="ad",
                subject_type="asset",
                subject_id=asset.id,
                subject_title=asset.title,
                severity=worst,
                violations=violations,
                status="open",
            )
            db.add(issue)
            db.flush()
            issue_id = str(issue.id)

        out.append(
            {
                "asset_id": str(asset.id),
                "program_id": str(program.id),
                "program_name": program.name,
                "headline": headline,
                "status": "draft",
                "compliance_violations": len(violations),
                "compliance_issue_id": issue_id,
            }
        )
        if already:
            # An ad already existed — keep the new draft too (versions are
            # cheap) but note the duplicate so callers can dedupe in UI.
            out[-1]["note"] = "a draft ad already existed for this program"
    db.commit()
    return out


async def live_search(
    db: Any,
    business_id: uuid.UUID,
    settings: Any,
    destination_id: int,
    count: int = 25,
    currency: str = "USD",
) -> list[dict[str, Any]]:
    """Live Viator product search (no DB writes)."""
    connector = connector_for_business(db, business_id, settings)
    return await connector.search_products(destination_id, count, currency)
