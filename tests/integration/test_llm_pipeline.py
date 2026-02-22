"""Integration tests for the LLM analysis pipeline.

All HTTP calls to Ollama are mocked. Tests exercise the full flow:
evidence assembly -> prompt construction -> HTTP call -> JSON parse ->
confidence computation -> LLMResult.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from kuberca.llm.analyzer import LLMAnalyzer, _compute_confidence
from kuberca.llm.evidence import MAX_PROMPT_BYTES, EvidencePackage
from kuberca.models.config import OllamaConfig
from kuberca.models.resources import FieldChange

from .conftest import make_event, make_oom_event

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ollama_config() -> OllamaConfig:
    return OllamaConfig(
        enabled=True,
        endpoint="http://localhost:11434",
        model="qwen2.5:7b",
        timeout_seconds=30,
        max_retries=3,
        temperature=0.1,
        max_tokens=2048,
    )


def _make_successful_llm_response() -> dict:
    """Return a valid LLM JSON response with all required keys."""
    return {
        "root_cause": "Container exceeded memory limit due to memory leak in request handler.",
        "evidence_citations": [
            "2024-01-15T10:30:00Z OOMKilled event on pod my-app-7b4f8c6d-x2kj",
            "2024-01-15T10:25:00Z Memory limit changed from 128Mi to 256Mi",
            "2024-01-15T10:20:00Z High memory usage warning",
        ],
        "affected_resources": [
            {"kind": "Pod", "namespace": "default", "name": "my-app-7b4f8c6d-x2kj"},
            {"kind": "Deployment", "namespace": "default", "name": "my-app"},
        ],
        "suggested_remediation": "kubectl set resources deployment/my-app -c my-app --limits=memory=512Mi",
        "causal_chain": "The OOM-killed event at 2024-01-15T10:30:00Z correlates with a memory limit change at 2024-01-15T10:25:00Z. The new limit of 256Mi appears insufficient for the workload.",
    }


def _make_evidence_package() -> EvidencePackage:
    """Create a realistic evidence package for LLM tests."""
    now = datetime.now(tz=UTC)
    event = make_oom_event(
        first_seen=now - timedelta(hours=1),
        last_seen=now,
    )
    changes = [
        FieldChange(
            field_path="spec.template.spec.containers[0].resources.limits.memory",
            old_value='"128Mi"',
            new_value='"256Mi"',
            changed_at=now - timedelta(minutes=15),
        ),
    ]
    return EvidencePackage(
        incident_event=event,
        related_events=[],
        changes=changes,
        container_statuses=[],
        resource_specs=[],
        time_window="2h",
    )


def _mock_ollama_response(content: str, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response mimicking Ollama's chat/completions endpoint."""
    if status_code != 200:
        return httpx.Response(
            status_code=status_code,
            json={"error": "server error"},
            request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"),
        )
    return httpx.Response(
        status_code=200,
        json={
            "choices": [
                {
                    "message": {
                        "content": content,
                    }
                }
            ]
        },
        request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"),
    )


# ---------------------------------------------------------------------------
# Successful LLM analysis
# ---------------------------------------------------------------------------


