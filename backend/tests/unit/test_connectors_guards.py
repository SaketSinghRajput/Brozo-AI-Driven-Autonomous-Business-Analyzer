"""TASK-DATA-013: SSRF guard and query/table validation (no database, no network: IP literals)."""

import pytest

from app.connectors.network import resolve_target
from app.connectors.postgres import select_query, table_parts
from app.core.config import ConnectorConfig
from app.core.errors import AppValidationError, SecurityError


def reason(exc: pytest.ExceptionInfo[Exception]) -> str:
    details = getattr(exc.value, "details", None) or {}
    return str(details.get("reason"))


def test_deny_by_default() -> None:
    with pytest.raises(SecurityError) as exc:
        resolve_target("10.0.0.5", 5432, ConnectorConfig())
    assert reason(exc) == "CONNECTOR_HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",  # cloud metadata (link-local)
        "fe80::1",
        "::ffff:169.254.169.254",  # IPv4-mapped metadata
        "fd00:ec2::254",  # AWS IPv6 metadata
        "100.100.100.200",  # Alibaba metadata
        "0.0.0.0",  # noqa: S104 (an address under test, not a bind)
        "224.0.0.1",
        "127.0.0.1",  # loopback without allow_loopback
        "::1",
    ],
)
def test_refused_addresses_even_when_allow_listed(host: str) -> None:
    with pytest.raises(SecurityError) as exc:
        resolve_target(host, 5432, ConnectorConfig(allowed_hosts=[host]))
    assert reason(exc) == "CONNECTOR_ADDRESS_REFUSED"


def test_loopback_only_when_allowed_and_port_allow_list() -> None:
    cfg = ConnectorConfig(allowed_hosts=["127.0.0.1"], allow_loopback=True)
    assert resolve_target("127.0.0.1", 5432, cfg) == "127.0.0.1"
    with pytest.raises(SecurityError) as exc:
        resolve_target("127.0.0.1", 22, cfg)
    assert reason(exc) == "CONNECTOR_PORT_NOT_ALLOWED"


def test_private_lan_address_is_allowed_when_listed() -> None:
    cfg = ConnectorConfig(allowed_hosts=["10.0.0.5"])
    assert resolve_target("10.0.0.5", 5432, cfg) == "10.0.0.5"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "UPDATE t SET a = 1",
        "SELECT 1; SELECT 2",
        "select 1 --",
        "SELECT /**/ 1",
        "selectx FROM t",
        "WITH a AS (SELECT 1) SELECT * FROM a",
        "SELECT " + "1" * 20_001,
    ],
)
def test_select_query_rejects(query: str) -> None:
    with pytest.raises(AppValidationError):
        select_query(query)


def test_select_query_accepts_one_select_and_strips_a_trailing_semicolon() -> None:
    assert select_query("  select a, b from t where a > 1;  ") == "select a, b from t where a > 1"


@pytest.mark.parametrize("name", ['a"b', "a.b.c", "1abc", "a b", "", "a;drop"])
def test_table_parts_rejects(name: str) -> None:
    with pytest.raises(AppValidationError):
        table_parts(name)


def test_table_parts_accepts_schema_qualified() -> None:
    assert table_parts("public.sales_2026") == ("public", "sales_2026")
