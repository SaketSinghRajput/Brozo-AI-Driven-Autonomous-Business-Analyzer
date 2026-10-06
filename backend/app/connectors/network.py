"""SSRF guard for outbound connector connections (TASK-DATA-013).

A target is reachable only when its host name is in `connectors.allowed_hosts` (deny by
default) and its port in `allowed_ports`. The name is then resolved and *every* address is
checked: link-local (169.254.0.0/16 incl. cloud metadata, fe80::/10), known metadata addresses,
multicast, unspecified and reserved ranges are always refused; loopback only with
`allow_loopback`. The caller connects to the returned address (libpq `hostaddr`), so a DNS
answer that changes between the check and the connect (rebinding) cannot redirect it.
"""

import ipaddress
import socket

from app.core.config import ConnectorConfig
from app.core.errors import DataError, SecurityError
from app.data.ingestion.errors import reject

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
# Metadata endpoints outside the link-local ranges (AWS IPv6, Alibaba Cloud).
_METADATA = {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}


def _refused(ip: IP, allow_loopback: bool) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip in _METADATA
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (ip.is_loopback and not allow_loopback)
    )


def resolve_target(host: str, port: int, cfg: ConnectorConfig) -> str:
    """Returns the address to connect to; SecurityError when the target is not allowed."""
    name = host.strip().lower().rstrip(".")
    if name not in {h.strip().lower().rstrip(".") for h in cfg.allowed_hosts}:
        raise reject(SecurityError, "CONNECTOR_HOST_NOT_ALLOWED", "This host is not allow-listed.")
    if port not in cfg.allowed_ports:
        raise reject(SecurityError, "CONNECTOR_PORT_NOT_ALLOWED", "This port is not allow-listed.")
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except socket.gaierror, UnicodeError:
        raise reject(
            DataError, "CONNECTOR_UNRESOLVABLE", "The host name does not resolve."
        ) from None
    addresses = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    if not addresses or any(_refused(ip, cfg.allow_loopback) for ip in addresses):
        raise reject(
            SecurityError, "CONNECTOR_ADDRESS_REFUSED", "The host resolves to a refused address."
        )
    return str(addresses[0])
