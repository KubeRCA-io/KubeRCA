"""Core event data structures and enumerations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4


class EventSource(StrEnum):
    """Source of a Kubernetes event."""

    CORE_EVENT = "core_event"
    POD_PHASE = "pod_phase"
    NODE_CONDITION = "node_condition"


class Severity(StrEnum):
    """Event severity level."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DiagnosisSource(StrEnum):
    """Source of a diagnosis."""

    RULE_ENGINE = "rule_engine"
    LLM = "llm"
    INCONCLUSIVE = "inconclusive"


class EvidenceType(StrEnum):
    """Type of evidence item."""

    EVENT = "event"
    CHANGE = "change"


@dataclass(frozen=True)
class EventRecord:
    """Canonical event representation.

    Produced by the Event Collector, consumed by every downstream component.
    Immutable: no component may mutate an EventRecord after creation.
    """

    source: EventSource
    severity: Severity
    reason: str
    message: str
    namespace: str
    resource_kind: str
    resource_name: str
    first_seen: datetime
    last_seen: datetime
    event_id: str = field(default_factory=lambda: str(uuid4()))
    cluster_id: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    count: int = 1
    raw_object: dict[str, object] | None = None
