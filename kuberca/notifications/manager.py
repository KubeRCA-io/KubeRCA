"""Notification dispatcher and deduplication for KubeRCA.

NotificationChannel  -- ABC every channel must implement.
NotificationDispatcher -- Fans out alerts to all registered channels;
                          failures in one channel never block others or
                          the anomaly-detection pipeline.
AlertDeduplicator    -- Enforces a 15-minute cooldown per
                        (namespace, resource_kind, resource_name, reason).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

import structlog

from kuberca.models.alerts import AnomalyAlert
from kuberca.observability.metrics import notifications_total

_log = structlog.get_logger(component="notifications.manager")

_DEDUP_COOLDOWN = timedelta(minutes=15)


class NotificationChannel(ABC):
    """Abstract base class for all notification channels.

    Every concrete channel must implement ``send``, which should be
    idempotent and not raise — return ``False`` instead of raising.
    """

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Human-readable channel identifier used in metrics and logs."""

    @abstractmethod
    async def send(self, alert: AnomalyAlert) -> bool:
        """Deliver *alert* via this channel.

        Returns:
            True  -- message accepted by the remote endpoint.
            False -- delivery failed (already logged inside implementation).
        """


class AlertDeduplicator:
    """Suppresses duplicate alerts within a 15-minute cooldown window.

    The deduplication key is the 4-tuple
    ``(namespace, resource_kind, resource_name, reason)``.
    State is held in-process; restarting KubeRCA resets all cooldowns.
    """

    def __init__(self, cooldown: timedelta = _DEDUP_COOLDOWN) -> None:
        self._cooldown = cooldown
        # key -> last_sent timestamp (UTC)
        self._last_sent: dict[tuple[str, str, str, str], datetime] = {}

    def should_send(self, alert: AnomalyAlert) -> bool:
        """Return True if this alert should be dispatched.

        A False return means an identical alert was sent within the cooldown
        window and should be suppressed.
        """
        key = (
            alert.namespace,
            alert.resource_kind,
            alert.resource_name,
            alert.reason,
        )
        now = datetime.now(tz=UTC)
        last = self._last_sent.get(key)
        if last is not None and (now - last) < self._cooldown:
            _log.debug(
                "alert_suppressed_by_deduplicator",
                namespace=alert.namespace,
                resource_kind=alert.resource_kind,
                resource_name=alert.resource_name,
                reason=alert.reason,
                seconds_remaining=int((self._cooldown - (now - last)).total_seconds()),
            )
            return False
        self._last_sent[key] = now
        return True

    def reset(self, namespace: str, resource_kind: str, resource_name: str, reason: str) -> None:
        """Remove a cooldown entry so the next matching alert is dispatched."""
        key = (namespace, resource_kind, resource_name, reason)
        self._last_sent.pop(key, None)


class NotificationDispatcher:
    """Fan-out dispatcher that sends an alert to every registered channel.

    * Never raises — exceptions from individual channels are caught and logged.
    * Never blocks the caller — ``dispatch`` is fire-and-forget; it schedules
      the fan-out as a background asyncio task.
    * Applies AlertDeduplicator before any I/O.
    """

    def __init__(
        self,
        channels: list[NotificationChannel],
        deduplicator: AlertDeduplicator | None = None,
    ) -> None:
        self._channels = channels
        self._deduplicator = deduplicator or AlertDeduplicator()

    def dispatch(self, alert: AnomalyAlert) -> None:
        """Schedule fan-out delivery of *alert* as a background task.

        Deduplication is checked synchronously before scheduling so that the
        result is available to callers without awaiting.
        """
        if not self._deduplicator.should_send(alert):
            return
        asyncio.ensure_future(self._fan_out(alert))

    async def _fan_out(self, alert: AnomalyAlert) -> None:
        """Deliver *alert* to every channel concurrently."""
        tasks = [self._send_one(channel, alert) for channel in self._channels]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_one(self, channel: NotificationChannel, alert: AnomalyAlert) -> None:
        """Deliver to a single channel, recording metrics regardless of outcome."""
        try:
            success = await channel.send(alert)
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "notification_channel_unexpected_error",
                channel=channel.channel_name,
                alert_id=alert.alert_id,
                error=str(exc),
            )
            success = False

        label = "true" if success else "false"
        notifications_total.labels(channel=channel.channel_name, success=label).inc()

        if success:
            _log.info(
                "notification_sent",
                channel=channel.channel_name,
                alert_id=alert.alert_id,
                severity=alert.severity.value,
                namespace=alert.namespace,
                resource=f"{alert.resource_kind}/{alert.resource_name}",
            )
        else:
            _log.warning(
                "notification_failed",
                channel=channel.channel_name,
                alert_id=alert.alert_id,
            )
