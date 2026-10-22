"""Fetching untrusted, user-supplied URLs without SSRF exposure.

A URL typed by a Discord user must never reach the dashboard, ComfyUI, the
host's LAN, or a cloud metadata endpoint. The check is enforced in the
connector's DNS resolver, so it applies at connect time to every hop of a
redirect chain and cannot be bypassed by DNS rebinding between a separate
"validate" lookup and the real connection.
"""

import ipaddress
import logging
import socket
from typing import Optional, Tuple
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from yarl import URL

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL is not allowed to be fetched. Message is user-facing."""


def is_public_ip(address: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> str:
    """Checks scheme and host syntax. Returns the URL stripped of whitespace."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        raise UnsafeURLError("That doesn't look like a valid URL.")
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http:// and https:// URLs are supported.")
    if not parts.hostname:
        raise UnsafeURLError("That URL has no host name.")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not supported.")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_ip(host):
        raise UnsafeURLError("That address is not reachable from here.")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeURLError("That address is not reachable from here.")
    return url


class PublicOnlyResolver(AbstractResolver):
    """Resolver that drops every non-public address from DNS answers."""

    def __init__(self, inner: Optional[AbstractResolver] = None):
        self._inner = inner or DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        results = await self._inner.resolve(host, port, family)
        allowed = [r for r in results if is_public_ip(r["host"])]
        if not allowed:
            logger.warning(f"[SafeFetch] Blocked non-public address for host '{host}'")
            raise UnsafeURLError("That address is not reachable from here.")
        return allowed

    async def close(self):
        await self._inner.close()


def create_safe_session(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> aiohttp.ClientSession:
    """A ClientSession that can only connect to public IP addresses."""
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout),
    )


async def fetch_text(session: aiohttp.ClientSession, url: str, *,
                     headers: Optional[dict] = None,
                     max_bytes: int = DEFAULT_MAX_BYTES) -> Tuple[int, str, str]:
    """Fetches a URL as text, following redirects manually and validating each hop.

    ``session`` should come from ``create_safe_session()``.

    Returns (status, final_url, text). The body is truncated at ``max_bytes``.
    Raises UnsafeURLError for disallowed URLs and aiohttp errors for
    network failures.
    """
    current = validate_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        async with session.get(current, headers=headers, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return response.status, current, ""
                current = validate_url(str(response.url.join(URL(location))))
                continue

            chunks = []
            received = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                remaining = max_bytes - received
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                received += len(chunks[-1])
            body = b"".join(chunks)
            charset = response.charset or "utf-8"
            try:
                text = body.decode(charset, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return response.status, current, text
    raise UnsafeURLError("Too many redirects.")
