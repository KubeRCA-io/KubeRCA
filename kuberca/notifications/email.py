"""Email notification channel for KubeRCA.

Sends AnomalyAlert instances as HTML emails via SMTP using the Python
standard-library ``smtplib`` executed in a thread-pool executor so the
asyncio event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

from kuberca.models.alerts import AnomalyAlert
from kuberca.models.events import Severity
from kuberca.notifications.manager import NotificationChannel

_log = structlog.get_logger(component="notifications.email")

_SEVERITY_COLOR: dict[Severity, str] = {
    Severity.INFO: "#2e7d32",
    Severity.WARNING: "#e65100",
    Severity.ERROR: "#b71c1c",
    Severity.CRITICAL: "#4a0000",
}


class SMTPConfig:
    """SMTP connection parameters.

    Args:
        host:       SMTP server hostname.
        port:       SMTP server port (587 for STARTTLS, 465 for SSL).
        username:   SMTP authentication username.
        password:   SMTP authentication password.
        use_tls:    If True, use SMTP_SSL (port 465). Defaults to False
                    (STARTTLS on port 587).
        from_addr:  Sender email address.
        timeout:    Socket timeout in seconds. Defaults to 10.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        use_tls: bool = False,
        timeout: float = 10.0,
    ) -> None:
        if not host:
            raise ValueError("SMTP host must not be empty")
        if not from_addr:
            raise ValueError("SMTP from_addr must not be empty")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.use_tls = use_tls
        self.timeout = timeout


class EmailNotificationChannel(NotificationChannel):
    """Delivers alerts as HTML emails via SMTP.

    Args:
        smtp_config: Connection and authentication parameters.
        to_addr:     Recipient email address.
    """

    def __init__(self, smtp_config: SMTPConfig, to_addr: str) -> None:
        if not to_addr:
            raise ValueError("Email to_addr must not be empty")
        self._smtp = smtp_config
        self._to_addr = to_addr

    @property
    def channel_name(self) -> str:
        return "email"

    async def send(self, alert: AnomalyAlert) -> bool:
        """Send *alert* as an HTML email.

        SMTP I/O is delegated to a thread-pool executor to avoid blocking
        the event loop.

        Returns True on successful delivery, False otherwise.
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._send_sync, alert)
            return True
        except smtplib.SMTPException as exc:
            _log.warning("email_smtp_error", error=str(exc), alert_id=alert.alert_id)
            return False
        except OSError as exc:
            _log.warning("email_connection_error", error=str(exc), alert_id=alert.alert_id)
            return False

    def _send_sync(self, alert: AnomalyAlert) -> None:
        """Blocking SMTP delivery — runs inside a thread executor."""
        msg = self._build_message(alert)
        context = ssl.create_default_context()

        if self._smtp.use_tls:
            with smtplib.SMTP_SSL(
                self._smtp.host,
                self._smtp.port,
                context=context,
                timeout=self._smtp.timeout,
            ) as server:
                if self._smtp.username:
                    server.login(self._smtp.username, self._smtp.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(
                self._smtp.host,
                self._smtp.port,
                timeout=self._smtp.timeout,
            ) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                if self._smtp.username:
                    server.login(self._smtp.username, self._smtp.password)
                server.send_message(msg)

    def _build_message(self, alert: AnomalyAlert) -> MIMEMultipart:
        """Construct a MIME multipart email with a plain-text and HTML part."""
        severity_label = alert.severity.value.upper()
        subject = (
            f"[KubeRCA] {severity_label} — "
            f"{alert.resource_kind}/{alert.resource_name} in {alert.namespace}: "
            f"{alert.reason}"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._smtp.from_addr
        msg["To"] = self._to_addr

        plain = self._build_plain(alert, severity_label)
        html = self._build_html(alert, severity_label)

        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        return msg

    def _build_plain(self, alert: AnomalyAlert, severity_label: str) -> str:
        detected_at = alert.detected_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"KubeRCA Alert\n"
            f"{'=' * 60}\n\n"
            f"Severity:   {severity_label}\n"
            f"Resource:   {alert.resource_kind}/{alert.resource_name}\n"
            f"Namespace:  {alert.namespace}\n"
            f"Reason:     {alert.reason}\n"
            f"Events:     {alert.event_count}\n"
            f"Detected:   {detected_at}\n"
            f"Alert ID:   {alert.alert_id}\n\n"
            f"Summary\n"
            f"{'-' * 60}\n"
            f"{alert.summary}\n"
        )

    def _build_html(self, alert: AnomalyAlert, severity_label: str) -> str:
        color = _SEVERITY_COLOR.get(alert.severity, "#333333")
        detected_at = alert.detected_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>KubeRCA Alert</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
             background: #f5f5f5; margin: 0; padding: 24px;">
  <table width="600" cellpadding="0" cellspacing="0"
         style="background: #ffffff; border-radius: 8px;
                box-shadow: 0 1px 3px rgba(0,0,0,.15); margin: 0 auto;">
    <tr>
      <td style="background: {color}; padding: 20px 28px; border-radius: 8px 8px 0 0;">
        <h1 style="color: #ffffff; margin: 0; font-size: 20px;">
          KubeRCA Alert &mdash; {severity_label}
        </h1>
      </td>
    </tr>
    <tr>
      <td style="padding: 24px 28px;">
        <table width="100%" cellpadding="6" cellspacing="0"
               style="border-collapse: collapse; margin-bottom: 20px;">
          <tr style="border-bottom: 1px solid #e0e0e0;">
            <td style="color: #757575; width: 120px;"><strong>Resource</strong></td>
            <td style="font-family: monospace;">{alert.resource_kind}/{alert.resource_name}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e0e0e0;">
            <td style="color: #757575;"><strong>Namespace</strong></td>
            <td style="font-family: monospace;">{alert.namespace}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e0e0e0;">
            <td style="color: #757575;"><strong>Reason</strong></td>
            <td>{alert.reason}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e0e0e0;">
            <td style="color: #757575;"><strong>Events</strong></td>
            <td>{alert.event_count}</td>
          </tr>
          <tr>
            <td style="color: #757575;"><strong>Detected</strong></td>
            <td>{detected_at}</td>
          </tr>
        </table>
        <h2 style="font-size: 15px; color: #424242; margin: 0 0 8px;">Summary</h2>
        <p style="color: #212121; line-height: 1.6; margin: 0 0 20px;">{alert.summary}</p>
      </td>
    </tr>
    <tr>
      <td style="background: #f5f5f5; padding: 12px 28px; border-radius: 0 0 8px 8px;
                 font-size: 12px; color: #9e9e9e;">
        Alert ID: {alert.alert_id}
      </td>
    </tr>
  </table>
</body>
</html>"""
