"""Shared fixtures for KubeRCA integration tests.

Provides pre-configured components (cache, ledger, rule engine, coordinator)
wired together with realistic test data so integration tests can exercise
full pipelines without touching real Kubernetes clusters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from kuberca.cache.resource_cache import ResourceCache
from kuberca.ledger.change_ledger import ChangeLedger
from kuberca.models.config import KubeRCAConfig, OllamaConfig, RuleEngineConfig
from kuberca.models.events import EventRecord, EventSource, Severity
from kuberca.models.resources import ResourceSnapshot
from kuberca.rules.base import RuleEngine
from kuberca.rules.r01_oom_killed import OOMKilledRule
from kuberca.rules.r02_crash_loop import CrashLoopRule
from kuberca.rules.r03_failed_scheduling import FailedSchedulingRule

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

# Use timezone-aware UTC datetimes matching the ledger/diff convention.
_NOW = datetime.now(UTC)
_30_MIN_AGO = _NOW - timedelta(minutes=30)
_1H_AGO = _NOW - timedelta(hours=1)
_2H_AGO = _NOW - timedelta(hours=2)


# ---------------------------------------------------------------------------
# Event factory helpers
# ---------------------------------------------------------------------------


def make_event(
    reason: str = "OOMKilled",
    message: str = "Container was OOM-killed",
    resource_kind: str = "Pod",
    resource_name: str = "my-app-7b4f8c6d-x2kj",
    namespace: str = "default",
    severity: Severity = Severity.WARNING,
    count: int = 1,
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
    event_id: str = "",
    labels: dict[str, str] | None = None,
) -> EventRecord:
    """Create an EventRecord with sensible defaults for testing."""
    return EventRecord(
        source=EventSource.CORE_EVENT,
        severity=severity,
        reason=reason,
        message=message,
        namespace=namespace,
        resource_kind=resource_kind,
        resource_name=resource_name,
        first_seen=first_seen or _1H_AGO,
        last_seen=last_seen or _NOW,
        event_id=event_id or f"test-{reason}-{resource_name}",
        cluster_id="test-cluster",
        labels=labels or {},
        count=count,
    )


def make_oom_event(**kwargs) -> EventRecord:
    """Create an OOMKilled event."""
    defaults = {
        "reason": "OOMKilled",
        "message": "Container my-app was OOM-killed",
        "resource_kind": "Pod",
        "resource_name": "my-app-7b4f8c6d-x2kj",
        "namespace": "default",
        "severity": Severity.ERROR,
        "count": 1,
    }
    defaults.update(kwargs)
    return make_event(**defaults)


def make_crash_loop_event(**kwargs) -> EventRecord:
    """Create a CrashLoopBackOff event."""
    defaults = {
        "reason": "BackOff",
        "message": "Back-off restarting failed container",
        "resource_kind": "Pod",
        "resource_name": "my-app-7b4f8c6d-x2kj",
        "namespace": "default",
        "severity": Severity.WARNING,
        "count": 5,
    }
    defaults.update(kwargs)
    return make_event(**defaults)


def make_failed_scheduling_event(pattern: str = "insufficient", **kwargs) -> EventRecord:
    """Create a FailedScheduling event for a recognized pattern."""
    messages = {
        "insufficient": "0/3 nodes are available: 3 Insufficient cpu.",
        "node_selector": "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector.",
        "taint": "0/3 nodes are available: 3 node(s) had taint {key: dedicated} that the pod didn't tolerate.",
        "pvc": "persistentvolumeclaim 'data-vol' not found",
    }
    defaults = {
        "reason": "FailedScheduling",
        "message": messages.get(pattern, messages["insufficient"]),
        "resource_kind": "Pod",
        "resource_name": "my-app-7b4f8c6d-x2kj",
        "namespace": "default",
        "severity": Severity.WARNING,
        "count": 3,
    }
    defaults.update(kwargs)
    return make_event(**defaults)


# ---------------------------------------------------------------------------
# Cache fixtures
# ---------------------------------------------------------------------------


def _populate_cache_with_test_data(cache: ResourceCache) -> None:
    """Populate a ResourceCache with realistic Kubernetes objects."""
    # Register the kinds so the cache can transition from WARMING to READY
    cache._all_kinds = {"Pod", "Deployment", "ReplicaSet", "Node", "Service", "ConfigMap", "PersistentVolumeClaim"}

    # Deployment: my-app
    cache.update(
        "Deployment",
        "default",
        "my-app",
        {
            "metadata": {
                "name": "my-app",
                "namespace": "default",
                "resourceVersion": "1001",
                "labels": {"app": "my-app"},
                "annotations": {"kubernetes.io/change-cause": "image update"},
            },
            "spec": {
                "replicas": 3,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "my-app",
                                "image": "my-app:v2",
                                "resources": {
                                    "limits": {"memory": "256Mi", "cpu": "500m"},
                                    "requests": {"memory": "128Mi", "cpu": "250m"},
                                },
                                "env": [
                                    {"name": "DB_HOST", "value": "db.svc"},
                                    {"name": "DB_PASSWORD", "value": "secret123"},
                                ],
                            }
                        ],
                    },
                },
            },
            "status": {"availableReplicas": 3, "readyReplicas": 3},
        },
    )

    # ReplicaSet: my-app-7b4f8c6d
    cache.update(
        "ReplicaSet",
        "default",
        "my-app-7b4f8c6d",
        {
            "metadata": {
                "name": "my-app-7b4f8c6d",
                "namespace": "default",
                "resourceVersion": "1002",
                "labels": {"app": "my-app"},
                "annotations": {},
            },
            "spec": {"replicas": 3},
            "status": {"readyReplicas": 3},
        },
    )

    # Pod: my-app-7b4f8c6d-x2kj
    cache.update(
        "Pod",
        "default",
        "my-app-7b4f8c6d-x2kj",
        {
            "metadata": {
                "name": "my-app-7b4f8c6d-x2kj",
                "namespace": "default",
                "resourceVersion": "1003",
                "labels": {"app": "my-app"},
                "annotations": {},
            },
            "spec": {
                "containers": [
                    {
                        "name": "my-app",
                        "image": "my-app:v2",
                        "resources": {
                            "limits": {"memory": "256Mi"},
                            "requests": {"memory": "128Mi"},
                        },
                    }
                ],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "my-app",
                        "ready": True,
                        "restartCount": 0,
                        "state": {"running": {"startedAt": _NOW.isoformat()}},
                    }
                ],
            },
        },
    )

    # Nodes (3 nodes)
    for i in range(3):
        cache.update(
            "Node",
            "",
            f"node-{i}",
            {
                "metadata": {
                    "name": f"node-{i}",
                    "resourceVersion": f"200{i}",
                    "labels": {"kubernetes.io/hostname": f"node-{i}"},
                    "annotations": {},
                },
                "spec": {},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                    ],
                    "allocatable": {"cpu": "4", "memory": "8Gi"},
                },
            },
        )

    # Service
    cache.update(
        "Service",
        "default",
        "my-app-svc",
        {
            "metadata": {
                "name": "my-app-svc",
                "namespace": "default",
                "resourceVersion": "3001",
                "labels": {"app": "my-app"},
                "annotations": {},
            },
            "spec": {"type": "ClusterIP", "ports": [{"port": 80, "targetPort": 8080}]},
            "status": {},
        },
    )

    # ConfigMap
    cache.update(
        "ConfigMap",
        "default",
        "my-app-config",
        {
            "metadata": {
                "name": "my-app-config",
                "namespace": "default",
                "resourceVersion": "4001",
                "labels": {},
                "annotations": {},
            },
            "spec": {},
            "status": {},
        },
    )

    # PersistentVolumeClaim (unbound for PVC test)
    cache.update(
        "PersistentVolumeClaim",
        "default",
        "data-vol",
        {
            "metadata": {
                "name": "data-vol",
                "namespace": "default",
                "resourceVersion": "5001",
                "labels": {},
                "annotations": {},
            },
            "spec": {"storageClassName": "standard", "resources": {"requests": {"storage": "10Gi"}}},
            "status": {"phase": "Pending"},
        },
    )

    # Mark all kinds as ready
    cache._ready_kinds = set(cache._all_kinds)
    cache._recompute_readiness()


@pytest.fixture()
def resource_cache() -> ResourceCache:
    """Pre-populated ResourceCache in READY state."""
    cache = ResourceCache()
    _populate_cache_with_test_data(cache)
    return cache


# ---------------------------------------------------------------------------
# Ledger fixtures
# ---------------------------------------------------------------------------


def _populate_ledger_with_test_data(ledger: ChangeLedger) -> None:
    """Record test snapshots into the ChangeLedger for realistic diffs."""
    now = datetime.now(UTC)
    one_hour_ago = now - timedelta(hours=1)

    # Two snapshots for my-app Deployment: memory limit and image changed.
    # The spec stored in ResourceSnapshot represents the full Kubernetes object
    # starting with the "spec" key, so diff paths will be like
    # "spec.template.spec.containers[0].resources.limits.memory" which matches
    # the OOM rule's _MEMORY_FIELD_PREFIX.
    ledger.record(
        ResourceSnapshot(
            kind="Deployment",
            namespace="default",
            name="my-app",
            spec_hash="hash_old",
            spec={
                "spec": {
                    "replicas": 3,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "my-app",
                                    "image": "my-app:v1",
                                    "resources": {
                                        "limits": {"memory": "128Mi", "cpu": "500m"},
                                        "requests": {"memory": "64Mi", "cpu": "250m"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
            captured_at=one_hour_ago,
            resource_version="999",
        )
    )
    ledger.record(
        ResourceSnapshot(
            kind="Deployment",
            namespace="default",
            name="my-app",
            spec_hash="hash_new",
            spec={
                "spec": {
                    "replicas": 3,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "my-app",
                                    "image": "my-app:v2",
                                    "resources": {
                                        "limits": {"memory": "256Mi", "cpu": "500m"},
                                        "requests": {"memory": "128Mi", "cpu": "250m"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
            captured_at=now - timedelta(minutes=15),
            resource_version="1001",
        )
    )


@pytest.fixture()
def change_ledger() -> ChangeLedger:
    """Pre-populated ChangeLedger with test diffs."""
    ledger = ChangeLedger(max_versions=10, retention_hours=6)
    _populate_ledger_with_test_data(ledger)
    return ledger


@pytest.fixture()
def empty_ledger() -> ChangeLedger:
    """Empty ChangeLedger with no recorded snapshots."""
    return ChangeLedger(max_versions=10, retention_hours=6)


# ---------------------------------------------------------------------------
# Rule engine fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rule_engine(resource_cache: ResourceCache, change_ledger: ChangeLedger) -> RuleEngine:
    """RuleEngine wired with pre-populated cache and ledger, all Tier 1 rules registered."""
    engine = RuleEngine(cache=resource_cache, ledger=change_ledger)
    engine.register(OOMKilledRule())
    engine.register(CrashLoopRule())
    engine.register(FailedSchedulingRule())
    return engine


@pytest.fixture()
def rule_engine_empty_ledger(resource_cache: ResourceCache, empty_ledger: ChangeLedger) -> RuleEngine:
    """RuleEngine wired with cache but an empty ledger (no diffs)."""
    engine = RuleEngine(cache=resource_cache, ledger=empty_ledger)
    engine.register(OOMKilledRule())
    engine.register(CrashLoopRule())
    engine.register(FailedSchedulingRule())
    return engine


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def kuberca_config() -> KubeRCAConfig:
    """Default KubeRCAConfig for integration tests."""
    return KubeRCAConfig(
        cluster_id="test-cluster",
        ollama=OllamaConfig(enabled=False),
        rule_engine=RuleEngineConfig(time_window="2h", tier2_enabled=True),
    )


# ---------------------------------------------------------------------------
# Mock event buffer fixture
# ---------------------------------------------------------------------------


def make_event_buffer(events: list[EventRecord] | None = None) -> MagicMock:
    """Create a mock event buffer that returns provided events."""
    buffer = MagicMock()

    def _get_events(kind: str, namespace: str, name: str, time_window: str = "2h") -> list[EventRecord]:
        if events is None:
            return []
        return [e for e in events if e.resource_kind == kind and e.namespace == namespace and e.resource_name == name]

    buffer.get_events = MagicMock(side_effect=_get_events)
    return buffer


@pytest.fixture()
def event_buffer() -> MagicMock:
    """Empty event buffer mock."""
    return make_event_buffer()
