"""Integration tests for the ResourceCache state machine.

Exercises the 4-state readiness model (WARMING -> PARTIALLY_READY -> READY -> DEGRADED)
and divergence detection triggers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kuberca.cache.resource_cache import ResourceCache
from kuberca.models.resources import CacheReadiness

# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_initial_state_is_warming(self) -> None:
        """A freshly created cache should be in WARMING state."""
        cache = ResourceCache()
        assert cache.readiness() == CacheReadiness.WARMING

    def test_no_kinds_registered_stays_warming(self) -> None:
        """Without registering any kinds, readiness stays WARMING."""
        cache = ResourceCache()
        cache._recompute_readiness()
        assert cache.readiness() == CacheReadiness.WARMING


# ---------------------------------------------------------------------------
# After populating all kinds -> READY
# ---------------------------------------------------------------------------


class TestReadyState:
    def test_all_kinds_populated_transitions_to_ready(self) -> None:
        """When all registered kinds have data, cache should be READY."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod", "Deployment"}
        cache._ready_kinds = {"Pod", "Deployment"}
        cache._recompute_readiness()
        assert cache.readiness() == CacheReadiness.READY

    def test_single_kind_populated_partially_ready(self) -> None:
        """When only some registered kinds have data, cache should be PARTIALLY_READY."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod", "Deployment", "Service"}
        cache._ready_kinds = {"Pod"}
        cache._recompute_readiness()
        assert cache.readiness() == CacheReadiness.PARTIALLY_READY


# ---------------------------------------------------------------------------
# Miss rate > 5% -> DEGRADED
# ---------------------------------------------------------------------------


class TestMissRateDegradation:
    def test_high_miss_rate_triggers_degraded(self) -> None:
        """A miss rate above 5% over the 5-minute window should trigger DEGRADED."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        # Simulate many misses: get() on absent resources
        for i in range(20):
            cache.get("Pod", "default", f"nonexistent-{i}")

        # The miss rate should now be >5% (100% actually, since all are misses)
        assert cache.readiness() == CacheReadiness.DEGRADED

    def test_low_miss_rate_stays_ready(self) -> None:
        """When miss rate is below 5%, cache should remain READY."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        # Populate 100 pods, then do lookups mostly hitting
        for i in range(100):
            cache.update(
                "Pod",
                "default",
                f"pod-{i}",
                {
                    "metadata": {
                        "name": f"pod-{i}",
                        "namespace": "default",
                        "resourceVersion": "1",
                        "labels": {},
                        "annotations": {},
                    },
                    "spec": {},
                    "status": {},
                },
            )

        # Do 100 hits
        for i in range(100):
            cache.get("Pod", "default", f"pod-{i}")

        # Do 2 misses (2% miss rate, below 5%)
        cache.get("Pod", "default", "nonexistent-1")
        cache.get("Pod", "default", "nonexistent-2")

        assert cache.readiness() == CacheReadiness.READY


# ---------------------------------------------------------------------------
# Reconnect failures > 3 -> DEGRADED
# ---------------------------------------------------------------------------


class TestReconnectFailureDegradation:
    def test_reconnect_failures_above_3_triggers_degraded(self) -> None:
        """More than 3 reconnect failures should transition to DEGRADED."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        # Fire 4 reconnect failures (threshold is > 3)
        for _ in range(4):
            cache.notify_reconnect_failure()

        assert cache.readiness() == CacheReadiness.DEGRADED

    def test_reconnect_failures_at_3_stays_ready(self) -> None:
        """Exactly 3 reconnect failures should NOT trigger DEGRADED (threshold is > 3)."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        for _ in range(3):
            cache.notify_reconnect_failure()

        assert cache.readiness() == CacheReadiness.READY

    def test_reset_reconnect_failures_recovers(self) -> None:
        """After resetting reconnect failures, cache can recover from DEGRADED."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        for _ in range(4):
            cache.notify_reconnect_failure()
        assert cache.readiness() == CacheReadiness.DEGRADED

        cache.reset_reconnect_failures()
        assert cache.readiness() == CacheReadiness.READY


