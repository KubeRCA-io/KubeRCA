"""Configuration data structures."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OllamaConfig:
    """Ollama LLM configuration."""

    enabled: bool = False
    endpoint: str = "http://localhost:11434"
    model: str = "qwen2.5:7b"
    timeout_seconds: int = 30
    max_retries: int = 3
    temperature: float = 0.1
    max_tokens: int = 2048
    health_check_interval: int = 60


@dataclass
class RuleEngineConfig:
    """Rule engine configuration."""

    time_window: str = "2h"
    tier2_enabled: bool = True


@dataclass
class ChangeLedgerConfig:
    """Change ledger configuration."""

    max_versions: int = 5
    retention: str = "6h"
    persistence_enabled: bool = False


@dataclass
class ScoutConfig:
    """Scout anomaly detector configuration."""

    anomaly_cooldown: str = "15m"


@dataclass
class NotificationConfig:
    """Notification system configuration."""

    slack_secret_ref: str = ""
    email_secret_ref: str = ""
    email_to: str = ""
    webhook_secret_ref: str = ""


@dataclass
class APIConfig:
    """REST API configuration."""

    port: int = 8080


@dataclass
class LogConfig:
    """Logging configuration."""

    level: str = "info"


@dataclass
class KubeRCAConfig:
    """Top-level KubeRCA configuration."""

    cluster_id: str = ""
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    rule_engine: RuleEngineConfig = field(default_factory=RuleEngineConfig)
    change_ledger: ChangeLedgerConfig = field(default_factory=ChangeLedgerConfig)
    scout: ScoutConfig = field(default_factory=ScoutConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    api: APIConfig = field(default_factory=APIConfig)
    log: LogConfig = field(default_factory=LogConfig)
