"""Typed configuration loaded from environment variables (design.md section 66).

Env vars use the APP_ prefix and "__" for nesting, e.g. APP_DATABASE__URL.
Secrets are SecretStr and never appear in repr, logs or error messages.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"


class DatabaseConfig(BaseModel):
    url: SecretStr
    pool_size: int = Field(default=5, ge=1, le=50)
    statement_timeout_ms: int = Field(default=30_000, ge=100)
    # Refuse to start when the schema is behind the latest migration (TASK-DB-002).
    check_schema_on_startup: bool = True


class StorageConfig(BaseModel):
    datastore_path: Path
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, gt=0)


class LLMConfig(BaseModel):
    endpoint: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:11434")
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    timeout_s: float = Field(default=60.0, gt=0)
    # ADR-0008 (proposed): qwen2.5:7b Q4_K_M at 8k context on the 6 GB GPU (BENCH-009).
    model: str = "qwen2.5:7b"
    num_ctx: int = Field(default=8192, ge=512)
    # Ollama "think" for reasoning models (e.g. qwen3.5); None = not sent (model default).
    think: bool | None = None
    max_concurrency: int = Field(default=1, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=5)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_reset_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def endpoint_must_be_allow_listed(self) -> Self:
        # CLAUDE.md section 20: the LLM endpoint must be a local/self-hosted, allow-listed host.
        host = urlsplit(str(self.endpoint)).hostname
        if host not in self.allowed_hosts:
            raise ValueError(f"LLM endpoint host {host!r} is not in allowed_hosts")
        return self


class SecurityConfig(BaseModel):
    """ADR-0013: server-side sessions, Argon2id, synchronizer CSRF (keyed by session_secret)."""

    session_secret: SecretStr
    cookie_secure: bool = True
    session_ttl_s: int = Field(default=12 * 3600, ge=60)  # absolute lifetime
    session_idle_s: int = Field(default=30 * 60, ge=60)  # idle timeout
    # OWASP Password Storage Cheat Sheet Argon2id floor: m>=47104 KiB (t=1) or m>=19456 KiB with
    # t>=2 (p=1); enforced by `argon2_meets_owasp`.
    argon2_memory_kib: int = Field(default=19_456, ge=19_456)
    argon2_time_cost: int = Field(default=2, ge=1)
    argon2_parallelism: int = Field(default=1, ge=1)
    max_body_bytes: int = Field(default=1024 * 1024, gt=0)  # non-upload request bodies
    # Login throttling (M2): failed logins per lower-cased email and per client IP within the
    # window; at the limit, logins are refused with 429 + Retry-After until the window ends.
    login_max_failures_per_email: int = Field(default=5, ge=1)
    login_max_failures_per_ip: int = Field(default=20, ge=1)
    login_failure_window_s: int = Field(default=15 * 60, ge=1)
    # Denial audit rows (authz/CSRF) per principal per window; excess denials are still refused
    # but only logged, so a principal cannot grow the audit chain without bound.
    denial_audit_max_per_principal: int = Field(default=10, ge=1)
    denial_audit_window_s: int = Field(default=60, ge=1)
    # base64url-encoded 32-byte AES-256-GCM key for connector credentials (TASK-SEC-009).
    credential_key: SecretStr | None = None
    # HMAC key for the audit hash chain (L4). Required in staging/prod; dev/test without it
    # derive a key from session_secret (key id "derived"). Rotation: set a new key + id and
    # move the old one into audit_retired_keys so existing rows still verify.
    audit_key: SecretStr | None = None
    audit_key_id: str = Field(default="k1", pattern=r"^[a-z0-9_-]{1,32}$")
    audit_retired_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def secret_long_enough(self) -> Self:
        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("session_secret must be at least 32 characters")
        keys = [self.audit_key, *self.audit_retired_keys.values()]
        if any(k is not None and len(k.get_secret_value()) < 32 for k in keys):
            raise ValueError("audit_key must be at least 32 characters")
        return self

    @model_validator(mode="after")
    def argon2_meets_owasp(self) -> Self:
        if self.argon2_memory_kib < 47_104 and self.argon2_time_cost < 2:
            raise ValueError("Argon2id below the OWASP floor: need m>=47104 or t>=2")
        return self


class ObservabilityConfig(BaseModel):
    """ADR-0015. Tracing is a no-op unless an exporter is chosen; OTLP must be a local collector."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    tracing_exporter: Literal["none", "console", "otlp"] = "none"
    otlp_endpoint: AnyHttpUrl | None = None
    service_name: str = "analyzer"
    # /metrics on the api app (outside /api, so the reverse proxy never exposes it).
    metrics_enabled: bool = True
    # Worker process metrics listener; None = off. Bind is container-internal (0.0.0.0 inside).
    worker_metrics_port: int | None = Field(default=None, ge=1024, le=65535)

    @model_validator(mode="after")
    def otlp_needs_endpoint(self) -> Self:
        if self.tracing_exporter == "otlp" and self.otlp_endpoint is None:
            raise ValueError("otlp_endpoint is required when tracing_exporter is 'otlp'")
        return self


class SqlConfig(BaseModel):
    """Analytical query caps (design section 65 `sql.*`), passed to AnalyticalEngine."""

    timeout_s: float = Field(default=10.0, gt=0)
    max_rows: int = Field(default=10_000, ge=1)
    memory_limit_mb: int = Field(default=2048, ge=16)  # BENCH-001
    threads: int = Field(default=2, ge=1)


class ConnectorConfig(BaseModel):
    """External database snapshot import (TASK-DATA-013). Deny by default: no host is reachable
    until listed. Resolved addresses are checked too: link-local/metadata, multicast and
    unspecified addresses are always refused, loopback only with `allow_loopback`."""

    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=lambda: [5432])
    allow_loopback: bool = False
    connect_timeout_s: int = Field(default=10, ge=1, le=120)
    statement_timeout_ms: int = Field(default=60_000, ge=100)
    max_rows: int = Field(default=1_000_000, ge=1)


class WorkerConfig(BaseModel):
    """Job worker resource limits (design section 65, TASK-MLOPS-008)."""

    concurrency: dict[str, int] = Field(default_factory=dict)  # job type -> slots (default 1)
    heartbeat_s: float = Field(default=10.0, gt=0)
    stale_after_s: float = Field(default=60.0, gt=0)
    retry_base_s: float = Field(default=5.0, ge=0)
    poll_s: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def sane(self) -> Self:
        if any(n < 1 or n > 32 for n in self.concurrency.values()):
            raise ValueError("worker concurrency per job type must be 1..32")
        if self.stale_after_s <= self.heartbeat_s:
            raise ValueError("stale_after_s must exceed heartbeat_s")
        return self


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    env: Environment = Environment.DEV
    database: DatabaseConfig
    storage: StorageConfig
    security: SecurityConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    sql: SqlConfig = Field(default_factory=SqlConfig)
    connectors: ConnectorConfig = Field(default_factory=ConnectorConfig)

    @model_validator(mode="after")
    def deployed_envs_are_hardened(self) -> Self:
        if self.env not in (Environment.STAGING, Environment.PROD):
            return self
        if not self.security.cookie_secure:
            raise ValueError("security.cookie_secure must be true in staging/prod")
        if self.security.audit_key is None:
            raise ValueError("security.audit_key is required in staging/prod")
        return self


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or invalid."""


def load_config() -> AppConfig:
    """Load and validate config; fail fast with a message that never echoes input values."""
    try:
        return AppConfig()  # fields come from the environment
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"Invalid configuration: {problems}") from None
