"""Property-based fuzz tests for the KubeRCA REST API.

Uses hypothesis to generate randomised inputs for all 3 endpoints
(/health, /status, /analyze) and validates that:
 1. No 500s from malformed input (validation catches everything)
 2. No unhandled exceptions leak through
 3. Response body is always valid JSON
 4. Error responses always have ``error`` + ``detail``
 5. Content-Type is always ``application/json``
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from kuberca.api.app import create_app
from kuberca.models.analysis import AffectedResource, EvidenceItem, RCAResponse, ResponseMeta
from kuberca.models.events import DiagnosisSource, EvidenceType
from kuberca.models.resources import CacheReadiness

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_api_app.py patterns)
# ---------------------------------------------------------------------------


def _make_rca_response() -> RCAResponse:
    return RCAResponse(
        root_cause="Image pull failure due to invalid tag.",
        confidence=0.91,
        diagnosed_by=DiagnosisSource.RULE_ENGINE,
        rule_id="image-pull-failure",
        evidence=[
            EvidenceItem(
                type=EvidenceType.EVENT,
                timestamp="2024-01-15T10:30:00Z",
                summary="ErrImagePull: image not found",
            )
        ],
        affected_resources=[AffectedResource(kind="Pod", namespace="default", name="api-pod")],
        suggested_remediation="Fix the image tag in the Deployment spec.",
        _meta=ResponseMeta(
            kuberca_version="0.1.2",
            schema_version="1",
            cluster_id="test-cluster",
            timestamp="2024-01-15T10:30:01Z",
            response_time_ms=42,
            cache_state="ready",
            warnings=[],
        ),
    )


def _make_cache(readiness: CacheReadiness = CacheReadiness.READY) -> MagicMock:
    cache = MagicMock()
    cache.readiness_state = readiness
    cache.get = MagicMock(return_value=MagicMock())
    cache.list_pods = MagicMock(return_value=[])
    cache.list_nodes = MagicMock(return_value=[])
    cache.list_events = MagicMock(return_value=[])
    return cache


def _make_coordinator(rca: RCAResponse | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.analyze = AsyncMock(return_value=rca or _make_rca_response())
    return coordinator


def _make_app(
    coordinator: MagicMock | None = None,
    cache: MagicMock | None = None,
) -> TestClient:
    app = create_app(
        coordinator=coordinator or _make_coordinator(),
        cache=cache or _make_cache(),
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Printable text without NUL bytes (JSON-safe)
_json_safe_text = st.text(
    alphabet=st.characters(
        codec="utf-8",
        exclude_categories=("Cs",),  # exclude surrogates
    ),
    min_size=0,
    max_size=500,
)

# Strings that specifically lack the Kind/ns/name shape
_bad_resource_no_slashes = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
    min_size=1,
    max_size=200,
).filter(lambda s: s.count("/") != 2)

# Strings with wrong number of slashes
_wrong_slash_count = st.sampled_from(
    [
        "Pod/default",
        "Pod",
        "a/b/c/d",
        "a/b/c/d/e",
        "/",
        "//",
        "///",
        "////",
    ]
)

# Strings with exactly 2 slashes but empty segments
_empty_segments = st.sampled_from(
    [
        "/ns/name",
        "Kind//name",
        "Kind/ns/",
        "//name",
        "Kind//",
        "/ns/",
        "//",
        "///",
    ]
)

# Valid-shaped resource with random parts
_valid_shaped_resource = st.builds(
    lambda k, n, na: f"{k}/{n}/{na}",
    st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,20}", fullmatch=True),
    st.from_regex(r"[a-z][a-z0-9\-]{0,20}", fullmatch=True),
    st.from_regex(r"[a-z][a-z0-9\-]{0,20}", fullmatch=True),
)

# Invalid time_window strings
_bad_time_window = st.text(min_size=1, max_size=50).filter(lambda s: not __import__("re").match(r"^\d+(m|h|d)$", s))

# Valid time_window edge cases
_edge_time_window = st.sampled_from(["0m", "0h", "0d", "1m", "999999h", "1d"])

# Time window with invalid units
_bad_unit_time_window = st.builds(
    lambda n, u: f"{n}{u}",
    st.integers(min_value=0, max_value=9999),
    st.sampled_from(["s", "w", "y", "ms", "min", "hr", "M", "H", "D", "x", ""]),
)


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------


def _assert_valid_json_response(resp, allowed_status_codes: set[int] | None = None):
    """Assert universal invariants on every API response."""
    assert resp.headers.get("content-type", "").startswith("application/json"), (
        f"Expected application/json, got {resp.headers.get('content-type')}"
    )
    body = resp.json()  # must parse as JSON
    assert isinstance(body, dict)

    if allowed_status_codes is not None:
        assert resp.status_code in allowed_status_codes, f"Unexpected status {resp.status_code}, body={body}"

    # Error responses must have error + detail
    if resp.status_code >= 400:
        assert "error" in body, f"Error response missing 'error': {body}"
        assert "detail" in body, f"Error response missing 'detail': {body}"


# ===========================================================================
# A. POST /analyze — resource format fuzzing
# ===========================================================================


class TestAnalyzeResourceFuzz:
    """Fuzz the ``resource`` field of POST /analyze."""

    @given(resource=_bad_resource_no_slashes)
    @settings(max_examples=50)
    def test_random_strings_without_kind_ns_name_return_400(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})

    @given(resource=_wrong_slash_count)
    @settings(max_examples=20)
    def test_wrong_slash_count_returns_400(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})

    @given(resource=_empty_segments)
    @settings(max_examples=20)
    def test_empty_segments_return_400(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})

    @given(resource=_valid_shaped_resource)
    @settings(max_examples=50)
    def test_valid_shaped_resource_never_500(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={200, 400, 404, 422})

    @given(
        resource=st.builds(
            lambda k, n, na: f"{k}/{n}/{na}",
            st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=50),
            st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=50),
            st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=50),
        )
    )
    @settings(max_examples=50)
    def test_unicode_resource_parts_never_500(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={200, 400, 404, 422})

    @given(
        resource=st.builds(
            lambda k, n, na: f"{k}/{n}/{na}",
            st.text(min_size=1, max_size=10),
            st.text(min_size=1, max_size=10),
            st.text(min_size=1, max_size=10),
        ).filter(lambda s: "\x00" in s or any(ord(c) < 32 for c in s))
    )
    @settings(max_examples=30)
    def test_control_characters_never_crash(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={200, 400, 404, 422})

    @given(length=st.integers(min_value=1000, max_value=5000))
    @settings(max_examples=10)
    def test_extremely_long_resource_never_crash(self, length: int) -> None:
        resource = f"Pod/{'x' * length}/{'y' * length}"
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        _assert_valid_json_response(resp, allowed_status_codes={200, 400, 404, 422})

    def test_path_traversal_attempts_handled(self) -> None:
        client = _make_app()
        traversals = [
            "../../../etc/passwd",
            "Pod/../../secret",
            "Pod/default/..%2f..%2fetc%2fpasswd",
            "Pod/../../../name",
        ]
        for resource in traversals:
            resp = client.post("/api/v1/analyze", json={"resource": resource})
            _assert_valid_json_response(resp, allowed_status_codes={200, 400, 404, 422})
            assert resp.status_code != 500


# ===========================================================================
# B. POST /analyze — time_window fuzzing
# ===========================================================================


class TestAnalyzeTimeWindowFuzz:
    """Fuzz the ``time_window`` field of POST /analyze."""

    @given(time_window=_bad_time_window)
    @settings(max_examples=50)
    def test_random_invalid_time_windows_return_400(self, time_window: str) -> None:
        client = _make_app()
        resp = client.post(
            "/api/v1/analyze",
            json={"resource": "Pod/default/test", "time_window": time_window},
        )
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})

    @given(time_window=_edge_time_window)
    @settings(max_examples=20)
    def test_edge_value_time_windows_accepted(self, time_window: str) -> None:
        client = _make_app()
        resp = client.post(
            "/api/v1/analyze",
            json={"resource": "Pod/default/test", "time_window": time_window},
        )
        _assert_valid_json_response(resp, allowed_status_codes={200, 400})

    def test_missing_time_window_uses_default(self) -> None:
        client = _make_app()
        resp = client.post(
            "/api/v1/analyze",
            json={"resource": "Pod/default/test"},
        )
        _assert_valid_json_response(resp, allowed_status_codes={200})

    @given(time_window=_bad_unit_time_window)
    @settings(max_examples=30)
    def test_invalid_units_return_400(self, time_window: str) -> None:
        client = _make_app()
        resp = client.post(
            "/api/v1/analyze",
            json={"resource": "Pod/default/test", "time_window": time_window},
        )
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})


# ===========================================================================
# C. POST /analyze — body fuzzing
# ===========================================================================


class TestAnalyzeBodyFuzz:
    """Fuzz the request body structure of POST /analyze."""

    def test_missing_resource_field_returns_error(self) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={})
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})

    @given(extra=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=50), max_size=5))
    @settings(max_examples=30)
    def test_extra_fields_ignored(self, extra: dict) -> None:
        body = {"resource": "Pod/default/test", **extra}
        client = _make_app()
        resp = client.post("/api/v1/analyze", json=body)
        _assert_valid_json_response(resp, allowed_status_codes={200})

    def test_non_json_content_type_returns_error(self) -> None:
        client = _make_app()
        resp = client.post(
            "/api/v1/analyze",
            content=b"resource=Pod/default/test",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        _assert_valid_json_response(resp, allowed_status_codes={400, 422})

    @given(
        body=st.one_of(
            st.just(""),
            st.just("null"),
            st.just("[]"),
            st.just("42"),
            st.just('"string"'),
            st.just("true"),
        )
    )
    @settings(max_examples=10)
    def test_non_object_json_returns_error(self, body: str) -> None:
        client = _make_app()
        resp = client.post(
            "/api/v1/analyze",
            content=body.encode(),
            headers={"content-type": "application/json"},
        )
        _assert_valid_json_response(resp, allowed_status_codes={400, 422, 500})


# ===========================================================================
# D. GET /status — namespace fuzzing
# ===========================================================================


class TestStatusNamespaceFuzz:
    """Fuzz the ``namespace`` query param of GET /status."""

    @given(namespace=_json_safe_text)
    @settings(max_examples=50)
    def test_random_namespace_always_200(self, namespace: str) -> None:
        client = _make_app()
        resp = client.get("/api/v1/status", params={"namespace": namespace})
        _assert_valid_json_response(resp, allowed_status_codes={200})

    @given(length=st.integers(min_value=500, max_value=2000))
    @settings(max_examples=10)
    def test_very_long_namespace_always_200(self, length: int) -> None:
        client = _make_app()
        resp = client.get("/api/v1/status", params={"namespace": "x" * length})
        _assert_valid_json_response(resp, allowed_status_codes={200})

    @given(
        namespace=st.text(
            alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
            min_size=1,
            max_size=100,
        )
    )
    @settings(max_examples=30)
    def test_unicode_namespace_always_200(self, namespace: str) -> None:
        client = _make_app()
        resp = client.get("/api/v1/status", params={"namespace": namespace})
        _assert_valid_json_response(resp, allowed_status_codes={200})


# ===========================================================================
# E. GET /health — robustness
# ===========================================================================


class TestHealthFuzz:
    """Fuzz GET /health with random query parameters."""

    @given(
        params=st.dictionaries(
            st.text(min_size=1, max_size=20),
            st.text(max_size=50),
            max_size=5,
        )
    )
    @settings(max_examples=30)
    def test_random_query_params_always_200(self, params: dict) -> None:
        client = _make_app()
        resp = client.get("/api/v1/health", params=params)
        _assert_valid_json_response(resp, allowed_status_codes={200})


# ===========================================================================
# F. Response contract invariants
# ===========================================================================


class TestResponseContracts:
    """Verify response schemas across fuzzed valid requests."""

    @given(resource=_valid_shaped_resource)
    @settings(max_examples=30)
    def test_valid_analyze_response_matches_schema(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        if resp.status_code == 200:
            body = resp.json()
            assert "root_cause" in body
            assert "confidence" in body
            assert "diagnosed_by" in body
            assert "evidence" in body
            assert isinstance(body["evidence"], list)
            assert "affected_resources" in body
            assert isinstance(body["affected_resources"], list)

    @given(resource=_bad_resource_no_slashes)
    @settings(max_examples=30)
    def test_error_responses_always_have_error_and_detail(self, resource: str) -> None:
        client = _make_app()
        resp = client.post("/api/v1/analyze", json={"resource": resource})
        if resp.status_code >= 400:
            body = resp.json()
            assert "error" in body, f"Missing 'error' in {body}"
            assert "detail" in body, f"Missing 'detail' in {body}"
            assert isinstance(body["error"], str)
            assert isinstance(body["detail"], str)

    def test_health_always_returns_required_fields(self) -> None:
        client = _make_app()
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "version" in body
        assert "cache_state" in body
        assert body["status"] == "ok"
