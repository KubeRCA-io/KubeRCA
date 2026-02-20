# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-02-20

### Security

- Switch Docker base image from `python:3.12-slim` (Debian) to `python:3.12-alpine` — eliminates 23 Debian-package CVEs
- Remove pip/setuptools from runtime image — eliminates CVE-2025-8869 and CVE-2026-1703
- Remaining: 1 medium busybox CVE (CVE-2025-60876, no upstream fix)

### Changed

- Docker image size reduced from ~150 MB to ~45 MB
- Use Alpine's built-in nobody user (UID/GID 65534) instead of creating one

### Added

- Test coverage increased from 64% to 85% (623 → 1036 tests)
- 8 new test files covering rules R04-R10, collectors, MCP, notifications, rules init, cache, app lifecycle

[0.1.1]: https://github.com/KubeRCA-io/KubeRCA/compare/v0.1.0...v0.1.1

## [0.1.0] - 2025-02-19

### Added

- **Rule engine** with 18 diagnostic rules across two tiers:
  - Tier 1 (always enabled): OOMKilled (R01), CrashLoopBackOff (R02), FailedScheduling (R03)
  - Tier 2 (configurable): ImagePull (R04), HPA (R05), ServiceUnreachable (R06), ConfigDrift (R07), VolumeMount (R08), NodePressure (R09), ReadinessProbe (R10), FailedMount ConfigMap (R11), FailedMount Secret (R12), FailedMount PVC (R13), FailedMount NFS (R14), FailedScheduling Node (R15), ExceedQuota (R16), Evicted (R17), ClaimLost (R18)
- **LLM fallback** via Ollama integration for incidents not covered by deterministic rules
- **Event watchers** for Pods, Nodes, and Kubernetes Events with real-time streaming
- **Resource cache** with in-memory store for fast lookups across 18 resource types
- **Change ledger** with optional SQLite persistence for tracking resource spec diffs
- **Dependency graph** for blast-radius analysis of affected resources
- **Analyst coordinator** that orchestrates rule engine, LLM, and evidence collection
- **Analysis queue** with bounded concurrency for request processing
- **Scout anomaly detector** with configurable cooldown for proactive alerting
- **REST API** (FastAPI) with endpoints: `/api/v1/analyze`, `/api/v1/status`, `/api/v1/health`
- **MCP server** (Model Context Protocol) for AI-assisted Kubernetes debugging
- **Notification dispatcher** supporting Slack, email, and generic webhooks
- **Prometheus metrics** with counters, gauges, and rolling rate computation
- **Helm chart** with security-hardened defaults (non-root, read-only filesystem, seccomp, dropped capabilities)
- **Docker image** with multi-stage build (Python 3.12-slim, uv package manager)
- **CI/CD workflows** for Docker build/push, Helm chart release, and GitHub Release creation

[0.1.0]: https://github.com/KubeRCA-io/KubeRCA/releases/tag/v0.1.0
