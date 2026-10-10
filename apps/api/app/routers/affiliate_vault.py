"""Affiliate network credentials — vault-backed credential management.

Admin-only. One credential set per network per business (v1).

* ``GET /settings/affiliates/catalog`` — static network list with field
  schemas + docs URLs (public within admin; no secrets involved)
* ``GET /settings/affiliates/status`` — per-network configured yes/no,
  masked hints (never values)
* ``POST /settings/affiliates/{network}`` — save a network's fields
  (encrypted at rest via the vault)
* ``POST /settings/affiliates/{network}/test`` — live verification.
  Networks with a safe ping endpoint get a real authenticated call;
  others get strict format validation and an honest
  "saved — verify in the network dashboard" label. Never fakes a pass.
* ``DELETE /settings/affiliates/{network}`` — remove a network's credentials
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.affiliate_networks import (
    AffiliateNetwork,
    get_network,
    list_networks,
    vault_key,
)
from app.core.deps import CurrentSettings, DbSession, require_role
from app.draven_tools import write_audit_row
from app.settings_vault import service as vault
from forge_db.models import User

router = APIRouter(prefix="/settings/affiliates", tags=["settings"])

AdminUser = Annotated[User, Depends(require_role("owner", "admin"))]

_rate: dict[tuple[uuid.UUID, str], list[float]] = {}
_RATE_LIMITS = {"read": (120, 60.0), "write": (30, 60.0), "test": (20, 60.0)}


def _check_rate(business_id: uuid.UUID, bucket: str) -> None:
    limit, window = _RATE_LIMITS[bucket]
    now = time.monotonic()
    key = (business_id, bucket)
    stamps = [s for s in _rate.get(key, []) if s > now - window]
    if len(stamps) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="settings rate limit exceeded — slow down",
        )
    stamps.append(now)
    _rate[key] = stamps


def _cfg_key(settings: CurrentSettings) -> str:
    return settings.DRAVEN_CONFIG_KEY


def _audit(
    db: Session,
    user: User,
    tool: str,
    network_id: str,
    result: dict[str, Any],
    duration_ms: int = 0,
) -> None:
    # raw_input carries only the network id — never field values.
    write_audit_row(
        db, user, tool, "medium", {"network": network_id}, result, duration_ms
    )


def _require_network(network_id: str) -> AffiliateNetwork:
    net = get_network(network_id)
    if net is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown affiliate network: {network_id!r}",
        )
    return net


def _reject_redirect(net: AffiliateNetwork) -> None:
    """Redirect entries have no credentials of their own."""
    if net.redirect_to:
        parent = get_network(net.redirect_to)
        parent_name = parent.name if parent else net.redirect_to
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{net.name} has no separate API — configure "
            f"{parent_name} instead.",
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AffiliateFieldOut(BaseModel):
    name: str
    label: str
    kind: str
    required: bool
    placeholder: str = ""
    help: str = ""


class AffiliateTestOut(BaseModel):
    has_ping: bool
    auth: str | None = None


class AffiliateNetworkOut(BaseModel):
    id: str
    name: str
    fields: list[AffiliateFieldOut]
    docs_url: str
    test: AffiliateTestOut
    notes: str = ""
    verified: bool = True
    redirect_to: str | None = None


class CatalogOut(BaseModel):
    networks: list[AffiliateNetworkOut]


class NetworkFieldStatus(BaseModel):
    name: str
    label: str
    configured: bool
    hint: str | None = None


class NetworkStatusOut(BaseModel):
    id: str
    name: str
    configured: bool  # all required fields present
    fields: list[NetworkFieldStatus]
    last_verified_at: str | None = None
    redirect_to: str | None = None
    redirect_note: str = ""


class StatusOut(BaseModel):
    networks: list[NetworkStatusOut]


class NetworkSaveIn(BaseModel):
    fields: dict[str, str] = Field(min_length=1, max_length=32)


class NetworkSaveOut(BaseModel):
    network: str
    configured: bool
    fields: list[NetworkFieldStatus]


class NetworkTestOut(BaseModel):
    ok: bool
    verified_live: bool  # False = format validation only, check dashboard
    latency_ms: float | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Catalog + status (reads)
# ---------------------------------------------------------------------------


def _network_out(net: AffiliateNetwork) -> AffiliateNetworkOut:
    return AffiliateNetworkOut(
        id=net.id,
        name=net.name,
        fields=[
            AffiliateFieldOut(
                name=f.name,
                label=f.label,
                kind=f.kind,
                required=f.required,
                placeholder=f.placeholder,
                help=f.help,
            )
            for f in net.fields
        ],
        docs_url=net.docs_url,
        test=AffiliateTestOut(
            has_ping=net.test is not None,
            auth=net.test.auth if net.test else None,
        ),
        notes=net.notes,
        verified=net.verified,
        redirect_to=net.redirect_to,
    )


@router.get("/catalog", response_model=CatalogOut)
def get_catalog(admin: AdminUser) -> CatalogOut:
    """Static credential schemas for every supported network."""
    _check_rate(admin.business_id, "read")
    return CatalogOut(networks=[_network_out(n) for n in list_networks()])


def _field_status(
    db: Session,
    business_id: uuid.UUID,
    net: AffiliateNetwork,
    ck: str,
) -> list[NetworkFieldStatus]:
    out: list[NetworkFieldStatus] = []
    for f in net.fields:
        key = vault_key(net.id, f.name)
        st = vault.secret_status(db, business_id, key, ck)
        configured = st["configured"]
        hint = st["hint"]
        # Fall back to the legacy single-key scheme (e.g. Viator's original
        # vault key) so keys saved before the network catalog existed still
        # show as configured.
        if not configured and net.legacy_key and f.name in net.legacy_key:
            try:
                legacy = vault.secret_status(
                    db, business_id, net.legacy_key, ck
                )
                configured = legacy["configured"]
                hint = legacy["hint"]
            except ValueError:
                pass
        out.append(
            NetworkFieldStatus(
                name=f.name,
                label=f.label,
                configured=configured,
                hint=hint,
            )
        )
    return out


@router.get("/status", response_model=StatusOut)
def get_status(
    admin: AdminUser, db: DbSession, settings: CurrentSettings
) -> StatusOut:
    """Per-network credential status — masked hints only, never values."""
    _check_rate(admin.business_id, "read")
    ck = _cfg_key(settings)
    networks: list[NetworkStatusOut] = []
    for net in list_networks():
        fields = _field_status(db, admin.business_id, net, ck)
        required = [f for f in net.fields if f.required]
        configured = all(
            fs.configured
            for fs in fields
            if any(r.name == fs.name for r in required)
        )
        networks.append(
            NetworkStatusOut(
                id=net.id,
                name=net.name,
                configured=configured,
                fields=fields,
                last_verified_at=None,
                redirect_to=net.redirect_to,
                redirect_note=net.notes if net.redirect_to else "",
            )
        )
    db.commit()
    return StatusOut(networks=networks)


# ---------------------------------------------------------------------------
# Save / delete
# ---------------------------------------------------------------------------


@router.post("/{network_id}", response_model=NetworkSaveOut)
def save_network(
    network_id: str,
    payload: NetworkSaveIn,
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> NetworkSaveOut:
    """Save a network's credential fields. Each is encrypted at rest."""
    _check_rate(admin.business_id, "write")
    net = _require_network(network_id)
    _reject_redirect(net)
    ck = _cfg_key(settings)

    # Reject unknown fields — the catalog is the allowlist.
    known = {f.name: f for f in net.fields}
    for name in payload.fields:
        if name not in known:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown field for {net.name}: {name!r}",
            )
    # Require every required field.
    for f in net.fields:
        if f.required and not (payload.fields.get(f.name) or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{net.name}: {f.label} is required",
            )

    for name, value in payload.fields.items():
        f = known[name]
        key = vault_key(net.id, name)
        # Reuse the vault's format validation via a synthesized spec.
        spec = vault.SecretSpec(
            key, "affiliates", f"{net.name} — {f.label}", f.kind, f.help
        )
        try:
            vault.validate_value(spec, value)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{f.label}: {exc}",
            ) from exc
        vault.put_secret(
            db,
            admin.business_id,
            key,
            value,
            actor=f"user:{admin.id}",
            config_key=ck,
        )
    _audit(
        db, admin, "settings.affiliate_save", network_id,
        {"status": "ok", "output": {"network": network_id}},
    )
    db.commit()
    fields = _field_status(db, admin.business_id, net, ck)
    required = [f for f in net.fields if f.required]
    configured = all(
        fs.configured for fs in fields
        if any(r.name == fs.name for r in required)
    )
    return NetworkSaveOut(
        network=network_id, configured=configured, fields=fields
    )


