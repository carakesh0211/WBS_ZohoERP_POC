"""The mail provider behind an adapter (Stream E, 2026-09-13).

`pg/notifications.py` drains the outbox through ONE interface, `MailAdapter`,
and never imports a provider. Three implementations:

* :class:`RecordingMailAdapter` -- the default. Sends nothing; records every
  message it was handed (in memory, and the outbox's own delivery history
  records it as RECORDED). It is what UAT runs until the Catalyst sender
  domain is verified, and what every test runs.
* :class:`CatalystSdkMailAdapter` -- Zoho Catalyst Mail through the official
  Python SDK (`zcatalyst_sdk`), which AppSail provisions with the project's
  own credentials; nothing is configured in this repository. The SDK is
  imported lazily and its absence is reported, not hidden: the adapter says
  `available()` is False and the dispatcher records the reason.
* :class:`CatalystRestMailAdapter` -- UNVERIFIED. The shape of the REST call
  is written down so the day it is verified against the DEMO tenant is a
  one-line change; until then it refuses to send and says why.

Selection is by environment, `CAPEX_MAIL_ADAPTER` = recording (default) |
catalyst_sdk | catalyst_rest. Sender address and display name come from
`CAPEX_MAIL_FROM` and `CAPEX_MAIL_FROM_NAME`; the domain of the sender must
be verified in the Catalyst console (Mail -> Sender domains) before Catalyst
will accept a message -- see docs/fable51/IDENTITY_AND_NOTIFICATIONS.md.

No credential is read here. The SDK reads its own environment inside
AppSail; the REST adapter would take a token from the environment when it
is verified, and would never log it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol


class MailSendError(RuntimeError):
    """A send that failed. `retryable` says whether the dispatcher should try
    again with backoff or move the message to the dead-letter state."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class OutboundMail:
    to_email: str
    subject: str
    body_text: str
    body_html: str | None = None
    notification_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class SendReceipt:
    provider: str
    provider_message_id: str | None
    detail: str = ""


class MailAdapter(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(can send, why not)."""

    def send(self, mail: OutboundMail) -> SendReceipt:
        """Deliver, or raise `MailSendError`."""


def sender() -> tuple[str, str]:
    """The configured sender address and display name."""
    return (os.environ.get("CAPEX_MAIL_FROM", "").strip(),
            os.environ.get("CAPEX_MAIL_FROM_NAME", "CAPEX & WBS Control Hub").strip())


@dataclass
class RecordingMailAdapter:
    """Records; never sends."""

    name: str = "recording"
    sent: list[OutboundMail] = field(default_factory=list)
    fail_next: list[MailSendError] = field(default_factory=list)

    def available(self) -> tuple[bool, str]:
        return True, ""

    def send(self, mail: OutboundMail) -> SendReceipt:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append(mail)
        return SendReceipt(provider=self.name,
                           provider_message_id=f"recorded-{len(self.sent)}",
                           detail="recorded, not sent")


class CatalystSdkMailAdapter:
    """Zoho Catalyst Mail through `zcatalyst_sdk`, initialised per send with
    the request-less server credentials AppSail injects."""

    name = "catalyst_sdk"

    def _sdk(self):
        try:
            import zcatalyst_sdk  # type: ignore
        except ImportError:
            return None
        return zcatalyst_sdk

    def available(self) -> tuple[bool, str]:
        if self._sdk() is None:
            return False, "zcatalyst_sdk is not installed in this process"
        from_email, _ = sender()
        if not from_email:
            return False, "CAPEX_MAIL_FROM is not set (a verified sender address)"
        return True, ""

    def send(self, mail: OutboundMail) -> SendReceipt:
        ok, why = self.available()
        if not ok:
            raise MailSendError(why, retryable=False)
        sdk = self._sdk()
        from_email, from_name = sender()
        try:
            app = sdk.initialize()
            payload: dict[str, Any] = {
                "from_email": from_email, "to_email": [mail.to_email],
                "subject": mail.subject,
                "content": mail.body_html or mail.body_text,
                "html_mode": bool(mail.body_html),
            }
            if from_name:
                payload["display_name"] = from_name
            result = app.email().send_mail(payload)
        except Exception as exc:  # the SDK raises its own hierarchy
            # The exception TYPE only: a provider error text can carry the
            # recipient, the message or a token, none of which belongs in the
            # delivery history.
            raise MailSendError(f"provider raised {type(exc).__name__}", retryable=True) from exc
        message_id = None
        if isinstance(result, dict):
            message_id = str(result.get("message_id") or result.get("id") or "") or None
        return SendReceipt(provider=self.name, provider_message_id=message_id)


class CatalystRestMailAdapter:
    """UNVERIFIED against the tenant. Refuses until it is."""

    name = "catalyst_rest"

    def available(self) -> tuple[bool, str]:
        return False, ("the Catalyst Mail REST call has not been verified against the "
                       "DEMO tenant; use catalyst_sdk on AppSail or recording")

    def send(self, mail: OutboundMail) -> SendReceipt:
        raise MailSendError(self.available()[1], retryable=False)


_ADAPTER: MailAdapter | None = None


def get_mail_adapter() -> MailAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        name = (os.environ.get("CAPEX_MAIL_ADAPTER") or "recording").strip().lower()
        if name == "recording":
            _ADAPTER = RecordingMailAdapter()
        elif name == "catalyst_sdk":
            _ADAPTER = CatalystSdkMailAdapter()
        elif name == "catalyst_rest":
            _ADAPTER = CatalystRestMailAdapter()
        else:
            raise RuntimeError(
                f"CAPEX_MAIL_ADAPTER={name!r} names no adapter this build carries "
                f"(recording, catalyst_sdk, catalyst_rest).")
    return _ADAPTER


def set_mail_adapter(adapter: MailAdapter | None) -> None:
    global _ADAPTER
    _ADAPTER = adapter
