"""Integration tests for the security redaction pipeline.

Tests verify that sensitive data is properly redacted when flowing through
the cache, rule engine, and evidence assembly layers.
"""

from __future__ import annotations

import copy
import hashlib

import pytest

from kuberca.cache.redaction import redact_dict, redact_value, redacted_json
from kuberca.cache.resource_cache import ResourceCache
from kuberca.llm.evidence import EvidencePackage

from .conftest import make_oom_event

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_hash(value: str) -> str:
    """Compute expected hash for a given value string."""
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"[HASH:sha256:{digest[:8]}:{digest}]"


def _build_deployment_spec_with_secrets() -> dict:
    """Build a Deployment spec that contains various kinds of secrets."""
    return {
        "replicas": 3,
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "my-app",
                        "image": "my-app:v2",
                        "resources": {
                            "limits": {"memory": "256Mi", "cpu": "500m"},
                        },
                        "env": [
                            {"name": "DB_HOST", "value": "db.svc.cluster.local"},
                            {"name": "DB_PASSWORD", "value": "supersecret123"},
                            {"name": "API_KEY", "value": "sk-1234567890abcdef"},
                            {"name": "LOG_LEVEL", "value": "info"},
                        ],
                        "command": [
                            "/bin/app",
                            "--config=/etc/config.yaml",
                            "--password=topsecret",
                        ],
                    }
                ],
                "volumes": [
                    {"name": "config", "configMap": {"name": "app-config"}},
                    {"name": "secret-vol", "secret": {"secretName": "app-secrets"}},
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# Full Deployment spec through cache -> no secrets in CachedResourceView
# ---------------------------------------------------------------------------


class TestCacheRedaction:
    def test_deployment_spec_secrets_redacted_in_view(self) -> None:
        """A Deployment with secret env vars should have them redacted in CachedResourceView."""
        cache = ResourceCache()
        cache._all_kinds = {"Deployment"}
        cache._ready_kinds = {"Deployment"}

        raw_obj = {
            "metadata": {
                "name": "my-app",
                "namespace": "default",
                "resourceVersion": "1001",
                "labels": {"app": "my-app"},
                "annotations": {"custom/internal-note": "secret stuff"},
            },
            "spec": _build_deployment_spec_with_secrets(),
            "status": {"availableReplicas": 3},
        }
        cache.update("Deployment", "default", "my-app", raw_obj)

        view = cache.get("Deployment", "default", "my-app")
        assert view is not None

        # Check that env values are redacted
        containers = view.spec.get("template", {}).get("spec", {}).get("containers", [])
        assert len(containers) >= 1
        env_list = containers[0].get("env", [])
        for env_entry in env_list:
            # Every env value should be [REDACTED]
            if "value" in env_entry:
                assert env_entry["value"] == "[REDACTED]"

    def test_secret_volumes_redacted(self) -> None:
        """Secret volume references should be redacted in the cached view."""
        cache = ResourceCache()
        cache._all_kinds = {"Deployment"}
        cache._ready_kinds = {"Deployment"}

        raw_obj = {
            "metadata": {
                "name": "my-app",
                "namespace": "default",
                "resourceVersion": "1001",
                "labels": {},
                "annotations": {},
            },
            "spec": _build_deployment_spec_with_secrets(),
            "status": {},
        }
        cache.update("Deployment", "default", "my-app", raw_obj)

        view = cache.get("Deployment", "default", "my-app")
        assert view is not None

        # The "secret" key should be redacted because "secret" is in the denylist
        volumes = view.spec.get("template", {}).get("spec", {}).get("volumes", [])
        for vol in volumes:
            if "secret" in vol:
                assert vol["secret"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# Secret env vars redacted in evidence package
# ---------------------------------------------------------------------------


class TestEvidencePackageRedaction:
    def test_secret_env_vars_redacted_in_evidence(self) -> None:
        """When resource specs flow into an evidence package, secrets should be redacted."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod"}
        cache._ready_kinds = {"Pod"}

        cache.update(
            "Pod",
            "default",
            "my-app-pod",
            {
                "metadata": {
                    "name": "my-app-pod",
                    "namespace": "default",
                    "resourceVersion": "1001",
                    "labels": {},
                    "annotations": {},
                },
                "spec": {
                    "containers": [
                        {
                            "name": "my-app",
                            "image": "my-app:v2",
                            "env": [
                                {"name": "DB_PASSWORD", "value": "supersecret123"},
                                {"name": "SAFE_VAR", "value": "hello"},
                            ],
                        }
                    ],
                },
                "status": {"containerStatuses": [{"name": "my-app", "ready": True}]},
            },
        )

        view = cache.get("Pod", "default", "my-app-pod")
        assert view is not None

        # Simulate building evidence with the redacted spec
        resource_spec = {
            "kind": view.kind,
            "namespace": view.namespace,
            "name": view.name,
            "spec": view.spec,
            "status": view.status,
        }

        event = make_oom_event()
        package = EvidencePackage(
            incident_event=event,
            resource_specs=[resource_spec],
            time_window="2h",
        )
        prompt = package.to_prompt_context()

        # The prompt should not contain the raw secret value
        assert "supersecret123" not in prompt


# ---------------------------------------------------------------------------
# Bearer tokens hashed consistently
# ---------------------------------------------------------------------------


class TestBearerTokenHashing:
    def test_bearer_token_hashed_consistently(self) -> None:
        """The same bearer token should always produce the same hash."""
        token = "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnop"

        result1 = redact_value("auth_header", token)
        result2 = redact_value("auth_header", token)

        assert result1 == result2
        assert isinstance(result1, str)
        assert result1.startswith("[HASH:sha256:")

    def test_different_tokens_produce_different_hashes(self) -> None:
        """Different bearer tokens should produce different hashes."""
        token_a = "Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        token_b = "Bearer bbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        hash_a = redact_value("auth_header", token_a)
        hash_b = redact_value("auth_header", token_b)

        assert hash_a != hash_b


# ---------------------------------------------------------------------------
# Annotation redaction respects safe prefixes
# ---------------------------------------------------------------------------


class TestAnnotationRedaction:
    def test_safe_prefix_annotations_preserved(self) -> None:
        """Annotations with safe prefixes (kubernetes.io/, helm.sh/, etc.) should be kept."""
        annotations = {
            "kubernetes.io/change-cause": "kubectl apply",
            "helm.sh/chart": "my-chart-1.0.0",
            "app.kubernetes.io/name": "my-app",
            "custom/internal-secret": "sensitive-data",
        }
        redacted = redact_dict(copy.deepcopy(annotations), is_annotation=True)

        # Safe-prefix annotations should be preserved
        assert redacted["kubernetes.io/change-cause"] == "kubectl apply"
        assert redacted["helm.sh/chart"] == "my-chart-1.0.0"
        assert redacted["app.kubernetes.io/name"] == "my-app"

        # Custom annotations should be redacted
        assert redacted["custom/internal-secret"] == "[REDACTED]"

    def test_empty_annotations_unchanged(self) -> None:
        """Empty annotations dict should be returned unchanged."""
        annotations: dict[str, object] = {}
        redacted = redact_dict(annotations, is_annotation=True)
        assert redacted == {}


# ---------------------------------------------------------------------------
# Redacted data passed through rule engine correctly
# ---------------------------------------------------------------------------


class TestRedactionInRuleEngine:
    def test_rule_engine_sees_redacted_spec(self) -> None:
        """When rule engine queries cache, it should receive redacted CachedResourceView."""
        cache = ResourceCache()
        cache._all_kinds = {"Pod", "Deployment", "ReplicaSet"}
        cache._ready_kinds = {"Pod", "Deployment", "ReplicaSet"}

        # Add deployment with secrets
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
                    "annotations": {},
                },
                "spec": _build_deployment_spec_with_secrets(),
                "status": {},
            },
        )

        # Add matching ReplicaSet and Pod
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
                "spec": {},
                "status": {},
            },
        )

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
                "spec": {},
                "status": {},
            },
        )

        # The view returned by cache.get should be redacted
        deploy_view = cache.get("Deployment", "default", "my-app")
        assert deploy_view is not None

        # Verify env values are redacted in what the rule engine would see
        containers = deploy_view.spec.get("template", {}).get("spec", {}).get("containers", [])
        if containers:
            for env_entry in containers[0].get("env", []):
                if "value" in env_entry:
                    assert env_entry["value"] == "[REDACTED]"

    def test_command_arg_secrets_redacted(self) -> None:
        """Command arguments with --password= prefix should be redacted."""
        spec = {
            "containers": [
                {
                    "name": "app",
                    "command": ["/bin/app", "--password=topsecret", "--config=app.yaml"],
                }
            ]
        }
        redacted = redact_dict(copy.deepcopy(spec))
        commands = redacted["containers"][0]["command"]
        assert commands[1] == "--password=[REDACTED]"
        assert commands[2] == "--config=app.yaml"  # safe, not redacted


# ---------------------------------------------------------------------------
# Redacted data in LLM evidence package
# ---------------------------------------------------------------------------


class TestRedactionInLLMEvidence:
    def test_llm_evidence_contains_no_raw_secrets(self) -> None:
        """Evidence package formatted for LLM should contain no raw secret values."""
        event = make_oom_event()

        # Create resource specs with some secret content that should be redacted
        redacted_spec = redact_dict(copy.deepcopy(_build_deployment_spec_with_secrets()))

        package = EvidencePackage(
            incident_event=event,
            related_events=[],
            changes=[],
            container_statuses=[],
            resource_specs=[
                {
                    "kind": "Deployment",
                    "namespace": "default",
                    "name": "my-app",
                    "spec": redacted_spec,
                }
            ],
            time_window="2h",
        )

        prompt = package.to_prompt_context()

        # Raw secrets should NOT appear in the prompt
        assert "supersecret123" not in prompt
        assert "topsecret" not in prompt
        assert "sk-1234567890abcdef" not in prompt

    def test_redacted_json_utility(self) -> None:
        """redacted_json() should return a JSON string with all secrets redacted."""
        spec = _build_deployment_spec_with_secrets()
        json_str = redacted_json(spec)

        assert "supersecret123" not in json_str
        assert "[REDACTED]" in json_str
        # Verify it's valid JSON
        import json

        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
