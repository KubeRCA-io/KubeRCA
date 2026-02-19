"""Core data structures for KubeRCA."""

from kuberca.models.alerts import AnomalyAlert
from kuberca.models.analysis import (
    AffectedResource,
    CorrelationResult,
    EvaluationMeta,
    EvidenceItem,
    RCAResponse,
    ResponseMeta,
    RuleResult,
)
from kuberca.models.config import KubeRCAConfig
from kuberca.models.events import (
    DiagnosisSource,
    EventRecord,
    EventSource,
    EvidenceType,
    Severity,
)
from kuberca.models.resources import (
    CachedResource,
    CachedResourceView,
    CacheReadiness,
    FieldChange,
    ResourceSnapshot,
)

__all__ = [
    "AffectedResource",
    "AnomalyAlert",
    "CacheReadiness",
    "CachedResource",
    "CachedResourceView",
    "CorrelationResult",
    "DiagnosisSource",
    "EvaluationMeta",
    "EventRecord",
    "EventSource",
    "EvidenceItem",
    "EvidenceType",
    "FieldChange",
    "KubeRCAConfig",
    "RCAResponse",
    "ResourceSnapshot",
    "ResponseMeta",
    "RuleResult",
    "Severity",
]
