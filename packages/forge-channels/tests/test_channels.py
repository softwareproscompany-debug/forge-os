"""Unit tests for forge_channels. No network access: httpx MockTransport is used
for every live provider, and SMTP is faked at the smtplib boundary.

Outbox strategy: if the real ``forge_db`` package is importable we try it
(sqlite in-memory); otherwise — e.g. right now, where the api workstream
hasn't built forge-db yet — we install a protocol-level fake ``forge_db``
module exposing a ``DevOutbox`` model and a fake session. Neither path fails
when forge_db is absent.
"""

from __future__ import annotations

import base64
import sys
import types
import urllib.parse
import uuid
from typing import Any

import httpx
import pytest

from forge_channels import (
    ChannelProvider,
    DevOutbox,
    LinkedInProvider,
    MetaProvider,
    SendGridProvider,
    SendRequest,
    SendResult,
    SESProvider,
    SMTPProvider,
    StubEmailProvider,
    StubSMSProvider,
    StubSocialProvider,
    TwilioProvider,
    XProvider,
    get_email_provider,
    get_sms_provider,
    get_social_provider,
)
from forge_channels.ses_auth import sign_sesv2_request
from forge_channels.social import _oauth1_authorization_header


# ---------------------------------------------------------------------------
# forge_db backend resolution (real sqlite if possible, else protocol fake)
# ---------------------------------------------------------------------------


class FakeSession:
    """Protocol-level stand-in for a sync SQLAlchemy session."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0

    def add(self, row: Any) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1


def _install_fake_forge_db() -> types.ModuleType:
    fake = types.ModuleType("forge_db")

    class FakeDevOutbox:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    fake.models = types.SimpleNamespace(DevOutbox=FakeDevOutbox)  # type: ignore[attr-defined]
    sys.modules["forge_db"] = fake
    return fake


# Raw DDL for the real forge_db.dev_outbox table on sqlite, used by the probe
# below. The model declares postgres-isms (gen_random_uuid() server defaults,
# FK to businesses) that sqlite can't compile, so an equivalent table is
# created by hand. NOTE (integration gap for the api workstream): forge_db's
# UUID columns use PG_UUID(as_uuid=True).with_variant(CHAR(32), "sqlite"),
# whose sqlite variant cannot bind uuid.UUID objects — and DevOutbox.record()
# relies on the model's client-side uuid.uuid4 default for `id`, so even
# hex-string business_ids don't save the insert. Until the sqlite variant gets
# proper bind/result processors, the probe fails and tests use the fake.
_DEV_OUTBOX_SQLITE_DDL = """CREATE TABLE dev_outbox (
  id CHAR(32) PRIMARY KEY,
  business_id CHAR(32) NOT NULL,
  channel VARCHAR(32) NOT NULL,
  to_address TEXT NOT NULL,
  subject TEXT,
  body TEXT NOT NULL,
  provider VARCHAR(64) NOT NULL DEFAULT 'stub',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


def _real_sqlite_session_factory() -> Any | None:
    """Return a session factory for the real forge_db DevOutbox on sqlite.

    Returns None when forge_db isn't installed, has no DevOutbox model, or
    can't round-trip on sqlite — in which case tests fall back to the fake.
    """
    try:
        import forge_db
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session

        models = getattr(forge_db, "models", forge_db)
        model = getattr(models, "DevOutbox", None)
        if model is None or not hasattr(model, "__table__"):
            return None
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text(_DEV_OUTBOX_SQLITE_DDL))
        # Probe the *actual* DevOutbox.record() code path end to end. Note this
        # currently fails: forge_db's UUID columns use
        # PG_UUID(as_uuid=True).with_variant(CHAR(32), "sqlite"), whose sqlite
        # variant cannot bind uuid.UUID objects — and record() relies on the
        # model's client-side uuid.uuid4 default for `id`. Until the api
        # workstream gives the sqlite variant proper bind processors, the probe
        # fails here and tests fall back to the protocol-level fake (graceful,
        # per the workstream contract).
        from forge_channels.outbox import DevOutbox as _Outbox

        probe = Session(engine)
        row = _Outbox.record(
            probe,
            uuid.uuid4().hex,
            "email",
            "probe@example.com",
            None,
            "probe",
            provider="stub",
        )
        assert probe.query(model).one().id == row.id
        probe.close()

        def make_session() -> Any:
            return Session(engine)

        return make_session
    except Exception:
        return None


