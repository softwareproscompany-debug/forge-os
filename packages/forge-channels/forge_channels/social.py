"""Social channel providers: stub (dev outbox), Meta, LinkedIn, X.

All live providers use httpx and raise a clear :class:`ValueError` naming the
missing credential when their tokens are not configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from forge_channels._env import get_env, require_env
from forge_channels._stub import stub_context, stub_message_id
from forge_channels.outbox import DevOutbox

if TYPE_CHECKING:  # pragma: no cover - avoids a runtime circular import
    from forge_channels import SendRequest, SendResult


def _oauth1_authorization_header(
    *,
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Build an OAuth 1.0a ``Authorization`` header (HMAC-SHA1).

    Only the ``oauth_*`` parameters are signed, which is correct for JSON
    request bodies (per RFC 5849 §3.4.1.3.1, entity-body params of non-form
    content types are excluded). ``nonce``/``timestamp`` are injectable so
    tests are deterministic.
    """
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_urlsafe(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    base_params = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}"
        for k, v in sorted(oauth_params.items())
    )
    base_string = "&".join(
        [method.upper(), quote(url, safe=""), quote(base_params, safe="")]
    )
    signing_key = f"{quote(consumer_secret, safe='')}&{quote(token_secret, safe='')}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(
        f'{quote(k, safe="")}="{quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )


class StubSocialProvider:
    """Dev-mode social provider: records to ``dev_outbox`` instead of posting."""

    def __init__(self, network: str = "stub") -> None:
        self.network = network
        self.name = f"stub:social:{network}"

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        db, business_id = stub_context(req)
        DevOutbox.record(
            db,
            business_id,
            channel="social",
            to_address=req.to,
            subject=req.subject,
            body=req.body,
            provider=self.name,
        )
        return SendResult(
            provider_message_id=stub_message_id("social", req.to, req.subject, req.body),
            status="sent",
            raw={"to": req.to, "network": self.network, "via": "dev_outbox"},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("provider_message_id"),
            "contact": payload.get("contact"),
            "raw": dict(payload),
        }


class MetaProvider:
    """Post to a Facebook Page feed via the Graph API."""

    name = "meta"
    _API_VERSION = "v21.0"

    def __init__(
        self,
        page_token: str | None = None,
        page_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.page_token = (
            page_token if page_token is not None else require_env("SOCIAL_META_PAGE_TOKEN")
        )
        resolved_page = page_id if page_id is not None else get_env("SOCIAL_META_PAGE_ID")
        if not resolved_page:
            raise ValueError(
                "No Meta page id configured. Pass page_id=..., set SOCIAL_META_PAGE_ID, "
                "or put 'page_id' in the send request metadata."
            )
        self.page_id = resolved_page
        self._client = client

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        page_id = str(req.metadata.get("page_id") or self.page_id)
        url = f"https://graph.facebook.com/{self._API_VERSION}/{page_id}/feed"
        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_client = self._client is None
        try:
            response = await client.post(
                url, data={"message": req.body, "access_token": self.page_token}
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Meta page post failed ({response.status_code}): {response.text[:500]}"
                ) from exc
            data = response.json()
        finally:
            if close_client:
                await client.aclose()
        post_id = data.get("id") or f"meta-{uuid.uuid4().hex[:16]}"
        return SendResult(
            provider_message_id=str(post_id), status="sent", raw={"page_id": page_id}
        )

    async def parse_webhook(self, payload: dict) -> dict:
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("post_id"),
            "contact": None,
            "raw": dict(payload),
        }


class LinkedInProvider:
    """Publish a text share via LinkedIn's ugcPosts API."""

    name = "linkedin"
    _ENDPOINT = "https://api.linkedin.com/v2/ugcPosts"

    def __init__(
        self,
        access_token: str | None = None,
        author_urn: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = (
            access_token
            if access_token is not None
            else require_env("SOCIAL_LINKEDIN_TOKEN")
        )
        resolved_author = (
            author_urn if author_urn is not None else get_env("SOCIAL_LINKEDIN_AUTHOR_URN")
        )
        if not resolved_author:
            raise ValueError(
                "No LinkedIn author URN configured. Pass author_urn=..., set "
                "SOCIAL_LINKEDIN_AUTHOR_URN, or put 'author_urn' in the send request "
                "metadata (e.g. 'urn:li:person:abc123')."
            )
        self.author_urn = resolved_author
        self._client = client

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        author = str(req.metadata.get("author_urn") or self.author_urn)
        payload = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": req.body},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_client = self._client is None
        try:
            response = await client.post(
                self._ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"LinkedIn ugcPosts failed ({response.status_code}): {response.text[:500]}"
                ) from exc
            data = response.json()
        finally:
            if close_client:
                await client.aclose()
        post_id = (
            data.get("id")
            or response.headers.get("x-restli-id")
            or f"linkedin-{uuid.uuid4().hex[:16]}"
        )
        return SendResult(
            provider_message_id=str(post_id), status="sent", raw={"author": author}
        )

    async def parse_webhook(self, payload: dict) -> dict:
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("post_id"),
            "contact": None,
            "raw": dict(payload),
        }


class XProvider:
    """Post a tweet via the X API v2 ``/tweets`` endpoint (OAuth 1.0a user context)."""

    name = "x"
    _ENDPOINT = "https://api.twitter.com/2/tweets"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_token_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else require_env("SOCIAL_X_API_KEY")
        self.api_secret = (
            api_secret if api_secret is not None else require_env("SOCIAL_X_API_SECRET")
        )
        self.access_token = (
            access_token
            if access_token is not None
            else require_env("SOCIAL_X_ACCESS_TOKEN")
        )
        self.access_token_secret = (
            access_token_secret
            if access_token_secret is not None
            else require_env("SOCIAL_X_ACCESS_TOKEN_SECRET")
        )
        self._client = client

    def _auth_header(self) -> str:
        return _oauth1_authorization_header(
            method="POST",
            url=self._ENDPOINT,
            consumer_key=self.api_key,
            consumer_secret=self.api_secret,
            token=self.access_token,
            token_secret=self.access_token_secret,
        )

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_client = self._client is None
        try:
            response = await client.post(
                self._ENDPOINT,
                headers={
                    "Authorization": self._auth_header(),
                    "Content-Type": "application/json",
                },
                json={"text": req.body},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"X tweet post failed ({response.status_code}): {response.text[:500]}"
                ) from exc
            data = response.json()
        finally:
            if close_client:
                await client.aclose()
        tweet_id = (data.get("data") or {}).get("id") or f"x-{uuid.uuid4().hex[:16]}"
        return SendResult(
            provider_message_id=str(tweet_id), status="sent", raw={"endpoint": self._ENDPOINT}
        )

    async def parse_webhook(self, payload: dict) -> dict:
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("tweet_id"),
            "contact": None,
            "raw": dict(payload),
        }