@router.delete("/{network_id}", response_model=NetworkSaveOut)
def delete_network(
    network_id: str,
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> NetworkSaveOut:
    """Remove all of a network's stored credentials."""
    _check_rate(admin.business_id, "write")
    net = _require_network(network_id)
    _reject_redirect(net)
    ck = _cfg_key(settings)
    removed_any = False
    for f in net.fields:
        key = vault_key(net.id, f.name)
        try:
            if vault.delete_secret(
                db, admin.business_id, key,
                actor=f"user:{admin.id}", config_key=ck,
            ):
                removed_any = True
        except ValueError:
            continue
    if not removed_any:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no credentials stored for {net.name}",
        )
    _audit(
        db, admin, "settings.affiliate_delete", network_id,
        {"status": "ok", "output": {"network": network_id}},
    )
    db.commit()
    fields = _field_status(db, admin.business_id, net, ck)
    return NetworkSaveOut(network=network_id, configured=False, fields=fields)


# ---------------------------------------------------------------------------
# Live verification
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live verification — per-network handlers
# ---------------------------------------------------------------------------
# Each handler takes the network's decrypted field values (server-side
# only) and returns (ok, detail). Values are never logged. A handler that
# can't reach the provider reports the failure honestly — never a faked
# pass.


async def _http_ok(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    success_note: str,
    network_name: str,
    **kwargs: Any,
) -> tuple[bool, str]:
    """Single authenticated request; maps status codes honestly."""
    try:
        if method.upper() == "POST":
            r = await client.post(url, **kwargs)
        else:
            r = await client.get(url, **kwargs)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        return True, success_note
    if r.status_code in (401, 403):
        return False, f"invalid credentials ({r.status_code})"
    return False, f"{network_name} error ({r.status_code})"