_REAL_SESSION_FACTORY = _real_sqlite_session_factory()
if _REAL_SESSION_FACTORY is None:
    _install_fake_forge_db()

USING_REAL_FORGE_DB = _REAL_SESSION_FACTORY is not None


@pytest.fixture
def db_session() -> Any:
    if _REAL_SESSION_FACTORY is not None:
        session = _REAL_SESSION_FACTORY()
        yield session
        session.close()
    else:
        yield FakeSession()


@pytest.fixture
def business_id() -> Any:
    # Hex strings on sqlite (see NOTE above); arbitrary value for the fake.
    return uuid.uuid4().hex if USING_REAL_FORGE_DB else "biz-123"


def _stub_meta(session: Any, business_id: Any) -> dict:
    return {"db": session, "business_id": business_id}


def _added_rows(session: Any) -> list[Any]:
    if USING_REAL_FORGE_DB:
        import forge_db

        models = getattr(forge_db, "models", forge_db)
        return session.query(models.DevOutbox).order_by(models.DevOutbox.created_at).all()
    return session.added


# ---------------------------------------------------------------------------
# Stub providers -> dev_outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_email_records_outbox(db_session: Any, business_id: Any) -> None:
    provider = StubEmailProvider()
    req = SendRequest(
        channel="email",
        to="user@example.com",
        subject="Hello",
        body="Body text",
        metadata=_stub_meta(db_session, business_id),
    )
    result = await provider.send(req)
    assert result.status == "sent"
    assert result.provider_message_id.startswith("stub-")
    rows = _added_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "email"
    assert row.to_address == "user@example.com"
    assert row.subject == "Hello"
    assert row.body == "Body text"
    assert row.business_id == business_id
    assert row.provider == "stub"


@pytest.mark.asyncio
async def test_stub_sms_records_outbox(db_session: Any, business_id: Any) -> None:
    provider = StubSMSProvider()
    result = await provider.send(
        SendRequest(
            channel="sms",
            to="+15550001111",
            subject=None,
            body="ping",
            metadata=_stub_meta(db_session, business_id),
        )
    )
    assert result.status == "sent"
    row = _added_rows(db_session)[0]
    assert row.channel == "sms"
    assert row.to_address == "+15550001111"
    assert row.subject is None


@pytest.mark.asyncio
async def test_stub_social_records_outbox(db_session: Any, business_id: Any) -> None:
    provider = StubSocialProvider(network="meta")
    result = await provider.send(
        SendRequest(
            channel="social",
            to="@ourpage",
            subject=None,
            body="hello world",
            metadata=_stub_meta(db_session, business_id),
        )
    )
    assert result.status == "sent"
    row = _added_rows(db_session)[0]
    assert row.channel == "social"
    assert "meta" in row.provider


@pytest.mark.asyncio
async def test_stub_message_id_is_deterministic(
    db_session: Any, business_id: Any
) -> None:
    provider = StubEmailProvider()
    meta = _stub_meta(db_session, business_id)
    mk = lambda: SendRequest(  # noqa: E731
        channel="email", to="a@b.c", subject="s", body="b", metadata=meta
    )
    first = await provider.send(mk())
    second = await provider.send(mk())
    assert first.provider_message_id == second.provider_message_id


@pytest.mark.asyncio
async def test_stub_requires_db_metadata() -> None:
    provider = StubEmailProvider()
    with pytest.raises(ValueError, match="metadata\\['db'\\]"):
        await provider.send(
            SendRequest(channel="email", to="a@b.c", subject="s", body="b", metadata={})
        )


def test_record_without_forge_db_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules[name] = None makes `import forge_db` raise ImportError.
    monkeypatch.setitem(sys.modules, "forge_db", None)
    with pytest.raises(RuntimeError, match="forge_db is not installed"):
        DevOutbox.record(FakeSession(), "biz-1", "email", "a@b.c", "s", "b")


def test_outbox_backend_selection() -> None:
    # Guards the fixture contract: whichever backend was selected (real
    # forge_db on sqlite, or the protocol-level fake), the import path that
    # DevOutbox.record() uses must resolve to a DevOutbox model.
    import forge_db

    models = getattr(forge_db, "models", forge_db)
    assert getattr(models, "DevOutbox", None) is not None, (
        "forge_db must expose a DevOutbox model (real or fake)"
    )


# ---------------------------------------------------------------------------
# Provider selection via env
# ---------------------------------------------------------------------------


