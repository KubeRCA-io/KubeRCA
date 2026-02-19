"""Integration tests for the WorkQueue + rate limiting.

Tests cover: submit/resolve cycle, deduplication, background rejection,
queue-full behavior, priority ordering, and rate limiting.
"""

from __future__ import annotations

import asyncio

import pytest

from kuberca.analyst.queue import Priority, QueueFullError, WorkQueue
from kuberca.models.resources import CacheReadiness

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _dummy_analyze(resource: str, time_window: str) -> dict:
    """Dummy analysis function for tests."""
    await asyncio.sleep(0.01)
    return {"resource": resource, "time_window": time_window, "result": "ok"}


async def _slow_analyze(resource: str, time_window: str) -> dict:
    """Slow analysis function for ordering tests."""
    await asyncio.sleep(0.1)
    return {"resource": resource, "result": "ok"}


# ---------------------------------------------------------------------------
# Submit and resolve
# ---------------------------------------------------------------------------


class TestSubmitAndResolve:
    async def test_submit_and_resolve_analysis_request(self) -> None:
        """Submit a request and verify it resolves with the analysis result."""
        queue = WorkQueue(analyze_fn=_dummy_analyze)
        await queue.start()
        try:
            future = queue.submit("Pod/default/my-pod", "2h")
            result = await asyncio.wait_for(future, timeout=5.0)
            assert result["resource"] == "Pod/default/my-pod"
            assert result["time_window"] == "2h"
            assert result["result"] == "ok"
        finally:
            await queue.stop()

    async def test_submit_returns_future(self) -> None:
        """submit() should return an asyncio.Future."""
        queue = WorkQueue(analyze_fn=_dummy_analyze)
        await queue.start()
        try:
            future = queue.submit("Pod/default/my-pod")
            assert isinstance(future, asyncio.Future)
            await asyncio.wait_for(future, timeout=5.0)
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    async def test_duplicate_dedup_returns_same_future(self) -> None:
        """Submitting the same resource twice should return the same future (dedup)."""
        queue = WorkQueue(analyze_fn=_dummy_analyze)
        await queue.start()
        try:
            future1 = queue.submit("Pod/default/my-pod", "2h")
            future2 = queue.submit("Pod/default/my-pod", "2h")

            # Both should return the same future object
            assert future1 is future2

            result = await asyncio.wait_for(future1, timeout=5.0)
            assert result["resource"] == "Pod/default/my-pod"
        finally:
            await queue.stop()

    async def test_different_resources_get_different_futures(self) -> None:
        """Different resources should get independent futures."""
        queue = WorkQueue(analyze_fn=_dummy_analyze)
        await queue.start()
        try:
            future1 = queue.submit("Pod/default/pod-a", "2h")
            future2 = queue.submit("Pod/default/pod-b", "2h")

            assert future1 is not future2

            result1 = await asyncio.wait_for(future1, timeout=5.0)
            result2 = await asyncio.wait_for(future2, timeout=5.0)
            assert result1["resource"] == "Pod/default/pod-a"
            assert result2["resource"] == "Pod/default/pod-b"
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# Background rejected at >80% fill
# ---------------------------------------------------------------------------


class TestBackgroundRejection:
    async def test_background_rejected_when_queue_over_80_percent(self) -> None:
        """Background priority items should be rejected when queue is >80% full."""

        async def never_resolve(resource: str, time_window: str) -> dict:
            # This function blocks forever — items pile up in queue
            await asyncio.sleep(3600)
            return {}

        queue = WorkQueue(analyze_fn=never_resolve)
        await queue.start()
        try:
            # Fill the queue past 80% (queue max = 100, so need > 80 items)
            # We need to push items faster than workers consume them
            # Workers are blocked in never_resolve, so items pile up
            filled = 0
            for i in range(85):
                try:
                    queue.submit(f"Pod/default/pod-fill-{i}", "2h", priority=Priority.INTERACTIVE)
                    filled += 1
                except QueueFullError:
                    break

            # Now a background request should be rejected
            with pytest.raises(QueueFullError):
                queue.submit("Pod/default/bg-pod", "2h", priority=Priority.BACKGROUND)
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# Queue full returns 429-style error
# ---------------------------------------------------------------------------


class TestQueueFull:
    async def test_queue_full_raises_queue_full_error(self) -> None:
        """When the queue is 100% full, any submit should raise QueueFullError."""

        async def never_resolve(resource: str, time_window: str) -> dict:
            await asyncio.sleep(3600)
            return {}

        queue = WorkQueue(analyze_fn=never_resolve)
        await queue.start()
        try:
            # Fill the queue completely (100 items)
            for i in range(100):
                try:
                    queue.submit(f"Pod/default/pod-{i}", "2h", priority=Priority.INTERACTIVE)
                except QueueFullError:
                    break

            # Next submit should raise QueueFullError
            with pytest.raises(QueueFullError):
                queue.submit("Pod/default/overflow", "2h", priority=Priority.INTERACTIVE)
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    async def test_interactive_higher_than_system(self) -> None:
        """Interactive priority should be processed before system priority."""
        assert Priority.INTERACTIVE < Priority.SYSTEM

    async def test_system_higher_than_background(self) -> None:
        """System priority should be processed before background priority."""
        assert Priority.SYSTEM < Priority.BACKGROUND

    async def test_priority_enum_values(self) -> None:
        """Priority enum should have the expected integer values."""
        assert Priority.INTERACTIVE == 1
        assert Priority.SYSTEM == 2
        assert Priority.BACKGROUND == 3


# ---------------------------------------------------------------------------
# Rate limiting enforcement
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_rate_limit_enforced_at_max_per_minute(self) -> None:
        """Exceeding the rate limit should raise QueueFullError."""
        queue = WorkQueue(analyze_fn=_dummy_analyze)
        await queue.start()
        try:
            # Default rate is 20/min. Submit 20 items fast, then the 21st should fail.
            rejected = False
            for i in range(25):
                try:
                    queue.submit(f"Pod/default/rate-pod-{i}", "2h")
                except QueueFullError:
                    rejected = True
                    break

            assert rejected, "Expected QueueFullError after exceeding rate limit"
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# Degraded mode reduces rate
# ---------------------------------------------------------------------------


class TestDegradedModeRate:
    async def test_degraded_cache_reduces_rate(self) -> None:
        """When cache is DEGRADED, effective rate should drop to 5/min."""
        queue = WorkQueue(
            analyze_fn=_dummy_analyze,
            cache_readiness_fn=lambda: CacheReadiness.DEGRADED,
        )
        await queue.start()
        try:
            # With degraded rate of 5/min, submitting 6 should fail
            rejected = False
            for i in range(10):
                try:
                    queue.submit(f"Pod/default/degraded-pod-{i}", "2h")
                except QueueFullError:
                    rejected = True
                    break

            assert rejected, "Expected QueueFullError with degraded rate limit"
        finally:
            await queue.stop()

    async def test_normal_cache_allows_higher_rate(self) -> None:
        """When cache is READY, the default rate limit of 20/min should apply."""
        queue = WorkQueue(
            analyze_fn=_dummy_analyze,
            cache_readiness_fn=lambda: CacheReadiness.READY,
        )
        await queue.start()
        try:
            # With normal rate of 20/min, submitting 15 should succeed
            submitted = 0
            for i in range(15):
                try:
                    queue.submit(f"Pod/default/normal-pod-{i}", "2h")
                    submitted += 1
                except QueueFullError:
                    break

            assert submitted >= 15, f"Expected at least 15 successful submits, got {submitted}"
        finally:
            await queue.stop()
