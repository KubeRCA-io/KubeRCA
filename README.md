# KubeRCA

[![CI](https://github.com/Kuberca-io/KubeRCA/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Kuberca-io/KubeRCA/actions/workflows/docker-publish.yml)
[![Release](https://img.shields.io/github/v/release/Kuberca-io/KubeRCA)](https://github.com/Kuberca-io/KubeRCA/releases)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Helm](https://img.shields.io/badge/Helm-v0.1.0-blue)](https://kuberca-io.github.io/KubeRCA)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-blue)](https://github.com/Kuberca-io/KubeRCA/pkgs/container/kuberca)

Kubernetes Root Cause Analysis system that automatically diagnoses cluster incidents using a deterministic rule engine with optional LLM fallback.

## Overview

KubeRCA watches your Kubernetes cluster in real time, correlates events across resources, and produces structured root-cause analysis reports. It uses a two-tier rule engine covering 18 common failure modes and can optionally escalate to an LLM (via Ollama) for incidents that don't match any deterministic rule.

## Key Features

- **18 diagnostic rules** - OOMKilled, CrashLoopBackOff, FailedScheduling, ImagePull errors, HPA issues, service connectivity, config drift, volume mount failures, node pressure, readiness probe failures, quota exceeded, eviction, and more
- **LLM fallback** - Optional Ollama integration for unmatched incidents with graceful degradation
- **Scout anomaly detector** - Proactive alerting with configurable cooldown
- **MCP server** - Model Context Protocol for AI-assisted Kubernetes debugging
- **Notifications** - Slack, email, and generic webhook channels
- **Prometheus metrics** - Rule hit rates, LLM escalation rates, analysis latency
- **Security hardened** - Non-root, read-only filesystem, seccomp, all capabilities dropped

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              KubeRCA Process                │
                    │                                             │
  K8s API ────────► │  Watchers ──► Cache ──► Coordinator ──────► │ ──► REST API
  (Events,Pods,     │              │   │       │        │         │     /api/v1/analyze
   Nodes)           │              │   │       ▼        ▼         │     /api/v1/status
                    │              │   │   Rule Engine  LLM       │     /api/v1/health
                    │              │   │   (18 rules)  (Ollama)   │
                    │              │   ▼                          │ ──► MCP Server
                    │              │  Ledger ──► Graph            │
                    │              │            (blast radius)    │ ──► Prometheus
                    │              │                              │     /metrics
                    │              └──► Scout ──► Notifications   │
                    │                   (anomaly   (Slack/Email/  │
                    │                    detect)    Webhook)      │
                    └─────────────────────────────────────────────┘
```

## Quick Start

### Helm (GitHub Pages)

```bash
helm repo add kuberca https://kuberca-io.github.io/KubeRCA
helm install kuberca kuberca/kuberca --namespace kuberca --create-namespace
```

### Helm (OCI)

```bash
helm install kuberca oci://ghcr.io/kuberca-io/charts/kuberca \
  --version 0.1.0 --namespace kuberca --create-namespace
```

### Docker

```bash
# GitHub Container Registry
docker pull ghcr.io/kuberca-io/kuberca:0.1.0

# Docker Hub
docker pull kuberca/kuberca:0.1.0
```

## Configuration

All settings are configured via `KUBERCA_*` environment variables (or Helm `values.yaml`).

| Variable | Default | Description |
|----------|---------|-------------|
| `KUBERCA_CLUSTER_ID` | `""` | Human-readable cluster identifier |
| `KUBERCA_OLLAMA_ENABLED` | `false` | Enable LLM fallback via Ollama |
| `KUBERCA_OLLAMA_ENDPOINT` | `http://localhost:11434` | Ollama API endpoint |
| `KUBERCA_OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model tag |
| `KUBERCA_OLLAMA_TIMEOUT` | `30` | Per-request timeout (10-90s) |
| `KUBERCA_OLLAMA_MAX_RETRIES` | `3` | Retries on JSON parse failure (0-5) |
| `KUBERCA_OLLAMA_TEMPERATURE` | `0.1` | Sampling temperature |
| `KUBERCA_OLLAMA_MAX_TOKENS` | `2048` | Max tokens per response |
| `KUBERCA_OLLAMA_HEALTH_CHECK_INTERVAL` | `60` | Health check interval (seconds) |
| `KUBERCA_RULE_ENGINE_TIME_WINDOW` | `2h` | Event correlation lookback window |
| `KUBERCA_RULE_ENGINE_TIER2_ENABLED` | `true` | Enable Tier 2 rules (R04-R18) |
| `KUBERCA_CHANGE_LEDGER_MAX_VERSIONS` | `5` | Max snapshot versions per resource (2-20) |
| `KUBERCA_CHANGE_LEDGER_RETENTION` | `6h` | Snapshot retention window |
| `KUBERCA_LEDGER_PERSISTENCE_ENABLED` | `false` | Persist ledger to SQLite |
| `KUBERCA_SCOUT_ANOMALY_COOLDOWN` | `15m` | Cooldown between repeated alerts |
| `KUBERCA_NOTIFICATIONS_SLACK_SECRET_REF` | `""` | K8s Secret name for Slack webhook |
| `KUBERCA_NOTIFICATIONS_EMAIL_SECRET_REF` | `""` | K8s Secret name for SMTP credentials |
| `KUBERCA_NOTIFICATIONS_EMAIL_TO` | `""` | Recipient email address |
| `KUBERCA_NOTIFICATIONS_WEBHOOK_SECRET_REF` | `""` | K8s Secret name for generic webhook |
| `KUBERCA_API_PORT` | `8080` | REST API listen port (1024-65535) |
| `KUBERCA_LOG_LEVEL` | `info` | Log level (debug, info, warning, error) |

## API Endpoints

All endpoints are under the `/api/v1` prefix.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/analyze` | Trigger root-cause analysis for a resource |
| `GET` | `/api/v1/status` | Cluster status snapshot (pods, nodes, events) |
| `GET` | `/api/v1/health` | Liveness probe |
| `GET` | `/metrics` | Prometheus metrics |

### Example: Analyze a resource

```bash
curl -X POST http://localhost:8080/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{"resource": "Pod/default/my-pod", "time_window": "2h"}'
```

## Development

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

### Setup

```bash
git clone https://github.com/Kuberca-io/KubeRCA.git
cd KubeRCA
uv sync --dev
```

### Tests, Lint, Type Check

```bash
uv run pytest                        # run tests
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run mypy kuberca/                 # type check (strict)
```

### Build Docker Image

```bash
docker build -t kuberca:dev .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, commit conventions, and PR process.

## License

[Apache License 2.0](LICENSE)