def test_email_defaults_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHANNEL_MODE", raising=False)
    assert isinstance(get_email_provider(), StubEmailProvider)


def test_sms_defaults_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHANNEL_MODE", raising=False)
    assert isinstance(get_sms_provider(), StubSMSProvider)


def test_social_stub_carries_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHANNEL_MODE", raising=False)
    provider = get_social_provider("x")
    assert isinstance(provider, StubSocialProvider)
    assert provider.network == "x"


def test_get_social_provider_rejects_unknown_network() -> None:
    with pytest.raises(ValueError, match="Unknown social network"):
        get_social_provider("myspace")


def test_email_live_sendgrid_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNEL_MODE", "live")
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test")
    assert isinstance(get_email_provider(), SendGridProvider)


def test_email_live_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNEL_MODE", "live")
    monkeypatch.setenv("EMAIL_PROVIDER", "carrier-pigeon")
    with pytest.raises(ValueError, match="Unknown EMAIL_PROVIDER"):
        get_email_provider()


def test_sms_live_twilio_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNEL_MODE", "live")
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550001111")
    assert isinstance(get_sms_provider(), TwilioProvider)


def test_sms_live_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNEL_MODE", "live")
    monkeypatch.setenv("SMS_PROVIDER", "smoke-signals")
    with pytest.raises(ValueError, match="Unknown SMS_PROVIDER"):
        get_sms_provider()


def test_social_live_meta_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNEL_MODE", "live")
    monkeypatch.setenv("SOCIAL_META_PAGE_TOKEN", "tok")
    monkeypatch.setenv("SOCIAL_META_PAGE_ID", "12345")
    assert isinstance(get_social_provider("meta"), MetaProvider)


# ---------------------------------------------------------------------------
# Twilio (mocked httpx)
# ---------------------------------------------------------------------------


def _mock_client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_twilio_auth_header_formation() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["form"] = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(200, json={"sid": "SM999", "status": "queued"})

    provider = TwilioProvider(
        account_sid="AC123",
        auth_token="tok456",
        from_number="+15550001111",
        client=_mock_client(handler),
    )
    result = await provider.send(
        SendRequest(channel="sms", to="+15550002222", subject=None, body="hi", metadata={})
    )
    assert result.provider_message_id == "SM999"
    assert result.status == "queued"
    expected = "Basic " + base64.b64encode(b"AC123:tok456").decode()
    assert seen["authorization"] == expected
    assert seen["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert seen["form"]["To"] == ["+15550002222"]
    assert seen["form"]["From"] == ["+15550001111"]
    assert seen["form"]["Body"] == ["hi"]


@pytest.mark.asyncio
async def test_twilio_error_surfaces_clearly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authenticate"})

    provider = TwilioProvider(
        account_sid="AC123",
        auth_token="bad",
        from_number="+15550001111",
        client=_mock_client(handler),
    )
    with pytest.raises(RuntimeError, match="Twilio SMS send failed \\(401\\)"):
        await provider.send(
            SendRequest(channel="sms", to="+1", subject=None, body="hi", metadata={})
        )


def test_twilio_missing_credentials() -> None:
    with pytest.raises(ValueError, match="TWILIO_ACCOUNT_SID"):
        TwilioProvider(account_sid=None, auth_token="t", from_number="+1")


@pytest.mark.asyncio
async def test_twilio_parse_webhook() -> None:
    provider = TwilioProvider(
        account_sid="AC1", auth_token="t", from_number="+1", client=_mock_client(lambda r: httpx.Response(200, json={}))
    )
    event = await provider.parse_webhook(
        {"MessageSid": "SM1", "MessageStatus": "delivered", "From": "+1555"}
    )
    assert event == {
        "event": "delivered",
        "provider_message_id": "SM1",
        "contact": "+1555",
        "raw": {"MessageSid": "SM1", "MessageStatus": "delivered", "From": "+1555"},
    }


# ---------------------------------------------------------------------------
# SendGrid (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sendgrid_request_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = __import__("json").loads(request.content.decode())
        return httpx.Response(202, headers={"x-message-id": "sg-abc"})

    provider = SendGridProvider(
        api_key="SG.test", from_address="news@example.com", client=_mock_client(handler)
    )
    result = await provider.send(
        SendRequest(
            channel="email", to="u@example.com", subject="Hi", body="Hello", metadata={}
        )
    )
    assert seen["authorization"] == "Bearer SG.test"
    payload = seen["json"]
    assert payload["personalizations"][0]["to"] == [{"email": "u@example.com"}]
    assert payload["from"] == {"email": "news@example.com"}
    assert payload["subject"] == "Hi"
    assert payload["content"] == [{"type": "text/plain", "value": "Hello"}]
    assert result.provider_message_id == "sg-abc"
    assert result.status == "sent"


def test_sendgrid_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    with pytest.raises(ValueError, match="SENDGRID_API_KEY"):
        SendGridProvider()


# ---------------------------------------------------------------------------
# SMTP (faked smtplib)
# ---------------------------------------------------------------------------


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int = 0) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.sent: list[Any] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: Any) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_smtp_send_uses_starttls_and_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    _FakeSMTP.instances.clear()
    provider = SMTPProvider(
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        from_address="news@example.com",
    )
    result = await provider.send(
        SendRequest(
            channel="email", to="u@example.com", subject="Hi", body="Hello", metadata={}
        )
    )
    assert result.status == "sent"
    conn = _FakeSMTP.instances[-1]
    assert conn.started_tls is True
    assert conn.login_args == ("user", "pass")
    message = conn.sent[0]
    assert message["To"] == "u@example.com"
    assert message["From"] == "news@example.com"
    assert message["Subject"] == "Hi"