# ---------------------------------------------------------------------------
# PARTIALLY_READY -> DEGRADED after 10 min staleness
# ---------------------------------------------------------------------------


class TestPartiallyReadyStaleness:
    def test_partially_ready_degrades_after_10_minutes(self) -> None:
        """PARTIALLY_READY should transition to DEGRADED after 10 minutes of staleness."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod", "Deployment"}
        cache._ready_kinds = {"Pod"}  # only one kind

        # Set partially_ready_since to 11 minutes ago
        cache._partially_ready_since = datetime.now(tz=UTC) - timedelta(minutes=11)
        cache._recompute_readiness()

        assert cache.readiness() == CacheReadiness.DEGRADED

    def test_partially_ready_stays_if_within_10_minutes(self) -> None:
        """PARTIALLY_READY should remain if staleness is under 10 minutes."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod", "Deployment"}
        cache._ready_kinds = {"Pod"}

        # Set partially_ready_since to 5 minutes ago
        cache._partially_ready_since = datetime.now(tz=UTC) - timedelta(minutes=5)
        cache._recompute_readiness()

        assert cache.readiness() == CacheReadiness.PARTIALLY_READY


# ---------------------------------------------------------------------------
# Readiness getter returns current state enum
# ---------------------------------------------------------------------------


class TestReadinessGetter:
    def test_readiness_returns_cache_readiness_enum(self) -> None:
        """readiness() should return a CacheReadiness enum member."""
        cache = ResourceCache()
        state = cache.readiness()
        assert isinstance(state, CacheReadiness)

    def test_readiness_recomputes_on_each_call(self) -> None:
        """readiness() recomputes the state each time it is called."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        assert cache.readiness() == CacheReadiness.WARMING

        cache._ready_kinds = {"Pod"}
        # After marking ready, next call should return READY
        assert cache.readiness() == CacheReadiness.READY


# ---------------------------------------------------------------------------
# Resource counts tracked per kind
# ---------------------------------------------------------------------------


class TestResourceCounts:
    def test_resource_counts_tracked_per_kind(self) -> None:
        """Each kind's count should be independently tracked."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod", "Service"}
        cache._ready_kinds = {"Pod", "Service"}

        for i in range(5):
            cache.update(
                "Pod",
                "default",
                f"pod-{i}",
                {
                    "metadata": {
                        "name": f"pod-{i}",
                        "namespace": "default",
                        "resourceVersion": "1",
                        "labels": {},
                        "annotations": {},
                    },
                    "spec": {},
                    "status": {},
                },
            )
        for i in range(3):
            cache.update(
                "Service",
                "default",
                f"svc-{i}",
                {
                    "metadata": {
                        "name": f"svc-{i}",
                        "namespace": "default",
                        "resourceVersion": "1",
                        "labels": {},
                        "annotations": {},
                    },
                    "spec": {},
                    "status": {},
                },
            )

        pod_views = cache.list("Pod", "default")
        svc_views = cache.list("Service", "default")

        assert len(pod_views) == 5
        assert len(svc_views) == 3

    def test_remove_decrements_count(self) -> None:
        """Removing a resource should decrement the kind's count."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        for i in range(3):
            cache.update(
                "Pod",
                "default",
                f"pod-{i}",
                {
                    "metadata": {
                        "name": f"pod-{i}",
                        "namespace": "default",
                        "resourceVersion": "1",
                        "labels": {},
                        "annotations": {},
                    },
                    "spec": {},
                    "status": {},
                },
            )

        assert len(cache.list("Pod", "default")) == 3

        cache.remove("Pod", "default", "pod-1")
        assert len(cache.list("Pod", "default")) == 2

    def test_list_truncated_flag(self) -> None:
        """list_truncated should return True when data was capped."""
        cache = ResourceCache()
        cache._truncated["Pod"] = True
        assert cache.list_truncated("Pod") is True
        assert cache.list_truncated("Service") is False
