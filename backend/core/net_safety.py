"""Stops user-supplied webhook URLs from reaching internal services."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURL(ValueError):
    pass


def _check_scheme_and_host(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("Webhook URL must be http or https with a host")
    if parts.username or parts.password:
        raise UnsafeURL("Webhook URL must not contain credentials")
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


async def ensure_public_url(url: str) -> None:
    """Raises UnsafeURL unless every address the host resolves to is public."""
    host, port = _check_scheme_and_host(url)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURL("Webhook host could not be resolved")
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise UnsafeURL("Webhook URL must not point at a private or internal address")
