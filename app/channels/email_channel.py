from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

from app.channels.base import Channel, ChannelResult
from app.channels.log_channel import LogChannel

logger = logging.getLogger("recovery.channels.email")


class EmailChannel(Channel):
    name = "email"

    def send(self, *, to: str, cc: str | None, subject: str, body: str, html: str | None = None) -> ChannelResult:
        override = os.getenv("TEST_EMAIL_OVERRIDE")
        if override:
            # Testing convenience (see CONTEXT.md): every escalation email
            # gets redirected to one inbox instead of the sheet's real
            # spoc/manager/skip_level addresses, so you can watch the whole
            # chain land without emailing anyone real. The original intended
            # recipient(s) go in the BODY, not the subject — a subject
            # stuffed with email addresses is itself a spam signal.
            original = to + (f", cc: {cc}" if cc else "")
            note = f"[Test override — would really have gone to: {original}]"
            body = f"{note}\n\n{body}"
            if html:
                html = f'<p style="color:#a15c00;font-size:12px;margin:0 0 16px">{note}</p>{html}'
            to, cc = override, None

        sendgrid_key = os.getenv("SENDGRID_API_KEY")
        smtp_host = os.getenv("SMTP_HOST")

        if sendgrid_key:
            return self._send_sendgrid(sendgrid_key, to, cc, subject, body, html)
        if smtp_host:
            return self._send_smtp(smtp_host, to, cc, subject, body)
        return LogChannel().send(to=to, cc=cc, subject=subject, body=body, html=html)

    def _send_sendgrid(self, api_key: str, to: str, cc: str | None, subject: str, body: str, html: str | None) -> ChannelResult:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Cc, ClickTracking, Mail, OpenTracking, TrackingSettings

            message = Mail(
                from_email=os.getenv("EMAIL_FROM", "collections@example.com"),
                to_emails=to,
                subject=subject,
                plain_text_content=body,
                html_content=html,
            )
            if cc:
                message.cc = [Cc(cc)]
            # SendGrid's default click-tracking rewrites every link into a
            # redirect through their own domain — the resulting URL chain
            # reads as spammy to mail providers and to a human recipient.
            # Point links straight at the real destination instead. Must be
            # the SDK's own helper objects, not a plain dict — the Mail
            # helper serializes via each object's own .get(), so a raw
            # dict here breaks with a cryptic "get expected at least 1
            # argument, got 0" deep inside the SDK.
            message.tracking_settings = TrackingSettings(
                click_tracking=ClickTracking(enable=False, enable_text=False),
                open_tracking=OpenTracking(enable=False),
            )
            response = SendGridAPIClient(api_key).send(message)
            return ChannelResult(status="sent", detail=f"sendgrid status={response.status_code}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("SendGrid send failed")
            return ChannelResult(status="failed", detail=str(exc))

    def _send_smtp(self, host: str, to: str, cc: str | None, subject: str, body: str) -> ChannelResult:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = os.getenv("EMAIL_FROM", "collections@example.com")
            msg["To"] = to
            if cc:
                msg["Cc"] = cc
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as server:
                server.starttls()
                user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
                if user and password:
                    server.login(user, password)
                recipients = [to] + ([cc] if cc else [])
                server.sendmail(msg["From"], recipients, msg.as_string())
            return ChannelResult(status="sent", detail=f"smtp to={to}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("SMTP send failed")
            return ChannelResult(status="failed", detail=str(exc))
