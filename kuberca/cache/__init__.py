"""Cache layer for KubeRCA.

Provides in-memory resource caching backed by Kubernetes watch streams.
The cache exposes only redacted views to downstream consumers (rule engine,
LLM analyzer, evidence assembly) — raw data never leaves this package.

Submodules:
    redaction       -- Spec redaction engine (key denylist, value heuristics, annotations).
    resource_cache  -- In-memory resource cache with 4-state readiness model.
"""

from kuberca.cache.resource_cache import ResourceCache

__all__ = ["ResourceCache"]