async def test_viator(v: dict[str, str]) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "POST",
            "https://api.viator.com/partner/products/search",
            "connected — Viator product search reachable",
            "Viator",
            headers={
                "exp-api-key": v["affiliate_api_key"],
                "Accept": "application/json;version=2.0",
                "Accept-Language": "en-US",
                "Content-Type": "application/json",
            },
            json={
                "filtering": {},
                "pagination": {"start": 1, "count": 1},
                "currency": "USD",
            },
        )


async def test_amazon_associates(v: dict[str, str]) -> tuple[bool, str]:
    """PA-API 5.0 SearchItems with AWS Signature Version 4."""
    import hashlib
    import hmac
    from datetime import datetime, timezone

    region = "us-east-1"
    service = "ProductAdvertisingAPI"
    host = "webservices.amazon.com"
    endpoint = f"https://{host}/paapi5/searchitems"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload = (
        '{"PartnerTag":"%s","PartnerType":"Associates",'
        '"Marketplace":"www.amazon.com",'
        '"Keywords":"test","SearchIndex":"All","ItemCount":1}'
    ) % v["partner_tag"]
    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems\n"
    )
    signed_headers = (
        "content-encoding;host;x-amz-date;x-amz-target"
    )
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    canonical_request = (
        f"POST\n/paapi5/searchitems\n\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + v["secret_key"]).encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={v['access_key']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(
                endpoint,
                content=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Encoding": "amz-1.0",
                    "X-Amz-Date": amz_date,
                    "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
                    "Authorization": auth,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        return True, "connected — PA-API credentials accepted"
    if r.status_code in (401, 403):
        return False, f"invalid credentials ({r.status_code})"
    # PA-API returns 400/429 for throttling or account issues — surface it.
    return False, f"Amazon PA-API error ({r.status_code})"


async def test_shareasale(v: dict[str, str]) -> tuple[bool, str]:
    """ShareASale signed request (apitokencount action)."""
    import hashlib
    from email.utils import formatdate

    date = formatdate(timeval=None, localtime=False, usegmt=True)
    action = "apitokencount"
    sig_src = f"{v['api_token']}:{date}:{action}:{v['api_secret_key']}"
    signature = hashlib.sha256(sig_src.encode()).hexdigest()
    url = (
        "https://api.shareasale.com/x.cfm"
        f"?affiliateId={v['affiliate_id']}"
        f"&token={v['api_token']}"
        f"&version={v['api_version']}"
        f"&action={action}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "GET", url,
            "connected — ShareASale API accepted the signature",
            "ShareASale",
            headers={
                "x-ShareASale-Date": date,
                "x-ShareASale-Authentication": signature,
            },
        )


async def test_cj_affiliate(v: dict[str, str]) -> tuple[bool, str]:
    """CJ GraphQL — minimal advertiser lookup."""
    query = (
        "{ advertiserLookup(companyIds: [\"1\"]) { "
        "count advertisers { companyId companyName } } }"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "POST",
            "https://ads.api.cj.com/query",
            "connected — CJ API accepted the token",
            "CJ Affiliate",
            headers={
                "Authorization": f"Bearer {v['personal_access_token']}",
                "Content-Type": "application/json",
            },
            json={"query": query},
        )


async def test_rakuten_advertising(v: dict[str, str]) -> tuple[bool, str]:
    """Rakuten OAuth2 client-credentials token exchange."""
    import base64

    basic = base64.b64encode(
        f"{v['client_id']}:{v['client_secret']}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(
                "https://api.rakutenadvertising.com/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.rakutenadvertising.com",
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        try:
            has_token = bool(r.json().get("access_token"))
        except Exception:
            has_token = False
        if has_token:
            return True, "connected — Rakuten issued an access token"
        return False, "Rakuten token response missing access_token"
    if r.status_code in (401, 403):
        return False, f"invalid credentials ({r.status_code})"
    return False, f"Rakuten error ({r.status_code})"


async def test_impact(v: dict[str, str]) -> tuple[bool, str]:
    """Impact — list campaigns with HTTP Basic."""
    url = (
        f"https://api.impact.com/Mediapartners/{v['account_sid']}/Campaigns"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "GET", url,
            "connected — Impact API accepted the credentials",
            "Impact",
            auth=(v["account_sid"], v["auth_token"]),
        )


async def test_awin(v: dict[str, str]) -> tuple[bool, str]:
    """Awin — accounts endpoint with Bearer token."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "GET",
            "https://api.awin.com/accounts",
            "connected — Awin API accepted the token",
            "Awin",
            headers={"Authorization": f"Bearer {v['access_token']}"},
        )


async def test_clickbank(v: dict[str, str]) -> tuple[bool, str]:
    """ClickBank — orders list with colon-joined key header."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "GET",
            "https://api.clickbank.com/rest/1.3/orders2/list",
            "connected — ClickBank API accepted the keys",
            "ClickBank",
            headers={
                "Authorization": f"{v['developer_key']}:{v['clerk_key']}",
                "Accept": "application/json",
            },
        )


async def test_digistore24(v: dict[str, str]) -> tuple[bool, str]:
    """Digistore24 — lightweight read call with API-key header."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "POST",
            "https://www.digistore24.com/api/call/listTransactions/format/json",
            "connected — Digistore24 API accepted the key",
            "Digistore24",
            headers={"X-DS-API-KEY": v["api_key"]},
            data={"per_page": "1", "page": "1"},
        )


async def test_ebay_partner_network(v: dict[str, str]) -> tuple[bool, str]:
    """eBay — OAuth2 client credentials, then a Browse API search."""
    import base64

    basic = base64.b64encode(
        f"{v['client_id']}:{v['client_secret']}".encode()
    ).decode()
    marketplace = v.get("marketplace_id") or "EBAY_US"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            tr = await client.post(
                "https://api.ebay.com/identity/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return False, f"network error: {type(exc).__name__}"
        if tr.status_code in (401, 403):
            return False, f"invalid eBay credentials ({tr.status_code})"
        if tr.status_code != 200:
            return False, f"eBay token error ({tr.status_code})"
        try:
            token = tr.json().get("access_token")
        except Exception:
            token = None
        if not token:
            return False, "eBay token response missing access_token"
        # Use the token against the Browse API — proves end-to-end access.
        return await _http_ok(
            client, "GET",
            "https://api.ebay.com/buy/browse/v1/item_summary/search"
            "?q=test&limit=1",
            "connected — eBay issued a token and Browse API responded",
            "eBay",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace,
            },
        )


async def test_partnerstack(v: dict[str, str]) -> tuple[bool, str]:
    """PartnerStack — partnerships list with Bearer key."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "GET",
            "https://api.partnerstack.com/api/v2/partnerships",
            "connected — PartnerStack API accepted the key",
            "PartnerStack",
            headers={"Authorization": f"Bearer {v['api_key']}"},
        )


