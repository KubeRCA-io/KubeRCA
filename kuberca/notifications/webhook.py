"""Generic JSON webhook notification channel for KubeRCA.

Posts AnomalyAlert data as a JSON body to any configured HTTP endpoint.
The payload schema mirrors the AnomalyAlert dataclass fields so that
consumers can parse it without KubeRCA-specific knowledge.
"""

from __future__ import annotations

import structlog

from kuberca.models.alerts import AnomalyAlert
from kuberca.notifications.manager import NotificationChannel

_log = structlog.get_logger(component="notifications.webhook")


class WebhookNotificationChannel(NotificationChannel):
    """Delivers alerts by POSTing a JSON payload to a configurable URL.

    Args:
        url:     Full endpoint URL (must be HTTPS in production).
        headers: Optional extra headers (e.g. Authorization).
        timeout: HTTP request timeout in seconds. Defaults to 10.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not url:
            raise ValueError("Webhook url must not be empty")
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout

    @property
    def channel_name(self) -> str:
        return "webhook"

    async def send(self, alert: AnomalyAlert) -> bool:
        """POST *alert* as JSON to the configured endpoint.

        Returns True on 2xx response, False otherwise.
        """
        import httpx

        payload = self._build_payload(alert)
        request_headers = {
            "Content-Type": "application/json",
            **self._headers,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._url,
                    json=payload,
                    headers=request_headers,
                )
                if response.is_success:
                    return True
                _log.warning(
                    "webhook_non_2xx_response",
                    status_code=response.status_code,
                    body=response.text[:200],
                    alert_id=alert.alert_id,
                )
                return False
        except httpx.TimeoutException:
            _log.warning("webhook_request_timeout", alert_id=alert.alert_id, url=self._url)
            return False
        except httpx.HTTPError as exc:
            _log.warning("webhook_http_error", error=str(exc), alert_id=alert.alert_id)
            return False

    def _build_payload(self, alert: AnomalyAlert) -> dict[str, object]:
        """Serialise *alert* to a plain dict for JSON encoding."""
        return {
            "alert_id": alert.alert_id,
            "severity": alert.severity.value,
            "resource_kind": alert.resource_kind,
            "resource_name": alert.resource_name,
            "namespace": alert.namespace,
            "reason": alert.reason,
            "summary": alert.summary,
            "detected_at": alert.detected_at.isoformat(),
            "event_count": alert.event_count,
            "source_events": alert.source_events,
        }
