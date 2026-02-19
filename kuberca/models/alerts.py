"""Anomaly alert data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from kuberca.models.events import Severity


@dataclass(frozen=True)
class AnomalyAlert:
    """Emitted by Scout's anomaly detector, consumed by the notification system."""

    severity: Severity
    resource_kind: str
    resource_name: str
    namespace: str
    reason: str
    summary: str
    detected_at: datetime
    event_count: int = 1
    alert_id: str = field(default_factory=lambda: str(uuid4()))
    source_events: list[str] = field(default_factory=list)
