# CLAUDE.md

## Build & Run Commands

```bash
# Install dependencies
uv sync --dev

# Run tests
uv run pytest                                      # all tests
uv run pytest tests/unit/                           # unit tests only
uv run pytest tests/integration/ -m integration     # integration tests
uv run pytest --cov=kuberca --cov-report=term-missing  # with coverage

# Lint & format
uv run ruff check .          # lint
uv run ruff check --fix .    # auto-fix
uv run ruff format .         # format

# Type check
uv run mypy kuberca/         # strict mode

# Docker
docker build -t kuberca:dev .

# Helm
helm lint helm/kuberca
helm template kuberca helm/kuberca
```

## Architecture

KubeRCA is a single-process async Python application. Components start in strict dependency order:

```
config -> logging -> K8s client -> cache -> dependency graph -> ledger
  -> collectors -> rules -> LLM -> coordinator -> queue -> scout
  -> notifications -> MCP -> REST
```

Shutdown is reverse order. Each component failure is isolated.

### Key Directories

```
kuberca/
  app.py              # Application bootstrap, lifecycle management
  config.py           # KUBERCA_* env var loading
  models/             # Pydantic models (config, analysis, resources, events)
  collector/          # Event, Pod, Node watchers (kubernetes-asyncio)
  cache/              # In-memory ResourceCache (18 resource types)
  ledger/             # Change tracking with optional SQLite persistence
  graph/              # Dependency graph for blast-radius analysis
  rules/              # 18 diagnostic rules (R01-R18), base class, confidence
  analyst/            # Coordinator + bounded work queue
  llm/                # Ollama LLM analyzer (optional fallback)
  scout/              # Anomaly detector with cooldown
  api/                # FastAPI REST API (routes, schemas, app builder)
  mcp/                # Model Context Protocol server
  notifications/      # Slack, email, webhook dispatchers
  observability/      # structlog setup, Prometheus metrics
  cli/                # Click CLI entrypoint
helm/kuberca/         # Helm chart (Chart.yaml, values.yaml, templates/)
tests/unit/           # Unit tests
tests/integration/    # Integration tests
e2e/                  # End-to-end tests
```

## Coding Conventions

- **Formatter/linter**: ruff (line-length 120, target py312)
- **Type checking**: mypy strict mode
- **Logging**: structlog (JSON in production, console in dev)
- **Models**: Pydantic v2 for all data structures
- **Async**: asyncio throughout, kubernetes-asyncio for K8s API
- **Dependencies**: managed by uv with lockfile (uv.lock)
- **Tests**: pytest + pytest-asyncio (asyncio_mode = "auto")
- **Commits**: Conventional Commits format