def test_smtp_missing_host() -> None:
    with pytest.raises(ValueError, match="SMTP_HOST"):
        SMTPProvider(host="")


# ---------------------------------------------------------------------------
# SES (SigV4 unit tests + mocked httpx)
# ---------------------------------------------------------------------------


def _ses_headers(payload: bytes = b"{}") -> dict[str, str]:
    return sign_sesv2_request(
        method="POST",
        url="https://email.us-east-1.amazonaws.com/v2/email/outbound-emails",
        headers={"Content-Type": "application/json"},
        payload=payload,
        access_key="AKIAEXAMPLE",
        secret_key="secret",
        region="us-east-1",
        amz_date="20250101T000000Z",
    )


def test_ses_sigv4_header_format_and_determinism() -> None:
    first = _ses_headers()
    second = _ses_headers()
    assert first == second  # deterministic for a fixed timestamp
    auth = first["authorization"]
    assert auth.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20250101/us-east-1/ses/aws4_request,"
    )
    assert "SignedHeaders=content-type;host;x-amz-date" in auth
    assert first["x-amz-date"] == "20250101T000000Z"
    assert len(auth.split("Signature=")[1]) == 64


def test_ses_sigv4_signature_covers_payload() -> None:
    assert _ses_headers(b"{}")["authorization"] != _ses_headers(b'{"a":1}')["authorization"]


