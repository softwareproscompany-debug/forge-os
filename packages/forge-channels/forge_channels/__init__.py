"""forge_channels — multi-channel send abstraction for ForgeOS.

* :class:`SendRequest` / :class:`SendResult` — the send contract
* :class:`ChannelProvider` — protocol every provider satisfies
* Factories: :func:`get_email_provider`, :func:`get_sms_provider`,
  :func:`get_social_provider` — selected from ``CHANNEL_MODE`` /
  ``EMAIL_PROVIDER`` / ``SMS_PROVIDER`` env vars per CONTRACTS.md
* :class:`DevOutbox` — dev-outbox writer used by the stub providers
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from forge_channels._env import get_env
from forge_channels.email import (
    SendGridProvider,
    SESProvider,
    SMTPProvider,
    StubEmailProvider,
)
from forge_channels.outbox import DevOutbox
from forge_channels.ses_auth import sign_sesv2_request
from forge_channels.sms import StubSMSProvider, TwilioProvider
from forge_channels.social import (
    LinkedInProvider,
    MetaProvider,
    StubSocialProvider,
    XProvider,
)


@dataclass
class SendRequest:
    """Input to :meth:`ChannelProvider.send`."""

    channel: str
    to: str
    subject: str | None
    body: str
    metadata: dict = field(default_factory=dict)


@dataclass
class SendResult:
    """Output of :meth:`ChannelProvider.send`."""

    provider_message_id: str
    status: str
    raw: dict = field(default_factory=dict)


class ChannelProvider(Protocol):
    """Contract every channel provider must satisfy."""

    name: str

    async def send(self, req: SendRequest) -> SendResult: ...

    async def parse_webhook(self, payload: dict) -> dict: ...


def _channel_mode() -> str:
    return get_env("CHANNEL_MODE", "stub").lower()


def get_email_provider() -> ChannelProvider:
    """Select the email provider.

    ``CHANNEL_MODE=stub`` (default) -> :class:`StubEmailProvider` writing to the
    dev outbox. Otherwise ``EMAIL_PROVIDER`` picks ``smtp`` (default),
    ``sendgrid`` or ``ses``.
    """
    if _channel_mode() == "stub":
        return StubEmailProvider()
    name = get_env("EMAIL_PROVIDER", "smtp").lower()
    if name == "smtp":
        return SMTPProvider()
    if name == "sendgrid":
        return SendGridProvider()
    if name == "ses":
        return SESProvider()
    raise ValueError(
        f"Unknown EMAIL_PROVIDER={name!r}; expected one of: smtp, sendgrid, ses"
    )


def get_sms_provider() -> ChannelProvider:
    """Select the SMS provider.

    ``CHANNEL_MODE=stub`` (default) -> :class:`StubSMSProvider`. Otherwise
    ``SMS_PROVIDER`` picks ``twilio`` (default).
    """
    if _channel_mode() == "stub":
        return StubSMSProvider()
    name = get_env("SMS_PROVIDER", "twilio").lower()
    if name == "twilio":
        return TwilioProvider()
    raise ValueError(f"Unknown SMS_PROVIDER={name!r}; expected one of: twilio")


def get_social_provider(network: str) -> ChannelProvider:
    """Select the social provider for ``network`` (``meta`` | ``linkedin`` | ``x``).

    ``CHANNEL_MODE=stub`` (default) -> :class:`StubSocialProvider` writing to
    the dev outbox.
    """
    net = network.strip().lower()
    if net not in ("meta", "linkedin", "x"):
        raise ValueError(
            f"Unknown social network={network!r}; expected one of: meta, linkedin, x"
        )
    if _channel_mode() == "stub":
        return StubSocialProvider(network=net)
    if net == "meta":
        return MetaProvider()
    if net == "linkedin":
        return LinkedInProvider()
    return XProvider()


__all__ = [
    "ChannelProvider",
    "DevOutbox",
    "LinkedInProvider",
    "MetaProvider",
    "SESProvider",
    "SendGridProvider",
    "SendRequest",
    "SendResult",
    "SMTPProvider",
    "StubEmailProvider",
    "StubSMSProvider",
    "StubSocialProvider",
    "TwilioProvider",
    "XProvider",
    "get_email_provider",
    "get_sms_provider",
    "get_social_provider",
    "sign_sesv2_request",
]
