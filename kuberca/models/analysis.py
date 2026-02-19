"""RCA response and analysis data structures."""

from __future__ import annotations

from dataclasses import dataclass, field

from kuberca.models.events import DiagnosisSource, EvidenceType
from kuberca.models.resources import CachedResourceView, FieldChange


@dataclass(frozen=True)
class StateContextEntry:
    """Verified state of a related resource from the dependency graph."""

    kind: str
    namespace: str
    name: str
    exists: bool
    status_summary: str  # e.g. "Bound", "Lost", "Running", "<not found>"
    relationship: str  # e.g. "PVC referenced by Pod via volume mount"


@dataclass
class QualityCheckResult:
    """Result of deterministic quality check on LLM output."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    # failure types: "zero_citations", "reason_mismatch", "state_contradiction"


@dataclass
class EvidenceItem:
    """A single piece of evidence supporting a diagnosis."""

    type: EvidenceType
    timestamp: str  # ISO-8601 UTC
    summary: str
    debug_context: dict[str, object] | None = None

    # debug_context constraints:
    # - Only populated when log.level=debug
    # - Max 2KB serialized per item
    # - Whitelisted keys only: event_reason, resource_version, container_name,
    #   exit_code, field_path, old_value, new_value


@dataclass
class AffectedResource:
    """A resource affected by the diagnosed incident."""

    kind: str
    namespace: str
    name: str


@dataclass
class ResponseMeta:
    """Metadata about the analysis response."""

    kuberca_version: str
    schema_version: str = "1"
    cluster_id: str = ""
    timestamp: str = ""  # ISO-8601 UTC
    response_time_ms: int = 0
    cache_state: str = "ready"
    warnings: list[str] = field(default_factory=list)


@dataclass
class RCAResponse:
    """Unified output structure returned by all interfaces.

    Contract between the Analyst Coordinator and all consumers (MCP, REST, CLI).
    """

    root_cause: str
    confidence: float
    diagnosed_by: DiagnosisSource
    rule_id: str | None = None
    evidence: list[EvidenceItem] = field(default_factory=list)
    affected_resources: list[AffectedResource] = field(default_factory=list)
    blast_radius: list[AffectedResource] | None = None
    suggested_remediation: str = ""
    _meta: ResponseMeta | None = None


@dataclass
class CorrelationResult:
    """Result of a rule's correlate() phase."""

    changes: list[FieldChange] = field(default_factory=list)
    related_events: list[object] = field(default_factory=list)  # EventRecord
    related_resources: list[CachedResourceView] = field(default_factory=list)
    objects_queried: int = 0
    duration_ms: float = 0.0


@dataclass
class RuleResult:
    """Result of a rule's explain() phase."""

    rule_id: str
    root_cause: str
    confidence: float
    evidence: list[EvidenceItem] = field(default_factory=list)
    affected_resources: list[AffectedResource] = field(default_factory=list)
    suggested_remediation: str = ""


@dataclass
class EvaluationMeta:
    """Returned alongside RuleResult | None for Analyst to populate _meta."""

    rules_evaluated: int = 0
    rules_matched: int = 0
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