@pytest.mark.asyncio
async def test_ses_send_signs_request() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["host"] = request.headers.get("host")
        seen["json"] = __import__("json").loads(request.content.decode())
        return httpx.Response(200, json={"MessageId": "mid-1"})

    provider = SESProvider(
        region="us-east-1",
        access_key="AK",
        secret_key="SK",
        from_address="news@example.com",
        client=_mock_client(handler),
    )
    result = await provider.send(
        SendRequest(
            channel="email", to="u@example.com", subject="Hi", body="Hello", metadata={}
        )
    )
    assert result.provider_message_id == "mid-1"
    assert seen["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
    assert "/us-east-1/ses/aws4_request," in seen["authorization"]
    payload = seen["json"]
    assert payload["FromEmailAddress"] == "news@example.com"
    assert payload["Destination"] == {"ToAddresses": ["u@example.com"]}


def test_ses_missing_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SES_REGION", raising=False)
    with pytest.raises(ValueError, match="SES_REGION"):
        SESProvider()


@pytest.mark.asyncio
async def test_ses_parse_webhook_sns_delivery() -> None:
    import json

    provider = SESProvider(
        region="us-east-1",
        access_key="AK",
        secret_key="SK",
        client=_mock_client(lambda r: httpx.Response(200, json={})),
    )
    inner = json.dumps(
        {"eventType": "Delivery", "mail": {"messageId": "mid-9"}}
    )
    event = await provider.parse_webhook({"Type": "Notification", "Message": inner})
    assert event["event"] == "delivered"
    assert event["provider_message_id"] == "mid-9"


# ---------------------------------------------------------------------------
# Social providers (mocked httpx)
# ---------------------------------------------------------------------------


def test_oauth1_header_deterministic_and_wellformed() -> None:
    kwargs = dict(
        method="POST",
        url="https://api.twitter.com/2/tweets",
        consumer_key="ck",
        consumer_secret="cs",
        token="t",
        token_secret="ts",
        nonce="fixed-nonce",
        timestamp="1700000000",
    )
    first = _oauth1_authorization_header(**kwargs)  # type: ignore[arg-type]
    second = _oauth1_authorization_header(**kwargs)  # type: ignore[arg-type]
    assert first == second
    assert first.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in first
    assert 'oauth_nonce="fixed-nonce"' in first
    assert 'oauth_signature_method="HMAC-SHA1"' in first
    assert 'oauth_signature="' in first


def test_oauth1_signature_changes_with_nonce() -> None:
    base = dict(
        method="POST",
        url="https://api.twitter.com/2/tweets",
        consumer_key="ck",
        consumer_secret="cs",
        token="t",
        token_secret="ts",
        timestamp="1700000000",
    )
    a = _oauth1_authorization_header(nonce="n1", **base)  # type: ignore[arg-type]
    b = _oauth1_authorization_header(nonce="n2", **base)  # type: ignore[arg-type]
    assert a != b


@pytest.mark.asyncio
async def test_x_send_uses_oauth1_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = __import__("json").loads(request.content.decode())
        return httpx.Response(201, json={"data": {"id": "12345", "text": "hi"}})

    provider = XProvider(
        api_key="ck",
        api_secret="cs",
        access_token="t",
        access_token_secret="ts",
        client=_mock_client(handler),
    )
    result = await provider.send(
        SendRequest(channel="social", to="@brand", subject=None, body="hi", metadata={})
    )
    assert result.provider_message_id == "12345"
    assert seen["authorization"].startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in seen["authorization"]
    assert seen["json"] == {"text": "hi"}


def test_x_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "SOCIAL_X_API_KEY",
        "SOCIAL_X_API_SECRET",
        "SOCIAL_X_ACCESS_TOKEN",
        "SOCIAL_X_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="SOCIAL_X_API_KEY"):
        XProvider()


@pytest.mark.asyncio
async def test_meta_send_posts_to_page_feed() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(200, json={"id": "123_456"})

    provider = MetaProvider(
        page_token="page-tok", page_id="123", client=_mock_client(handler)
    )
    result = await provider.send(
        SendRequest(channel="social", to="@page", subject=None, body="hello", metadata={})
    )
    assert result.provider_message_id == "123_456"
    assert seen["url"] == "https://graph.facebook.com/v21.0/123/feed"
    assert seen["form"]["message"] == ["hello"]
    assert seen["form"]["access_token"] == ["page-tok"]


def test_meta_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOCIAL_META_PAGE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SOCIAL_META_PAGE_TOKEN"):
        MetaProvider()


@pytest.mark.asyncio
async def test_linkedin_send_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = __import__("json").loads(request.content.decode())
        return httpx.Response(201, json={"id": "urn:li:share:1"})

    provider = LinkedInProvider(
        access_token="li-tok",
        author_urn="urn:li:person:abc",
        client=_mock_client(handler),
    )
    result = await provider.send(
        SendRequest(channel="social", to="@co", subject=None, body="news", metadata={})
    )
    assert result.provider_message_id == "urn:li:share:1"
    assert seen["authorization"] == "Bearer li-tok"
    payload = seen["json"]
    assert payload["author"] == "urn:li:person:abc"
    assert payload["lifecycleState"] == "PUBLISHED"
    assert (
        payload["specificContent"]["com.linkedin.ugc.ShareContent"]["shareCommentary"][
            "text"
        ]
        == "news"
    )


def test_linkedin_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOCIAL_LINKEDIN_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SOCIAL_LINKEDIN_TOKEN"):
        LinkedInProvider()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_all_providers_satisfy_protocol() -> None:
    providers: list[ChannelProvider] = [
        StubEmailProvider(),
        StubSMSProvider(),
        StubSocialProvider(network="meta"),
    ]
    for provider in providers:
        assert isinstance(provider.name, str)
        assert callable(provider.send)
        assert callable(provider.parse_webhook)


@pytest.mark.asyncio
async def test_send_result_shape(db_session: Any, business_id: Any) -> None:
    result = await StubEmailProvider().send(
        SendRequest(
            channel="email",
            to="a@b.c",
            subject="s",
            body="b",
            metadata=_stub_meta(db_session, business_id),
        )
    )
    assert isinstance(result, SendResult)
    assert isinstance(result.provider_message_id, str)
    assert isinstance(result.status, str)
    assert isinstance(result.raw, dict)
