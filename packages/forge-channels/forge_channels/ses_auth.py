"""Pure-python AWS Signature Version 4 signing for the SESv2 HTTPS API.

Implements just enough of SigV4 to sign ``POST .../v2/email/outbound-emails``
requests: canonical request, string-to-sign, derived signing key, and the
``Authorization`` header. No third-party dependencies (stdlib ``hashlib`` /
``hmac`` only), ~50 lines, and fully deterministic when ``amz_date`` is given,
which makes it unit-testable without network access.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlparse


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_sesv2_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "ses",
    amz_date: str | None = None,
) -> dict[str, str]:
    """Return ``headers`` plus SigV4 ``x-amz-date`` and ``authorization``.

    Args:
        method: HTTP method, e.g. ``"POST"``.
        url: full request URL (query string, if any, is signed as-is).
        headers: request headers; ``content-type`` defaults to
            ``application/json`` when absent. ``host`` and ``x-amz-date`` are
            (re)set by the signer.
        payload: raw request body bytes.
        access_key / secret_key: AWS credentials.
        region: AWS region, e.g. ``"us-east-1"``.
        service: SigV4 service name (``"ses"``).
        amz_date: explicit ``YYYYMMDDTHHMMSSZ`` timestamp (defaults to now, UTC).
            Pass a fixed value in tests for determinism.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"
    canonical_query = parsed.query or ""

    amz_date = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]
    payload_hash = hashlib.sha256(payload).hexdigest()

    signed: dict[str, str] = {k.lower(): v.strip() for k, v in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed.setdefault("content-type", "application/json")

    signed_header_names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in signed_header_names)
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            ";".join(signed_header_names),
            payload_hash,
        ]
    )

    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    key_date = _hmac(b"AWS4" + secret_key.encode("utf-8"), datestamp)
    key_region = _hmac(key_date, region)
    key_service = _hmac(key_region, service)
    signing_key = _hmac(key_service, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    signed["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={';'.join(signed_header_names)}, "
        f"Signature={signature}"
    )
    return signed
