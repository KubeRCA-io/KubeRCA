"""Configuration loading from environment variables."""

from __future__ import annotations

import os
import re

from kuberca.models.config import (
    APIConfig,
    ChangeLedgerConfig,
    KubeRCAConfig,
    LogConfig,
    NotificationConfig,
    OllamaConfig,
    RuleEngineConfig,
    ScoutConfig,
)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(f"KUBERCA_{key}", default)


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key, str(default).lower())
    return val.lower() in ("true", "1", "yes")


def _env_int(key: str, default: int, min_val: int | None = None, max_val: int | None = None) -> int:
    val = int(_env(key, str(default)))
    if min_val is not None:
        val = max(val, min_val)
    if max_val is not None:
        val = min(val, max_val)
    return val


def _env_float(key: str, default: float) -> float:
    return float(_env(key, str(default)))


def _validate_time_window(value: str) -> str:
    if not re.match(r"^[0-9]+(m|h|d)$", value):
        raise ValueError(f"Invalid time window format: {value}")
    return value


def _validate_log_level(value: str) -> str:
    valid = {"debug", "info", "warning", "error"}
    if value.lower() not in valid:
        raise ValueError(f"Invalid log level: {value}. Must be one of {valid}")
    return value.lower()


def load_config() -> KubeRCAConfig:
    """Load configuration from KUBERCA_* environment variables."""
    return KubeRCAConfig(
        cluster_id=_env("CLUSTER_ID", ""),
        ollama=OllamaConfig(
            enabled=_env_bool("OLLAMA_ENABLED", False),
            endpoint=_env("OLLAMA_ENDPOINT", "http://localhost:11434"),
            model=_env("OLLAMA_MODEL", "qwen2.5:7b"),
            timeout_seconds=_env_int("OLLAMA_TIMEOUT", 30, min_val=10, max_val=90),
            max_retries=_env_int("OLLAMA_MAX_RETRIES", 3, min_val=0, max_val=5),
            temperature=_env_float("OLLAMA_TEMPERATURE", 0.1),
            max_tokens=_env_int("OLLAMA_MAX_TOKENS", 2048),
            health_check_interval=_env_int("OLLAMA_HEALTH_CHECK_INTERVAL", 60),
        ),
        rule_engine=RuleEngineConfig(
            time_window=_validate_time_window(_env("RULE_ENGINE_TIME_WINDOW", "2h")),
            tier2_enabled=_env_bool("RULE_ENGINE_TIER2_ENABLED", True),
        ),
        change_ledger=ChangeLedgerConfig(
            max_versions=_env_int("CHANGE_LEDGER_MAX_VERSIONS", 5, min_val=2, max_val=20),
            retention=_validate_time_window(_env("CHANGE_LEDGER_RETENTION", "6h")),
            persistence_enabled=_env_bool("LEDGER_PERSISTENCE_ENABLED", False),
        ),
        scout=ScoutConfig(
            anomaly_cooldown=_validate_time_window(_env("SCOUT_ANOMALY_COOLDOWN", "15m")),
        ),
        notifications=NotificationConfig(
            slack_secret_ref=_env("NOTIFICATIONS_SLACK_SECRET_REF", ""),
            email_secret_ref=_env("NOTIFICATIONS_EMAIL_SECRET_REF", ""),
            email_to=_env("NOTIFICATIONS_EMAIL_TO", ""),
            webhook_secret_ref=_env("NOTIFICATIONS_WEBHOOK_SECRET_REF", ""),
        ),
        api=APIConfig(
            port=_env_int("API_PORT", 8080, min_val=1024, max_val=65535),
        ),
        log=LogConfig(
            level=_validate_log_level(_env("LOG_LEVEL", "info")),
        ),
    )
