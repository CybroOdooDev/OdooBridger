"""Outgoing email. With no SMTP_HOST, messages are logged (and kept in
``OUTBOX`` for tests) instead of sent, so development needs no mail server."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from obcore.config import settings

log = logging.getLogger(__name__)
OUTBOX: list[dict[str, str]] = []


class MailError(RuntimeError):
    pass


def send_email(to: str, subject: str, body: str) -> None:
    OUTBOX.append({"to": to, "subject": subject, "body": body})
    del OUTBOX[:-50]
    if not settings.smtp_host:
        log.info("Email (not sent, SMTP_HOST unset) to %s: %s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, 465, context=ssl.create_default_context(),
                                  timeout=settings.http_timeout_seconds) as smtp:
                _send(smtp, msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                              timeout=settings.http_timeout_seconds) as smtp:
                if settings.smtp_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                _send(smtp, msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"Could not send email: {exc}") from exc


def _send(smtp: smtplib.SMTP, msg: EmailMessage) -> None:
    if settings.smtp_username:
        smtp.login(settings.smtp_username, settings.smtp_password)
    smtp.send_message(msg)


def try_send(to: str, subject: str, body: str) -> bool:
    try:
        send_email(to, subject, body)
        return True
    except MailError as exc:
        log.warning("%s", exc)
        return False
