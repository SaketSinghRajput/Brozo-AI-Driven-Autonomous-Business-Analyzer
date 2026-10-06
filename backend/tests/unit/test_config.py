from pathlib import Path

import pytest

from app.core.config import AppConfig, ConfigError, Environment, load_config

REQUIRED = {
    "APP_DATABASE__URL": "postgresql+psycopg://u:p@127.0.0.1:5432/db",
    "APP_STORAGE__DATASTORE_PATH": "./datastore",
    "APP_SECURITY__SESSION_SECRET": "s" * 40,
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # ensure no developer .env is read
    for key in [*REQUIRED, "APP_ENV", "APP_LLM__ENDPOINT"]:
        monkeypatch.delenv(key, raising=False)


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_SECURITY__AUDIT_KEY", "a" * 32)
    cfg = load_config()
    assert cfg.env is Environment.PROD
    assert cfg.database.url.get_secret_value().endswith("/db")


def test_missing_required_values_fail_fast() -> None:
    with pytest.raises(ConfigError, match=r"database: Field required"):
        load_config()


def test_error_message_never_contains_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_SECURITY__SESSION_SECRET", "too-short-secret-value")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert "too-short-secret-value" not in str(info.value)
    assert "session_secret must be at least 32 characters" in str(info.value)


def test_secrets_hidden_in_repr(config: AppConfig) -> None:
    text = repr(config)
    assert "t" * 32 not in text
    assert "test:test" not in text


def test_llm_endpoint_must_be_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_LLM__ENDPOINT", "https://api.example.com/v1")
    with pytest.raises(ConfigError, match="not in allowed_hosts"):
        load_config()


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_insecure_cookie_rejected_outside_dev(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("APP_SECURITY__COOKIE_SECURE", "false")
    with pytest.raises(ConfigError, match="cookie_secure"):
        load_config()


def test_insecure_cookie_allowed_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_SECURITY__COOKIE_SECURE", "false")
    assert load_config().security.cookie_secure is False


def test_invalid_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ConfigError, match="env"):
        load_config()


def test_resource_limits_reach_their_consumers(monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK-MLOPS-008: design section 65 limits come from config, not code defaults."""
    from app.analytics.sql.engine import AnalyticalEngine
    from app.core.container import build_container

    _set_required(monkeypatch)
    monkeypatch.setenv("APP_SQL__MAX_ROWS", "123")
    monkeypatch.setenv("APP_SQL__TIMEOUT_S", "4")
    monkeypatch.setenv("APP_WORKER__CONCURRENCY", '{"ingest": 3}')
    cfg = load_config()
    assert cfg.worker.concurrency == {"ingest": 3}
    tools = build_container(cfg).tools._tools.values()
    engines = [e for t in tools if isinstance(e := getattr(t, "_engine", None), AnalyticalEngine)]
    assert engines
    assert {(e.limits.max_rows, e.limits.timeout_s) for e in engines} == {(123, 4.0)}


def test_worker_limits_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("APP_WORKER__CONCURRENCY", '{"ingest": 0}')
    with pytest.raises(ConfigError, match="concurrency"):
        load_config()
    monkeypatch.setenv("APP_WORKER__CONCURRENCY", "{}")
    monkeypatch.setenv("APP_WORKER__STALE_AFTER_S", "5")
    with pytest.raises(ConfigError, match="stale_after_s"):
        load_config()
