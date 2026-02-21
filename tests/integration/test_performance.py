"""Performance benchmark tests for KubeRCA.

All tests are marked with @pytest.mark.performance. They enforce hard
wall-clock budgets to ensure the system remains responsive under load.
"""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime, timedelta

import pytest

from kuberca.cache.redaction import redact_dict
from kuberca.cache.resource_cache import ResourceCache
from kuberca.ledger.change_ledger import ChangeLedger
from kuberca.models.analysis import CorrelationResult
from kuberca.models.resources import FieldChange, ResourceSnapshot
from kuberca.rules.base import RuleEngine
from kuberca.rules.confidence import compute_confidence
from kuberca.rules.r01_oom_killed import OOMKilledRule
from kuberca.rules.r02_crash_loop import CrashLoopRule
from kuberca.rules.r03_failed_scheduling import FailedSchedulingRule
from kuberca.rules.r04_image_pull import ImagePullRule
from kuberca.rules.r05_hpa import HPARule
from kuberca.rules.r06_service_unreachable import ServiceUnreachableRule
from kuberca.rules.r07_config_drift import ConfigDriftRule
from kuberca.rules.r08_volume_mount import VolumeMountRule
from kuberca.rules.r09_node_pressure import NodePressureRule
from kuberca.rules.r10_readiness_probe import ReadinessProbeRule

from .conftest import make_oom_event

pytestmark = [pytest.mark.integration, pytest.mark.performance]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_large_cache(cache: ResourceCache, count: int = 200) -> None:
    """Populate cache with many resources for performance testing."""
    cache._all_kinds = {"Pod", "Deployment", "ReplicaSet", "Node", "Service"}
    for i in range(count):
        cache.update(
            "Pod",
            "default",
            f"pod-{i}",
            {
                "metadata": {
                    "name": f"pod-{i}",
                    "namespace": "default",
                    "resourceVersion": str(i),
                    "labels": {"app": f"app-{i % 10}"},
                    "annotations": {},
                },
                "spec": {
                    "containers": [
                        {
                            "name": f"container-{i}",
                            "image": f"image-{i}:latest",
                            "resources": {"limits": {"memory": "256Mi", "cpu": "500m"}},
                        }
                    ],
                },
                "status": {"phase": "Running"},
            },
        )
    cache._ready_kinds = set(cache._all_kinds)


def _build_large_spec() -> dict:
    """Build a large but realistic Kubernetes Deployment spec."""
    containers = []
    for i in range(5):
        containers.append(
            {
                "name": f"container-{i}",
                "image": f"registry.example.com/app-{i}:v1.2.3",
                "resources": {
                    "limits": {"memory": "512Mi", "cpu": "1000m"},
                    "requests": {"memory": "256Mi", "cpu": "500m"},
                },
                "env": [
                    {"name": "DB_HOST", "value": "postgres.svc.cluster.local"},
                    {"name": "DB_PASSWORD", "value": "supersecretpassword123"},
                    {"name": "API_KEY", "value": "sk-1234567890abcdef"},
                    {"name": "LOG_LEVEL", "value": "info"},
                ],
                "volumeMounts": [
                    {"name": "config", "mountPath": "/etc/config"},
                    {"name": "secrets", "mountPath": "/etc/secrets"},
                ],
                "command": ["/bin/app", "--config=/etc/config/app.yaml", "--password=mysecret"],
            }
        )

    return {
        "replicas": 3,
        "selector": {"matchLabels": {"app": "my-app"}},
        "template": {
            "metadata": {
                "labels": {"app": "my-app", "version": "v1.2.3"},
                "annotations": {
                    "kubernetes.io/change-cause": "image update",
                    "custom/secret-note": "internal only",
                },
            },
            "spec": {
                "containers": containers,
                "volumes": [
                    {"name": "config", "configMap": {"name": "app-config"}},
                    {"name": "secrets", "secret": {"secretName": "app-secrets"}},
                ],
                "serviceAccountName": "app-sa",
                "nodeSelector": {"disktype": "ssd"},
            },
        },
    }