async def test_refersion(v: dict[str, str]) -> tuple[bool, str]:
    """Refersion v2 — affiliate list with dual-key headers."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "POST",
            "https://api.refersion.com/v2/affiliate/list",
            "connected — Refersion API accepted the keys",
            "Refersion",
            headers={
                "Refersion-Public-Key": v["public_key"],
                "Refersion-Secret-Key": v["secret_key"],
                "Content-Type": "application/json",
            },
            json={"limit": 1},
        )


async def test_goaffpro(v: dict[str, str]) -> tuple[bool, str]:
    """GoAffPro — affiliates list with public-token header."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await _http_ok(
            client, "GET",
            "https://api.goaffpro.com/v1/admin/affiliates?limit=1",
            "connected — GoAffPro API accepted the token",
            "GoAffPro",
            headers={"x-goaffpro-public-token": v["public_token"]},
        )


_TEST_HANDLERS: dict[str, Any] = {
    "test_amazon_associates": test_amazon_associates,
    "test_shareasale": test_shareasale,
    "test_cj_affiliate": test_cj_affiliate,
    "test_rakuten_advertising": test_rakuten_advertising,
    "test_impact": test_impact,
    "test_awin": test_awin,
    "test_clickbank": test_clickbank,
    "test_digistore24": test_digistore24,
    "test_ebay_partner_network": test_ebay_partner_network,
    "test_partnerstack": test_partnerstack,
    "test_refersion": test_refersion,
    "test_goaffpro": test_goaffpro,
    "test_viator": test_viator,
}


