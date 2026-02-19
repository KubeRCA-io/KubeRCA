"""R11 FailedMount-ConfigMapNotFound -- Tier 2 rule.

Matches FailedMount events where a ConfigMap referenced by a Pod volume
could not be found, and correlates the ConfigMap's presence in cache.
"""

from __future__ import annotations

import re
import time
from datetime import UTC

from kuberca.models.analysis import (
    AffectedResource,
    CorrelationResult,
    EvidenceItem,
    RuleResult,
)
from kuberca.models.events import EventRecord, EvidenceType
from kuberca.models.resources import FieldChange
from kuberca.observability.logging import get_logger
from kuberca.observability.metrics import rule_matches_total
from kuberca.rules.base import ChangeLedger, ResourceCache, Rule
from kuberca.rules.confidence import compute_confidence

_logger = get_logger("rule.r11_failedmount_configmap")

_RE_CONFIGMAP_NAME = re.compile(
    r'configmap[s]?\s+["\']?([\w.-]+)["\']?|configmap\s+"?([\w.-]+)"?\s+not found',
    re.IGNORECASE,
)


def _extract_configmap_name(message: str) -> str | None:
    match = _RE_CONFIGMAP_NAME.search(message)
    if match:
        return match.group(1) or match.group(2)
    return None


class FailedMountConfigMapRule(Rule):
    """Matches FailedMount events caused by missing ConfigMaps."""

    rule_id = "R11_failedmount_configmap"
    display_name = "FailedMount -- ConfigMap Not Found"
    priority = 20
    base_confidence = 0.60
    resource_dependencies = ["Pod", "ConfigMap"]
    relevant_field_paths = ["spec.volumes", "spec.template.spec.volumes"]

    def match(self, event: EventRecord) -> bool:
        if event.reason != "FailedMount":
            return False
        msg = event.message.lower()
        return "configmap" in msg

    def correlate(
        self,
        event: EventRecord,
        cache: ResourceCache,
        ledger: ChangeLedger,
    ) -> CorrelationResult:
        t_start = time.monotonic()
        objects_queried = 0
        related_resources = []
        relevant_changes: list[FieldChange] = []

        cm_name = _extract_configmap_name(event.message)
        if cm_name:
            cm = cache.get("ConfigMap", event.namespace, cm_name)
            objects_queried += 1
            if cm is not None:
                related_resources.append(cm)

        duration_ms = (time.monotonic() - t_start) * 1000.0
        return CorrelationResult(
            changes=relevant_changes,
            related_resources=related_resources,
            objects_queried=objects_queried,
            duration_ms=duration_ms,
        )

    def explain(self, event: EventRecord, correlation: CorrelationResult) -> RuleResult:
        rule_matches_total.labels(rule_id=self.rule_id).inc()
        confidence = compute_confidence(self, correlation, event)

        cm_name = _extract_configmap_name(event.message)
        cm_found = any(r.kind == "ConfigMap" for r in correlation.related_resources)

        evidence = [
            EvidenceItem(
                type=EvidenceType.EVENT,
                timestamp=event.last_seen.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                summary=f"Pod {event.resource_name} failed to mount ConfigMap (count={event.count}): {event.message[:300]}",
            )
        ]
        affected = [AffectedResource(kind="Pod", namespace=event.namespace, name=event.resource_name)]

        if cm_name and not cm_found:
            root_cause = f"Pod {event.resource_name} cannot start: ConfigMap '{cm_name}' not found in namespace '{event.namespace}'."
        else:
            root_cause = f"Pod {event.resource_name} failed to mount a ConfigMap volume. The referenced ConfigMap may be missing or inaccessible."

        remediation = (
            f"Verify the ConfigMap exists: `kubectl get configmap {cm_name or '<name>'} -n {event.namespace}`. "
            f"Inspect pod events: `kubectl describe pod {event.resource_name} -n {event.namespace}`."
        )

        _logger.info("r11_match", pod=event.resource_name, namespace=event.namespace, confidence=confidence)

        return RuleResult(
            rule_id=self.rule_id,
            root_cause=root_cause,
            confidence=confidence,
            evidence=evidence,
            affected_resources=affected,
            suggested_remediation=remediation,
        )