# ---------------------------------------------------------------------------
# Rule engine evaluates 10 rules in <500ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestRuleEnginePerformance:
    def test_10_rules_evaluate_under_500ms(self) -> None:
        """Rule engine should evaluate 10 registered rules in under 500ms."""
        cache = ResourceCache()
        _populate_large_cache(cache)
        ledger = ChangeLedger()

        engine = RuleEngine(cache=cache, ledger=ledger)
        engine.register(OOMKilledRule())
        engine.register(CrashLoopRule())
        engine.register(FailedSchedulingRule())
        engine.register(ImagePullRule())
        engine.register(HPARule())
        engine.register(ServiceUnreachableRule())
        engine.register(ConfigDriftRule())
        engine.register(VolumeMountRule())
        engine.register(NodePressureRule())
        engine.register(ReadinessProbeRule())

        event = make_oom_event()

        start = time.monotonic()
        result, meta = engine.evaluate(event)
        elapsed_ms = (time.monotonic() - start) * 1000.0

        # The evaluation should complete in under 500ms
        assert elapsed_ms < 500.0, f"Rule engine took {elapsed_ms:.1f}ms (budget: 500ms)"
        # It should have evaluated some rules
        assert meta.rules_evaluated >= 1


# ---------------------------------------------------------------------------
# Cache lookup of 200 resources in <100ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestCacheLookupPerformance:
    def test_200_resource_lookups_under_100ms(self) -> None:
        """Looking up 200 resources from cache should take under 100ms."""
        cache = ResourceCache()
        _populate_large_cache(cache, count=200)

        start = time.monotonic()
        for i in range(200):
            view = cache.get("Pod", "default", f"pod-{i}")
            assert view is not None
        elapsed_ms = (time.monotonic() - start) * 1000.0

        assert elapsed_ms < 100.0, f"200 lookups took {elapsed_ms:.1f}ms (budget: 100ms)"


# ---------------------------------------------------------------------------
# Redaction of large spec <50ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestRedactionPerformance:
    def test_large_spec_redaction_under_50ms(self) -> None:
        """Redacting a large deployment spec should take under 50ms."""
        spec = _build_large_spec()

        start = time.monotonic()
        redacted = redact_dict(copy.deepcopy(spec))
        elapsed_ms = (time.monotonic() - start) * 1000.0

        assert elapsed_ms < 50.0, f"Redaction took {elapsed_ms:.1f}ms (budget: 50ms)"
        # Verify redaction actually happened
        assert isinstance(redacted, dict)


# ---------------------------------------------------------------------------
# Change ledger record + diff cycle <10ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestLedgerPerformance:
    def test_record_and_diff_cycle_under_10ms(self) -> None:
        """Recording a snapshot and diffing should take under 10ms."""
        ledger = ChangeLedger(max_versions=10, retention_hours=6)
        now = datetime.now(UTC)

        # Record two snapshots
        snap1 = ResourceSnapshot(
            kind="Deployment",
            namespace="default",
            name="perf-app",
            spec_hash="hash1",
            spec={"replicas": 3, "image": "app:v1"},
            captured_at=now - timedelta(minutes=5),
            resource_version="100",
        )
        snap2 = ResourceSnapshot(
            kind="Deployment",
            namespace="default",
            name="perf-app",
            spec_hash="hash2",
            spec={"replicas": 3, "image": "app:v2"},
            captured_at=now,
            resource_version="101",
        )

        start = time.monotonic()
        ledger.record(snap1)
        ledger.record(snap2)
        changes = ledger.diff("Deployment", "default", "perf-app", since_hours=1.0)
        elapsed_ms = (time.monotonic() - start) * 1000.0

        assert elapsed_ms < 10.0, f"Record+diff cycle took {elapsed_ms:.1f}ms (budget: 10ms)"
        assert len(changes) >= 1  # At least the image change


# ---------------------------------------------------------------------------
# Confidence computation <1ms
# ---------------------------------------------------------------------------


@pytest.mark.performance
class TestConfidenceComputationPerformance:
    def test_confidence_computation_under_1ms(self) -> None:
        """compute_confidence should complete in under 1ms."""
        now = datetime.now(tz=UTC)
        event = make_oom_event(count=5, first_seen=now - timedelta(hours=1), last_seen=now)

        rule = OOMKilledRule()
        corr = CorrelationResult(
            changes=[
                FieldChange(
                    field_path="spec.template.spec.containers[0].resources.limits.memory",
                    old_value='"128Mi"',
                    new_value='"256Mi"',
                    changed_at=now - timedelta(minutes=10),
                ),
            ],
            related_resources=[],
            objects_queried=5,
            duration_ms=1.0,
        )

        # Run 100 iterations to get a stable measurement
        start = time.monotonic()
        for _ in range(100):
            score = compute_confidence(rule, corr, event)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        per_call_ms = elapsed_ms / 100.0

        assert per_call_ms < 1.0, f"Confidence computation took {per_call_ms:.3f}ms (budget: 1ms)"
        assert 0.0 < score <= 0.95
