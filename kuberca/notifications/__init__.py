"""Notification system for KubeRCA.

Dispatches AnomalyAlert instances to one or more notification channels
(Slack, Email, Webhook) with built-in deduplication.

Exports:
    NotificationChannel    -- Abstract base for all channel implementations.
    NotificationDispatcher -- Sends an alert to all registered channels
                              without blocking the detection pipeline.
    AlertDeduplicator      -- 15-minute cooldown per (namespace, resource_kind,
                              resource_name, reason) tuple.
    SlackNotificationChannel   -- Slack Block Kit channel via webhook URL.
    EmailNotificationChannel   -- SMTP email channel via stdlib smtplib.
    WebhookNotificationChannel -- Generic JSON POST webhook channel.
    build_notification_dispatcher -- Factory used by the application bootstrap.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from kuberca.notifications.email import EmailNotificationChannel, SMTPConfig
from kuberca.notifications.manager import (
    AlertDeduplicator,
    NotificationChannel,
    NotificationDispatcher,
)
from kuberca.notifications.slack import SlackNotificationChannel
from kuberca.notifications.webhook import WebhookNotificationChannel

if TYPE_CHECKING:
    from kuberca.models.config import NotificationConfig

_log = structlog.get_logger(component="notifications")

__all__ = [
    "AlertDeduplicator",
    "EmailNotificationChannel",
    "NotificationChannel",
    "NotificationDispatcher",
    "SlackNotificationChannel",
    "SMTPConfig",
    "WebhookNotificationChannel",
    "build_notification_dispatcher",
]


def build_notification_dispatcher(
    config: NotificationConfig,
) -> NotificationDispatcher:
    """Build a NotificationDispatcher from environment-resolved secrets.

    Channels are enabled only when the relevant secret ref resolves to a
    non-empty string.  The ``*_secret_ref`` fields in NotificationConfig
    are names of environment variables that hold the actual secret values.

    Slack:
        KUBERCA_NOTIFICATIONS_SLACK_SECRET_REF (env var name) ->
        env var value is the webhook URL.

    Email:
        KUBERCA_NOTIFICATIONS_EMAIL_SECRET_REF (env var name) ->
        env var value is ``smtp://user:pass@host:port/from@addr``.
        KUBERCA_NOTIFICATIONS_EMAIL_TO is the recipient address.

    Webhook:
        KUBERCA_NOTIFICATIONS_WEBHOOK_SECRET_REF (env var name) ->
        env var value is the webhook URL.
    """
    channels: list[NotificationChannel] = []

    # --- Slack ---
    slack_ref = config.slack_secret_ref
    if slack_ref:
        slack_url = os.environ.get(slack_ref, "")
        if slack_url:
            try:
                channels.append(SlackNotificationChannel(webhook_url=slack_url))
                _log.info("slack_channel_enabled")
            except ValueError as exc:
                _log.warning("slack_channel_disabled", reason=str(exc))
        else:
            _log.debug("slack_channel_skipped", reason="secret ref env var is empty")

    # --- Email ---
    email_ref = config.email_secret_ref
    email_to = config.email_to
    if email_ref and email_to:
        smtp_dsn = os.environ.get(email_ref, "")
        if smtp_dsn:
            try:
                smtp_cfg = _parse_smtp_dsn(smtp_dsn)
                channels.append(EmailNotificationChannel(smtp_config=smtp_cfg, to_addr=email_to))
                _log.info("email_channel_enabled", to=email_to)
            except (ValueError, KeyError) as exc:
                _log.warning("email_channel_disabled", reason=str(exc))
        else:
            _log.debug("email_channel_skipped", reason="secret ref env var is empty")

    # --- Generic webhook ---
    webhook_ref = config.webhook_secret_ref
    if webhook_ref:
        webhook_url = os.environ.get(webhook_ref, "")
        if webhook_url:
            try:
                channels.append(WebhookNotificationChannel(url=webhook_url))
                _log.info("webhook_channel_enabled")
            except ValueError as exc:
                _log.warning("webhook_channel_disabled", reason=str(exc))
        else:
            _log.debug("webhook_channel_skipped", reason="secret ref env var is empty")

    if not channels:
        _log.info("no_notification_channels_configured")

    return NotificationDispatcher(channels=channels)


def _parse_smtp_dsn(dsn: str) -> SMTPConfig:
    """Parse ``smtp[s]://user:pass@host:port/from@addr`` into SMTPConfig.

    The ``from@addr`` segment is the URL path (with leading slash stripped).
    Use_tls is True when the scheme is ``smtps``.

    Raises:
        ValueError: if the DSN cannot be parsed.
    """
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    if parsed.scheme not in ("smtp", "smtps"):
        raise ValueError(f"SMTP DSN must start with smtp:// or smtps://, got: {dsn!r}")
    host = parsed.hostname or ""
    port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
    username = parsed.username or ""
    password = parsed.password or ""
    from_addr = (parsed.path or "").lstrip("/")
    use_tls = parsed.scheme == "smtps"
    return SMTPConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        from_addr=from_addr,
        use_tls=use_tls,
    )