def _render_template(obj: Any, values: dict[str, str]) -> Any:
    """Substitute {field_name} placeholders in the test request template."""
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("{" + k + "}", v)
        return obj
    if isinstance(obj, dict):
        return {k: _render_template(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_template(v, values) for v in obj]
    return obj


@router.post("/{network_id}/test", response_model=NetworkTestOut)
async def test_network(
    network_id: str,
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> NetworkTestOut:
    """Verify a network's credentials.

    Networks with a safe ping endpoint get a real authenticated call.
    Others get strict format validation and an honest
    "saved — verify in the network dashboard" result. A test that always
    passes would be a lie.
    """
    _check_rate(admin.business_id, "test")
    net = _require_network(network_id)
    _reject_redirect(net)
    ck = _cfg_key(settings)

    # Gather decrypted values (server-side only).
    values: dict[str, str] = {}
    for f in net.fields:
        key = vault_key(net.id, f.name)
        v = vault.get_secret(db, admin.business_id, key, ck)
        # Legacy fallback for single-key networks.
        if not v and net.legacy_key and f.name in net.legacy_key:
            try:
                v = vault.get_secret(
                    db, admin.business_id, net.legacy_key, ck
                )
            except ValueError:
                v = None
        if v:
            values[f.name] = v
    missing = [f.label for f in net.fields if f.required and f.name not in values]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"not configured — missing: {', '.join(missing)}",
        )

    started = time.monotonic()
    if net.test is not None:
        handler = _TEST_HANDLERS.get(net.test.handler)
        if handler is None:  # pragma: no cover — catalog/handler drift
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="verification handler missing for this network",
            )
        ok, detail = await handler(values)
        verified_live = True
    else:
        # No safe ping exists: strict format re-validation, honestly labeled.
        try:
            for f in net.fields:
                if f.name in values:
                    spec = vault.SecretSpec(
                        vault_key(net.id, f.name),
                        "affiliates",
                        f"{net.name} — {f.label}",
                        f.kind,
                        f.help,
                    )
                    vault.validate_value(spec, values[f.name])
        except ValueError as exc:
            ok, detail = False, f"format check failed: {exc}"
        else:
            ok, detail = (
                True,
                "saved — verify in the network dashboard "
                f"({net.docs_url})",
            )
        verified_live = False
    latency_ms = (time.monotonic() - started) * 1000

    _audit(
        db, admin, "settings.affiliate_test", network_id,
        {"status": "ok" if ok else "error",
         "output": {"verified": ok, "live": verified_live,
                    "detail": (detail or "")[:200]}},
        int(latency_ms),
    )
    db.commit()
    return NetworkTestOut(
        ok=ok,
        verified_live=verified_live,
        latency_ms=round(latency_ms, 1),
        detail=detail,
    )
