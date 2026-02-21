"""Tests for Tier 1 rules R01 OOMKilled and R03 FailedScheduling.

Covers match(), correlate(), explain() methods and all helper functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from kuberca.models.events import EventRecord, EventSource, Severity
from kuberca.models.resources import CachedResourceView, FieldChange
from kuberca.rules.r01_oom_killed import (
    OOMKilledRule,
    _container_name_from_path,
    _current_memory_limit,
    _find_owning_deployment,
    _is_memory_change,
)
from kuberca.rules.r03_failed_scheduling import (
    FailedSchedulingRule,
    _build_diagnosis,
    _classify_scheduler_message,
    _is_scheduling_relevant,
    _SchedulerPattern,
)

_TS = datetime(2026, 2, 18, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helper factories (same pattern as test_rules_r04_r10.py)
# ---------------------------------------------------------------------------


def _make_event(
    reason: str = "OOMKilled",
    message: str = "Container was OOM-killed",
    kind: str = "Pod",
    namespace: str = "default",
    name: str = "my-app-7b4f8c6d-x2kj",
    count: int = 1,
) -> EventRecord:
    return EventRecord(
        source=EventSource.CORE_EVENT,
        severity=Severity.WARNING,
        reason=reason,
        message=message,
        namespace=namespace,
        resource_kind=kind,
        resource_name=name,
        first_seen=_TS,
        last_seen=_TS,
        count=count,
    )


def _make_cache(
    get_return: CachedResourceView | None = None,
    list_return: list[CachedResourceView] | None = None,
) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = get_return
    cache.list.return_value = list_return or []
    return cache


def _make_resource_view(
    kind: str = "Deployment",
    namespace: str = "default",
    name: str = "my-app",
    status: dict | None = None,
    spec: dict | None = None,
) -> CachedResourceView:
    return CachedResourceView(
        kind=kind,
        namespace=namespace,
        name=name,
        resource_version="1",
        labels={},
        annotations={},
        spec=spec or {},
        status=status or {},
        last_updated=_TS,
    )


def _make_ledger(diff_return: list[FieldChange] | None = None) -> MagicMock:
    ledger = MagicMock()
    ledger.diff.return_value = diff_return or []
    return ledger


def _make_field_change(
    field_path: str = "spec.template.spec.containers[0].resources.limits.memory",
    old_value: str = "128Mi",
    new_value: str = "256Mi",
) -> FieldChange:
    return FieldChange(
        field_path=field_path,
        old_value=old_value,
        new_value=new_value,
        changed_at=_TS,
    )


# =====================================================================
# R01 OOMKilledRule
# =====================================================================


class TestR01Match:
    """Tests for OOMKilledRule.match()."""

    def test_matches_oomkilled(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(reason="OOMKilled")
        assert rule.match(event) is True

    def test_matches_oomkilling(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(reason="OOMKilling")
        assert rule.match(event) is True

    def test_no_match_crash_loop(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(reason="CrashLoopBackOff")
        assert rule.match(event) is False

    def test_no_match_empty_reason(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(reason="")
        assert rule.match(event) is False

    def test_no_match_partial_oom(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(reason="OOM")
        assert rule.match(event) is False


class TestR01Correlate:
    """Tests for OOMKilledRule.correlate()."""

    def test_correlate_with_deployment(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="my-app-7b4f8c6d-x2kj")
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            "ReplicaSet": [rs],
            "Deployment": [deploy],
        }.get(kind, [])
        ledger = _make_ledger()

        result = rule.correlate(event, cache, ledger)

        assert len(result.related_resources) == 1
        assert result.related_resources[0].name == "my-app"
        assert result.objects_queried > 0
        assert result.duration_ms >= 0

    def test_correlate_without_deployment(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="orphan-pod")
        cache = _make_cache()
        ledger = _make_ledger()

        result = rule.correlate(event, cache, ledger)

        assert len(result.related_resources) == 0
        assert result.changes == []

    def test_correlate_with_memory_changes(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="my-app-7b4f8c6d-x2kj")
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            "ReplicaSet": [rs],
            "Deployment": [deploy],
        }.get(kind, [])

        memory_change = _make_field_change()
        ledger = _make_ledger(diff_return=[memory_change])

        result = rule.correlate(event, cache, ledger)

        assert len(result.changes) == 1
        assert result.changes[0].field_path == memory_change.field_path

    def test_correlate_filters_non_memory_changes(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="my-app-7b4f8c6d-x2kj")
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            "ReplicaSet": [rs],
            "Deployment": [deploy],
        }.get(kind, [])

        # An image change should NOT be included
        image_change = _make_field_change(
            field_path="spec.template.spec.containers[0].image",
            old_value="app:v1",
            new_value="app:v2",
        )
        ledger = _make_ledger(diff_return=[image_change])

        result = rule.correlate(event, cache, ledger)

        assert result.changes == []  # image changes filtered out


class TestR01Explain:
    """Tests for OOMKilledRule.explain()."""

    def test_explain_with_memory_change(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="my-app-7b4f8c6d-x2kj")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        memory_change = _make_field_change(
            old_value="128Mi",
            new_value="256Mi",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[memory_change],
            related_events=[],
            related_resources=[deploy],
            objects_queried=5,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R01_oom_killed"
        assert "OOM-killed" in result.root_cause
        assert "128Mi" in result.root_cause
        assert "256Mi" in result.root_cause
        assert result.confidence > 0
        assert result.confidence <= 0.95
        assert len(result.evidence) == 2  # event + change
        assert len(result.affected_resources) == 2  # pod + deployment
        assert "memory limit" in result.suggested_remediation.lower() or "memory" in result.suggested_remediation.lower()

    def test_explain_without_memory_change(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="my-app-7b4f8c6d-x2kj")
        deploy = _make_resource_view(
            kind="Deployment",
            name="my-app",
            spec={
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {"limits": {"memory": "512Mi"}},
                            }
                        ]
                    }
                }
            },
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[deploy],
            objects_queried=5,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R01_oom_killed"
        assert "OOM-killed" in result.root_cause
        assert "No recent memory limit change" in result.root_cause
        assert "512Mi" in result.root_cause
        assert len(result.evidence) == 1  # event only
        assert len(result.affected_resources) == 2  # pod + deployment

    def test_explain_without_deployment(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="standalone-pod")

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[],
            objects_queried=0,
            duration_ms=0.5,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R01_oom_killed"
        assert "standalone-pod" in result.root_cause
        assert len(result.affected_resources) == 1  # pod only
        assert "kubectl top pod" in result.suggested_remediation

    def test_explain_confidence_capped_at_095(self) -> None:
        rule = OOMKilledRule()
        event = _make_event(name="my-app-7b4f8c6d-x2kj", count=5)
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        memory_change = _make_field_change()

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[memory_change],
            related_events=[],
            related_resources=[deploy],
            objects_queried=1,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.confidence <= 0.95


class TestR01Helpers:
    """Tests for R01 helper functions."""

    def test_is_memory_change_true(self) -> None:
        fc = _make_field_change(
            field_path="spec.template.spec.containers[0].resources.limits.memory",
        )
        assert _is_memory_change(fc) is True

    def test_is_memory_change_false_image(self) -> None:
        fc = _make_field_change(
            field_path="spec.template.spec.containers[0].image",
        )
        assert _is_memory_change(fc) is False

    def test_is_memory_change_false_unrelated(self) -> None:
        fc = _make_field_change(
            field_path="metadata.labels.app",
        )
        assert _is_memory_change(fc) is False

    def test_find_owning_deployment_found(self) -> None:
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            "ReplicaSet": [rs],
            "Deployment": [deploy],
        }.get(kind, [])

        result = _find_owning_deployment("my-app-7b4f8c6d-x2kj", "default", cache)

        assert result is not None
        assert result.name == "my-app"

    def test_find_owning_deployment_no_replicaset(self) -> None:
        cache = _make_cache()
        result = _find_owning_deployment("orphan-pod", "default", cache)
        assert result is None

    def test_find_owning_deployment_no_deployment(self) -> None:
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            "ReplicaSet": [rs],
            "Deployment": [],
        }.get(kind, [])

        result = _find_owning_deployment("my-app-7b4f8c6d-x2kj", "default", cache)

        assert result is None

    def test_current_memory_limit_found(self) -> None:
        deploy = _make_resource_view(
            kind="Deployment",
            spec={
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {"limits": {"memory": "256Mi"}},
                            }
                        ]
                    }
                }
            },
        )
        assert _current_memory_limit(deploy) == "256Mi"

    def test_current_memory_limit_no_containers(self) -> None:
        deploy = _make_resource_view(kind="Deployment", spec={})
        assert _current_memory_limit(deploy) == "<unknown>"

    def test_current_memory_limit_no_limits(self) -> None:
        deploy = _make_resource_view(
            kind="Deployment",
            spec={
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {"requests": {"memory": "128Mi"}},
                            }
                        ]
                    }
                }
            },
        )
        assert _current_memory_limit(deploy) == "<unknown>"

    def test_current_memory_limit_empty_spec_fields(self) -> None:
        deploy = _make_resource_view(
            kind="Deployment",
            spec={"template": {"spec": {"containers": []}}},
        )
        assert _current_memory_limit(deploy) == "<unknown>"

    def test_current_memory_limit_non_dict_container(self) -> None:
        deploy = _make_resource_view(
            kind="Deployment",
            spec={"template": {"spec": {"containers": ["not_a_dict"]}}},
        )
        assert _current_memory_limit(deploy) == "<unknown>"

    def test_container_name_from_path_found(self) -> None:
        result = _container_name_from_path(
            "spec.template.spec.containers[0].resources.limits.memory"
        )
        assert result == "containers[0]"

    def test_container_name_from_path_no_containers(self) -> None:
        result = _container_name_from_path("spec.replicas")
        assert result == "the affected container"

    def test_container_name_from_path_empty(self) -> None:
        result = _container_name_from_path("")
        assert result == "the affected container"


class TestR01RuleAttributes:
    """Tests for OOMKilledRule class attributes."""

    def test_rule_id(self) -> None:
        assert OOMKilledRule.rule_id == "R01_oom_killed"

    def test_priority(self) -> None:
        assert OOMKilledRule.priority == 10

    def test_base_confidence(self) -> None:
        assert OOMKilledRule.base_confidence == 0.55


# =====================================================================
# R03 FailedSchedulingRule
# =====================================================================


class TestR03Match:
    """Tests for FailedSchedulingRule.match()."""

    def test_matches_failedscheduling(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(reason="FailedScheduling")
        assert rule.match(event) is True

    def test_no_match_oomkilled(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(reason="OOMKilled")
        assert rule.match(event) is False

    def test_no_match_empty(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(reason="")
        assert rule.match(event) is False


class TestR03ClassifySchedulerMessage:
    """Tests for _classify_scheduler_message()."""

    def test_insufficient_cpu(self) -> None:
        msg = "0/3 nodes are available: 3 Insufficient cpu."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.INSUFFICIENT_RESOURCES

    def test_insufficient_memory(self) -> None:
        msg = "0/3 nodes are available: 3 Insufficient memory."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.INSUFFICIENT_RESOURCES

    def test_insufficient_ephemeral_storage(self) -> None:
        msg = "0/2 nodes are available: 2 Insufficient ephemeral-storage."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.INSUFFICIENT_RESOURCES

    def test_node_selector_mismatch_affinity(self) -> None:
        msg = "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.NODE_SELECTOR_MISMATCH

    def test_node_selector_mismatch_selector(self) -> None:
        msg = "0/3 nodes are available: 3 node(s) didn't match node selector."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.NODE_SELECTOR_MISMATCH

    def test_taint_toleration(self) -> None:
        msg = "0/3 nodes are available: 3 node(s) had taint {node.kubernetes.io/not-ready: }, that the pod didn't tolerate."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.TAINT_TOLERATION

    def test_pvc_not_found(self) -> None:
        msg = "0/3 nodes are available: persistentvolumeclaim \"my-pvc\" not found."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.PVC_BINDING

    def test_pvc_unbound(self) -> None:
        msg = "0/2 nodes are available: persistentvolumeclaim \"data-pvc\" unbound."
        assert _classify_scheduler_message(msg) == _SchedulerPattern.PVC_BINDING

    def test_unknown_pattern(self) -> None:
        msg = "Some completely unrecognized scheduling failure message"
        assert _classify_scheduler_message(msg) == _SchedulerPattern.UNKNOWN

    def test_empty_message(self) -> None:
        assert _classify_scheduler_message("") == _SchedulerPattern.UNKNOWN


class TestR03IsSchedulingRelevant:
    """Tests for _is_scheduling_relevant()."""

    def test_nodeselector_relevant(self) -> None:
        fc = _make_field_change(field_path="spec.template.spec.nodeSelector.zone")
        assert _is_scheduling_relevant(fc) is True

    def test_affinity_relevant(self) -> None:
        fc = _make_field_change(field_path="spec.template.spec.affinity.nodeAffinity")
        assert _is_scheduling_relevant(fc) is True

    def test_tolerations_relevant(self) -> None:
        fc = _make_field_change(field_path="spec.template.spec.tolerations[0].key")
        assert _is_scheduling_relevant(fc) is True

    def test_resource_requests_relevant(self) -> None:
        fc = _make_field_change(field_path="spec.template.spec.containers[0].resources.requests.cpu")
        assert _is_scheduling_relevant(fc) is True

    def test_resource_limits_relevant(self) -> None:
        fc = _make_field_change(field_path="spec.template.spec.containers[0].resources.limits.memory")
        assert _is_scheduling_relevant(fc) is True

    def test_image_not_relevant(self) -> None:
        fc = _make_field_change(field_path="spec.template.spec.containers[0].image")
        assert _is_scheduling_relevant(fc) is False


class TestR03Correlate:
    """Tests for FailedSchedulingRule.correlate()."""

    def test_correlate_insufficient_resources(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 Insufficient cpu.",
            name="my-app-7b4f8c6d-x2kj",
        )
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        node = _make_resource_view(kind="Node", namespace="", name="node-1")

        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            ("Node", ""): [node],
            ("ReplicaSet", "default"): [rs],
            ("Deployment", "default"): [deploy],
        }.get((kind, ns), [])

        ledger = _make_ledger()

        result = rule.correlate(event, cache, ledger)

        assert result.objects_queried > 0
        assert result.duration_ms >= 0
        # Related resources include nodes + deployment
        resource_kinds = {r.kind for r in result.related_resources}
        assert "Node" in resource_kinds
        assert "Deployment" in resource_kinds

    def test_correlate_unknown_pattern(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="Unrecognized scheduler message",
            name="my-app-pod",
        )
        cache = _make_cache()
        ledger = _make_ledger()

        result = rule.correlate(event, cache, ledger)

        assert result.objects_queried == 0
        assert result.changes == []
        assert result.related_resources == []

    def test_correlate_pvc_binding_includes_unbound_pvcs(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message='persistentvolumeclaim "my-pvc" not found.',
            name="my-app-7b4f8c6d-x2kj",
        )
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        node = _make_resource_view(kind="Node", namespace="", name="node-1")
        pvc_pending = _make_resource_view(
            kind="PersistentVolumeClaim",
            name="my-pvc",
            status={"phase": "Pending"},
        )
        pvc_bound = _make_resource_view(
            kind="PersistentVolumeClaim",
            name="other-pvc",
            status={"phase": "Bound"},
        )

        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            ("Node", ""): [node],
            ("ReplicaSet", "default"): [rs],
            ("Deployment", "default"): [deploy],
            ("PersistentVolumeClaim", "default"): [pvc_pending, pvc_bound],
        }.get((kind, ns), [])

        ledger = _make_ledger()

        result = rule.correlate(event, cache, ledger)

        pvc_names = [r.name for r in result.related_resources if r.kind == "PersistentVolumeClaim"]
        assert "my-pvc" in pvc_names
        assert "other-pvc" not in pvc_names

    def test_correlate_with_scheduling_relevant_changes(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 Insufficient memory.",
            name="my-app-7b4f8c6d-x2kj",
        )
        rs = _make_resource_view(kind="ReplicaSet", name="my-app-7b4f8c6d")
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        node = _make_resource_view(kind="Node", namespace="", name="node-1")

        cache = MagicMock()
        cache.list.side_effect = lambda kind, ns="": {
            ("Node", ""): [node],
            ("ReplicaSet", "default"): [rs],
            ("Deployment", "default"): [deploy],
        }.get((kind, ns), [])

        req_change = _make_field_change(
            field_path="spec.template.spec.containers[0].resources.requests.memory",
            old_value="128Mi",
            new_value="8Gi",
        )
        image_change = _make_field_change(
            field_path="spec.template.spec.containers[0].image",
            old_value="app:v1",
            new_value="app:v2",
        )
        ledger = _make_ledger(diff_return=[req_change, image_change])

        result = rule.correlate(event, cache, ledger)

        # Only the resource request change should be included
        assert len(result.changes) == 1
        assert "resources.requests" in result.changes[0].field_path


class TestR03Explain:
    """Tests for FailedSchedulingRule.explain()."""

    def test_explain_insufficient_resources(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 Insufficient cpu.",
            name="my-app-pod",
        )
        node = _make_resource_view(kind="Node", namespace="", name="node-1")

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[node],
            objects_queried=5,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R03_failed_scheduling"
        assert "cpu" in result.root_cause.lower()
        assert "insufficient" in result.root_cause.lower()
        assert result.confidence > 0
        assert result.confidence <= 0.95

    def test_explain_node_selector_mismatch(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector.",
            name="my-app-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[],
            objects_queried=0,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R03_failed_scheduling"
        assert "nodeselector" in result.root_cause.lower() or "affinity" in result.root_cause.lower()

    def test_explain_taint_toleration(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 node(s) had taint {key: value}, that the pod didn't tolerate.",
            name="my-app-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[],
            objects_queried=0,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R03_failed_scheduling"
        assert "taint" in result.root_cause.lower()

    def test_explain_pvc_binding(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message='persistentvolumeclaim "my-pvc" not found.',
            name="my-app-pod",
        )
        pvc = _make_resource_view(kind="PersistentVolumeClaim", name="my-pvc", status={"phase": "Pending"})

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[pvc],
            objects_queried=3,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R03_failed_scheduling"
        assert "pvc" in result.root_cause.lower() or "persistentvolumeclaim" in result.root_cause.lower()

    def test_explain_unknown_pattern_uses_base_confidence(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="Unrecognized scheduler message",
            name="my-app-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[],
            related_events=[],
            related_resources=[],
            objects_queried=0,
            duration_ms=1.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R03_failed_scheduling"
        assert result.confidence == rule.base_confidence

    def test_explain_with_scheduling_change_evidence(self) -> None:
        rule = FailedSchedulingRule()
        event = _make_event(
            reason="FailedScheduling",
            message="0/2 nodes are available: 2 Insufficient memory.",
            name="my-app-7b4f8c6d-x2kj",
        )
        deploy = _make_resource_view(kind="Deployment", name="my-app")
        change = _make_field_change(
            field_path="spec.template.spec.containers[0].resources.requests.memory",
            old_value="128Mi",
            new_value="64Gi",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[change],
            related_events=[],
            related_resources=[deploy],
            objects_queried=10,
            duration_ms=2.0,
        )

        result = rule.explain(event, correlation)

        assert result.rule_id == "R03_failed_scheduling"
        assert len(result.evidence) == 2  # event + change
        # Deployment should be in affected resources
        deploy_affected = [r for r in result.affected_resources if r.kind == "Deployment"]
        assert len(deploy_affected) == 1


class TestR03BuildDiagnosis:
    """Tests for _build_diagnosis() helper."""

    def test_insufficient_resources_diagnosis(self) -> None:
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 Insufficient cpu.",
            name="my-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[], related_events=[], related_resources=[], objects_queried=0, duration_ms=0
        )

        root_cause, remediation = _build_diagnosis(
            event=event,
            pattern=_SchedulerPattern.INSUFFICIENT_RESOURCES,
            node_count=3,
            correlation=correlation,
            deploy=None,
            node_truncated=False,
        )

        assert "insufficient" in root_cause.lower()
        assert "cpu" in root_cause.lower()
        assert "3 node" in root_cause
        assert "kubectl top nodes" in remediation

    def test_node_selector_diagnosis(self) -> None:
        event = _make_event(
            reason="FailedScheduling",
            message="0/3 nodes are available: 3 node(s) didn't match node selector.",
            name="my-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[], related_events=[], related_resources=[], objects_queried=0, duration_ms=0
        )

        root_cause, remediation = _build_diagnosis(
            event=event,
            pattern=_SchedulerPattern.NODE_SELECTOR_MISMATCH,
            node_count=3,
            correlation=correlation,
            deploy=None,
            node_truncated=False,
        )

        assert "nodeselector" in root_cause.lower() or "affinity" in root_cause.lower()
        assert "kubectl get nodes" in remediation

    def test_taint_toleration_diagnosis(self) -> None:
        event = _make_event(
            reason="FailedScheduling",
            message="had taint that the pod didn't tolerate",
            name="my-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[], related_events=[], related_resources=[], objects_queried=0, duration_ms=0
        )

        root_cause, remediation = _build_diagnosis(
            event=event,
            pattern=_SchedulerPattern.TAINT_TOLERATION,
            node_count=2,
            correlation=correlation,
            deploy=None,
            node_truncated=False,
        )

        assert "taint" in root_cause.lower()
        assert "toleration" in remediation.lower() or "taint" in remediation.lower()

    def test_pvc_binding_diagnosis(self) -> None:
        event = _make_event(
            reason="FailedScheduling",
            message='persistentvolumeclaim "data" not found',
            name="my-pod",
        )
        pvc = _make_resource_view(kind="PersistentVolumeClaim", name="data")

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[], related_events=[], related_resources=[pvc], objects_queried=0, duration_ms=0
        )

        root_cause, remediation = _build_diagnosis(
            event=event,
            pattern=_SchedulerPattern.PVC_BINDING,
            node_count=0,
            correlation=correlation,
            deploy=None,
            node_truncated=False,
        )

        assert "persistentvolumeclaim" in root_cause.lower()
        assert "data" in root_cause
        assert "kubectl get pvc" in remediation

    def test_unknown_pattern_diagnosis(self) -> None:
        event = _make_event(
            reason="FailedScheduling",
            message="weird scheduler message",
            name="my-pod",
            namespace="prod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[], related_events=[], related_resources=[], objects_queried=0, duration_ms=0
        )

        root_cause, remediation = _build_diagnosis(
            event=event,
            pattern=_SchedulerPattern.UNKNOWN,
            node_count=0,
            correlation=correlation,
            deploy=None,
            node_truncated=False,
        )

        assert "not in the recognized" in root_cause.lower() or "unknown" in root_cause.lower() or "incomplete" in root_cause.lower()
        assert "kubectl describe pod" in remediation

    def test_truncated_node_note(self) -> None:
        event = _make_event(
            reason="FailedScheduling",
            message="0/10 nodes are available: 10 Insufficient cpu.",
            name="my-pod",
        )

        from kuberca.models.analysis import CorrelationResult

        correlation = CorrelationResult(
            changes=[], related_events=[], related_resources=[], objects_queried=0, duration_ms=0
        )

        root_cause, _ = _build_diagnosis(
            event=event,
            pattern=_SchedulerPattern.INSUFFICIENT_RESOURCES,
            node_count=5,
            correlation=correlation,
            deploy=None,
            node_truncated=True,
        )

        assert "node analysis based on" in root_cause.lower()


class TestR03RuleAttributes:
    """Tests for FailedSchedulingRule class attributes."""

    def test_rule_id(self) -> None:
        assert FailedSchedulingRule.rule_id == "R03_failed_scheduling"

    def test_priority(self) -> None:
        assert FailedSchedulingRule.priority == 20

    def test_base_confidence(self) -> None:
        assert FailedSchedulingRule.base_confidence == 0.60