class TestSuccessfulLLMAnalysis:
    async def test_successful_analysis_returns_correct_result(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """A valid LLM JSON response should produce a successful LLMResult."""
        analyzer = LLMAnalyzer(ollama_config)
        response_json = json.dumps(_make_successful_llm_response())
        mock_response = _mock_ollama_response(response_json)

        with patch.object(analyzer._client, "post", new_callable=AsyncMock, return_value=mock_response):
            event = make_oom_event()
            evidence = _make_evidence_package()
            result = await analyzer.analyze(event=event, evidence=evidence)

        assert result.success is True
        assert "memory" in result.root_cause.lower()
        assert result.confidence > 0.0
        assert result.confidence <= 0.70  # LLM confidence is capped at 0.70
        assert len(result.evidence_citations) == 3
        assert len(result.affected_resources) == 2
        assert result.retries_used == 0

        await analyzer.aclose()

    async def test_llm_confidence_capped_at_070(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """LLM confidence must never exceed 0.70 regardless of citation count."""
        analyzer = LLMAnalyzer(ollama_config)

        # Provide many citations + diff support for maximum confidence
        resp = _make_successful_llm_response()
        resp["evidence_citations"] = [f"citation-{i}" for i in range(10)]
        response_json = json.dumps(resp)
        mock_response = _mock_ollama_response(response_json)

        with patch.object(analyzer._client, "post", new_callable=AsyncMock, return_value=mock_response):
            evidence = _make_evidence_package()
            result = await analyzer.analyze(event=make_oom_event(), evidence=evidence)

        assert result.success is True
        assert result.confidence <= 0.70

        await analyzer.aclose()


# ---------------------------------------------------------------------------
# Malformed JSON -> retry with correction prompt
# ---------------------------------------------------------------------------


class TestMalformedJSONRetry:
    async def test_malformed_json_triggers_retry(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """When LLM returns invalid JSON, it should retry with a correction prompt."""
        analyzer = LLMAnalyzer(ollama_config)

        # First call returns invalid JSON, second returns valid
        bad_response = _mock_ollama_response("This is not JSON at all!")
        good_json = json.dumps(_make_successful_llm_response())
        good_response = _mock_ollama_response(good_json)

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return bad_response
            return good_response

        with patch.object(analyzer._client, "post", side_effect=mock_post):
            result = await analyzer.analyze(event=make_oom_event(), evidence=_make_evidence_package())

        assert result.success is True
        assert result.retries_used == 1
        assert call_count == 2

        await analyzer.aclose()

    async def test_all_retries_exhausted_returns_failure(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """When all retries return malformed JSON, result should be unsuccessful."""
        analyzer = LLMAnalyzer(ollama_config)
        bad_response = _mock_ollama_response("not json {{{")

        with patch.object(analyzer._client, "post", new_callable=AsyncMock, return_value=bad_response):
            result = await analyzer.analyze(event=make_oom_event(), evidence=_make_evidence_package())

        assert result.success is False
        assert result.confidence == 0.0
        assert result.retries_used == ollama_config.max_retries

        await analyzer.aclose()


# ---------------------------------------------------------------------------
# LLM unavailable -> falls back to INCONCLUSIVE
# ---------------------------------------------------------------------------


class TestLLMUnavailable:
    async def test_connection_error_returns_failure(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """When Ollama is unreachable, result should indicate failure."""
        analyzer = LLMAnalyzer(ollama_config)

        with patch.object(
            analyzer._client,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = await analyzer.analyze(event=make_oom_event(), evidence=_make_evidence_package())

        assert result.success is False
        assert "unavailable" in result.root_cause.lower()
        assert result.confidence == 0.0

        await analyzer.aclose()

    async def test_timeout_returns_failure(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """When Ollama times out, result should indicate timeout failure."""
        analyzer = LLMAnalyzer(ollama_config)

        with patch.object(
            analyzer._client,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.ReadTimeout("read timed out"),
        ):
            result = await analyzer.analyze(event=make_oom_event(), evidence=_make_evidence_package())

        assert result.success is False
        assert "timeout" in result.root_cause.lower()
        assert result.confidence == 0.0

        await analyzer.aclose()

    async def test_http_500_returns_failure(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """When Ollama returns HTTP 500, result should indicate failure."""
        analyzer = LLMAnalyzer(ollama_config)
        error_response = _mock_ollama_response("", status_code=500)

        with patch.object(analyzer._client, "post", new_callable=AsyncMock, return_value=error_response):
            result = await analyzer.analyze(event=make_oom_event(), evidence=_make_evidence_package())

        assert result.success is False
        assert result.confidence == 0.0

        await analyzer.aclose()


# ---------------------------------------------------------------------------
# Partial parse (missing keys) -> 0.30 confidence
# ---------------------------------------------------------------------------


class TestPartialParse:
    async def test_partial_json_missing_keys_gets_030_confidence(
        self,
        ollama_config: OllamaConfig,
    ) -> None:
        """When LLM returns JSON with root_cause but missing other required keys, confidence = 0.30."""
        analyzer = LLMAnalyzer(ollama_config)

        # Only root_cause present, missing causal_chain, suggested_remediation, etc.
        partial_response = json.dumps(
            {
                "root_cause": "Partial diagnosis: OOM due to memory leak.",
            }
        )
        mock_response = _mock_ollama_response(partial_response)

        with patch.object(analyzer._client, "post", new_callable=AsyncMock, return_value=mock_response):
            result = await analyzer.analyze(event=make_oom_event(), evidence=_make_evidence_package())

        assert result.success is True
        assert result.confidence == pytest.approx(0.30)
        assert "memory leak" in result.root_cause.lower() or "oom" in result.root_cause.lower()

        await analyzer.aclose()


# ---------------------------------------------------------------------------
# Evidence package assembly + truncation
# ---------------------------------------------------------------------------


class TestEvidencePackageTruncation:
    def test_evidence_package_respects_12kb_limit(self) -> None:
        """The prompt context should be truncated to MAX_PROMPT_BYTES (12KB)."""
        now = datetime.now(tz=UTC)
        incident = make_oom_event()

        # Create many related events to blow up the prompt size
        related_events = [
            make_event(
                reason="Warning",
                message="x" * 500,
                resource_name=f"pod-{i}",
                first_seen=now - timedelta(minutes=i),
                last_seen=now - timedelta(minutes=i),
                event_id=f"evt-{i}",
            )
            for i in range(50)
        ]

        # Create large resource specs
        resource_specs = [
            {"kind": "Deployment", "namespace": "default", "name": "my-app", "spec": {"data": "x" * 2000}}
            for _ in range(10)
        ]

        package = EvidencePackage(
            incident_event=incident,
            related_events=related_events,
            changes=[],
            container_statuses=[],
            resource_specs=resource_specs,
            time_window="2h",
        )

        prompt = package.to_prompt_context()
        prompt_bytes = len(prompt.encode("utf-8"))
        assert prompt_bytes <= MAX_PROMPT_BYTES

    def test_evidence_package_includes_incident_event(self) -> None:
        """The prompt context should always include the incident event section."""
        package = _make_evidence_package()
        prompt = package.to_prompt_context()

        assert "## Incident" in prompt
        assert "OOMKilled" in prompt or "OOM-killed" in prompt


# ---------------------------------------------------------------------------
# _compute_confidence logic
# ---------------------------------------------------------------------------


class TestComputeConfidenceFunction:
    def test_zero_citations_yields_030_to_040(self) -> None:
        confidence = _compute_confidence(citations=[], causal_chain="", has_ledger_diff_support=False)
        assert 0.30 <= confidence <= 0.40

    def test_zero_citations_with_chain_yields_035(self) -> None:
        confidence = _compute_confidence(citations=[], causal_chain="some chain", has_ledger_diff_support=False)
        assert confidence == pytest.approx(0.35)

    def test_two_citations_yields_040_to_050(self) -> None:
        confidence = _compute_confidence(citations=["c1", "c2"], causal_chain="chain", has_ledger_diff_support=False)
        assert 0.40 <= confidence <= 0.50

    def test_three_plus_citations_with_diff_yields_060_to_070(self) -> None:
        confidence = _compute_confidence(
            citations=["c1", "c2", "c3"], causal_chain="chain", has_ledger_diff_support=True
        )
        assert 0.60 <= confidence <= 0.70

    def test_confidence_hard_cap_at_070(self) -> None:
        confidence = _compute_confidence(
            citations=["c" for _ in range(20)], causal_chain="chain", has_ledger_diff_support=True
        )
        assert confidence <= 0.70
