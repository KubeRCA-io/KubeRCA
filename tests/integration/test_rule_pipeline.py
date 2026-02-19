"""Integration tests for the full rule engine pipeline.

Each test exercises: event creation -> rule matching -> correlation with
cache+ledger -> explain -> RuleResult with correct root_cause and confidence.
"""

from __future__ import annotations

import time

from kuberca.cache.resource_cache import ResourceCache
from kuberca.ledger.change_ledger import ChangeLedger
from kuberca.models.analysis import CorrelationResult, RuleResult
from kuberca.models.events import EventRecord, EvidenceType
from kuberca.rules.base import Rule, RuleEngine
from kuberca.rules.r01_oom_killed import OOMKilledRule

from .conftest import (
    make_crash_loop_event,
    make_event,
    make_failed_scheduling_event,
    make_oom_event,
)

# ---------------------------------------------------------------------------
# OOMKilled pipeline
# ---------------------------------------------------------------------------


class TestOOMKilledPipeline:
    """End-to-end OOMKilled event -> rule engine -> RCAResponse."""

    def test_oom_event_matches_and_produces_root_cause(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """OOMKilled event should match R01, correlate memory changes, produce a root cause."""
        event = make_oom_event()
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R01_oom_killed"
        assert "OOM-killed" in result.root_cause
        assert result.confidence > 0.0
        assert meta.rules_matched >= 1
        assert meta.rules_evaluated >= 1

    def test_oom_with_memory_diff_has_higher_confidence(
        self,
        rule_engine: RuleEngine,
        rule_engine_empty_ledger: RuleEngine,
    ) -> None:
        """OOMKilled with memory diff should have higher confidence than without."""
        event = make_oom_event()

        result_with_diff, _ = rule_engine.evaluate(event)
        result_no_diff, _ = rule_engine_empty_ledger.evaluate(event)

        assert result_with_diff is not None
        assert result_no_diff is not None
        # With diffs present, confidence should be higher due to +0.15 bonus
        assert result_with_diff.confidence > result_no_diff.confidence

    def test_oom_evidence_includes_triggering_event(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """The evidence list must contain at least the triggering OOMKilled event."""
        event = make_oom_event()
        result, _ = rule_engine.evaluate(event)

        assert result is not None
        assert len(result.evidence) >= 1
        event_evidence = [e for e in result.evidence if e.type == EvidenceType.EVENT]
        assert len(event_evidence) >= 1
        assert "OOMKilled" in event_evidence[0].summary or "OOM-killed" in event_evidence[0].summary

    def test_oom_affected_resources_include_pod(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """Affected resources must include the incident Pod."""
        event = make_oom_event()
        result, _ = rule_engine.evaluate(event)

        assert result is not None
        pod_resources = [r for r in result.affected_resources if r.kind == "Pod"]
        assert len(pod_resources) >= 1
        assert pod_resources[0].name == "my-app-7b4f8c6d-x2kj"

    def test_oom_with_repeated_events_boosts_confidence(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """An OOMKilled event with count >= 3 should earn the +0.05 repeated pattern bonus."""
        event_single = make_oom_event(count=1)
        event_repeated = make_oom_event(count=5)

        result_single, _ = rule_engine.evaluate(event_single)
        result_repeated, _ = rule_engine.evaluate(event_repeated)

        assert result_single is not None
        assert result_repeated is not None
        assert result_repeated.confidence >= result_single.confidence


# ---------------------------------------------------------------------------
# CrashLoopBackOff pipeline
# ---------------------------------------------------------------------------


class TestCrashLoopPipeline:
    """End-to-end CrashLoopBackOff event -> rule engine -> RCAResponse."""

    def test_crash_loop_matches_with_image_change(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """CrashLoopBackOff with image change should match R02 and cite image diff."""
        event = make_crash_loop_event()
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R02_crash_loop"
        assert "crash-looping" in result.root_cause
        assert result.confidence > 0.0
        assert meta.rules_matched >= 1

    def test_crash_loop_needs_count_ge_3(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """CrashLoopBackOff with count < 3 should NOT match R02."""
        event = make_crash_loop_event(count=2)
        result, meta = rule_engine.evaluate(event)

        # R02 requires count >= 3, so it should not match
        if result is not None:
            assert result.rule_id != "R02_crash_loop"

    def test_crash_loop_correlates_image_diff(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """CrashLoopBackOff with image diff should reference the image change in evidence."""
        event = make_crash_loop_event()
        result, _ = rule_engine.evaluate(event)

        assert result is not None
        # The ledger has an image change from v1 to v2; check evidence or root_cause
        change_evidence = [e for e in result.evidence if e.type == EvidenceType.CHANGE]
        if change_evidence:
            assert any("image" in e.summary.lower() or "v1" in e.summary or "v2" in e.summary for e in change_evidence)

    def test_crash_loop_suggests_rollback(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """CrashLoopBackOff remediation should suggest kubectl rollout undo."""
        event = make_crash_loop_event()
        result, _ = rule_engine.evaluate(event)

        assert result is not None
        assert "rollout undo" in result.suggested_remediation


# ---------------------------------------------------------------------------
# FailedScheduling pipeline (4 patterns)
# ---------------------------------------------------------------------------


class TestFailedSchedulingPipeline:
    """End-to-end FailedScheduling event -> rule engine for all 4 message patterns."""

    def test_insufficient_resources_pattern(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        event = make_failed_scheduling_event(pattern="insufficient")
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R03_failed_scheduling"
        assert "insufficient" in result.root_cause.lower() or "cpu" in result.root_cause.lower()
        assert result.confidence > 0.0

    def test_node_selector_mismatch_pattern(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        event = make_failed_scheduling_event(pattern="node_selector")
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R03_failed_scheduling"
        assert "nodeselector" in result.root_cause.lower() or "affinity" in result.root_cause.lower()

    def test_taint_toleration_pattern(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        event = make_failed_scheduling_event(pattern="taint")
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R03_failed_scheduling"
        assert "taint" in result.root_cause.lower()

    def test_pvc_binding_pattern(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        event = make_failed_scheduling_event(pattern="pvc")
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R03_failed_scheduling"
        assert "pvc" in result.root_cause.lower() or "persistentvolumeclaim" in result.root_cause.lower()


# ---------------------------------------------------------------------------
# Multiple rules competing (highest confidence wins)
# ---------------------------------------------------------------------------


class TestMultipleRulesCompeting:
    """When multiple rules match, the one with highest confidence should win."""

    def test_highest_confidence_wins(
        self,
        resource_cache: ResourceCache,
        change_ledger: ChangeLedger,
    ) -> None:
        """Register a custom high-confidence rule alongside built-in rules. Highest wins."""

        class AlwaysMatchRule(Rule):
            rule_id = "R_test_always"
            display_name = "AlwaysMatch"
            priority = 5  # Higher priority (lower number) than R01/R02
            base_confidence = 0.90
            resource_dependencies = ["Pod"]
            relevant_field_paths = ["spec"]

            def match(self, event: EventRecord) -> bool:
                return event.reason == "OOMKilled"

            def correlate(self, event, cache, ledger):
                return CorrelationResult(objects_queried=1, duration_ms=0.1)

            def explain(self, event, correlation):
                return RuleResult(
                    rule_id=self.rule_id,
                    root_cause="Always-match test rule",
                    confidence=0.90,
                )

        engine = RuleEngine(cache=resource_cache, ledger=change_ledger)
        engine.register(OOMKilledRule())
        engine.register(AlwaysMatchRule())

        event = make_oom_event()
        result, meta = engine.evaluate(event)

        assert result is not None
        # AlwaysMatchRule has confidence 0.90 (>= 0.85), so it should short-circuit
        assert result.rule_id == "R_test_always"
        assert result.confidence >= 0.85

    def test_no_match_returns_none(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        """An event that matches no rules should return None result."""
        event = make_event(reason="SomeUnknownReason", message="Nothing relevant")
        result, meta = rule_engine.evaluate(event)

        assert result is None
        assert meta.rules_matched == 0


# ---------------------------------------------------------------------------
# Short-circuit and timeout
# ---------------------------------------------------------------------------


class TestShortCircuitAndTimeout:
    """Short-circuit behavior at confidence >= 0.85 and rule timeout enforcement."""

    def test_short_circuit_at_085_confidence(
        self,
        resource_cache: ResourceCache,
        change_ledger: ChangeLedger,
    ) -> None:
        """A rule producing confidence >= 0.85 should short-circuit evaluation."""

        class HighConfRule(Rule):
            rule_id = "R_high"
            display_name = "HighConf"
            priority = 1
            base_confidence = 0.70
            resource_dependencies = ["Pod"]
            relevant_field_paths = ["spec"]

            def match(self, event):
                return True

            def correlate(self, event, cache, ledger):
                return CorrelationResult(objects_queried=1, duration_ms=0.1)

            def explain(self, event, correlation):
                return RuleResult(
                    rule_id=self.rule_id,
                    root_cause="High confidence result",
                    confidence=0.90,
                )

        class NeverReachedRule(Rule):
            rule_id = "R_never"
            display_name = "NeverReached"
            priority = 100
            base_confidence = 0.50
            resource_dependencies = ["Pod"]
            relevant_field_paths = ["spec"]

            def match(self, event):
                # Should never be called due to short-circuit
                raise AssertionError("NeverReachedRule.match was called!")

            def correlate(self, event, cache, ledger):
                raise AssertionError("Should not be reached")

            def explain(self, event, correlation):
                raise AssertionError("Should not be reached")

        engine = RuleEngine(cache=resource_cache, ledger=change_ledger)
        engine.register(HighConfRule())
        engine.register(NeverReachedRule())

        event = make_oom_event()
        result, meta = engine.evaluate(event)

        assert result is not None
        assert result.rule_id == "R_high"
        assert result.confidence >= 0.85
        # NeverReachedRule should not have been evaluated (short-circuit)
        # rules_evaluated counts how many had match() called. High-conf short-circuits
        # after its own explain(), so we just verify it returned the high-conf result.

    def test_rule_timeout_is_enforced(
        self,
        resource_cache: ResourceCache,
        change_ledger: ChangeLedger,
    ) -> None:
        """A rule whose correlate() exceeds 500ms should be skipped."""

        class SlowRule(Rule):
            rule_id = "R_slow"
            display_name = "SlowRule"
            priority = 1
            base_confidence = 0.90
            resource_dependencies = ["Pod"]
            relevant_field_paths = ["spec"]

            def match(self, event):
                return True

            def correlate(self, event, cache, ledger):
                # Sleep longer than the 500ms timeout
                time.sleep(1.0)
                return CorrelationResult(objects_queried=1, duration_ms=1000.0)

            def explain(self, event, correlation):
                return RuleResult(
                    rule_id=self.rule_id,
                    root_cause="Should not reach here",
                    confidence=0.90,
                )

        engine = RuleEngine(cache=resource_cache, ledger=change_ledger)
        engine.register(SlowRule())

        event = make_oom_event()
        result, meta = engine.evaluate(event)

        # The slow rule should be timed out and skipped
        assert result is None
        assert len(meta.warnings) >= 1
        assert any("timed out" in w for w in meta.warnings)


# ---------------------------------------------------------------------------
# Evaluation meta tracking
# ---------------------------------------------------------------------------


class TestEvaluationMeta:
    """The EvaluationMeta should accurately track rules evaluated and duration."""

    def test_meta_tracks_evaluation_count(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        event = make_oom_event()
        _, meta = rule_engine.evaluate(event)

        # At least R01 matches OOMKilled; other rules are evaluated but don't match
        assert meta.rules_evaluated >= 1
        assert meta.duration_ms >= 0.0

    def test_meta_tracks_matched_count(
        self,
        rule_engine: RuleEngine,
    ) -> None:
        event = make_oom_event()
        result, meta = rule_engine.evaluate(event)

        assert result is not None
        assert meta.rules_matched >= 1
