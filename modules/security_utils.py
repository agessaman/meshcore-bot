#!/usr/bin/env python3
"""
Security Utilities for MeshCore Bot
Provides centralized security validation functions to prevent common attacks
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import platform
import re
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
import requests
from aiohttp.abc import AbstractResolver
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.connection import create_connection

if TYPE_CHECKING:
    from aiohttp.abc import ResolveResult

logger = logging.getLogger('MeshCoreBot.Security')

# CGN (Carrier-Grade NAT) network 100.64.0.0/10 - RFC 6598
_CGN_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# Well-known instance/task metadata addresses must never receive feed requests,
# even when an administrator explicitly enables ordinary private-network feeds.
_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),  # AWS, Azure, GCP, and others
    ipaddress.ip_address("169.254.170.2"),  # AWS ECS task credentials
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 metadata
}
_METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SAFE_REDIRECT_HEADERS = {"accept", "accept-encoding", "user-agent"}


class UnsafeUrlError(ValueError):
    """Raised when a URL or any of its resolved destinations violates policy."""


class SafeUrlPolicy:
    """Reusable SSRF policy for parsing, DNS resolution, redirects, and connects."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        allow_loopback: bool | None = None,
        timeout: float = 2.0,
        max_redirects: int = 5,
    ) -> None:
        self.allow_private = allow_private
        self.allow_loopback = allow_loopback
        # Portable synchronous DNS has no per-call timeout. Async callers and
        # the aiohttp connect-time resolver enforce this value with wait_for().
        self.timeout = timeout
        self.max_redirects = max_redirects

    def parse(self, url: str) -> tuple[str, str, int]:
        """Strictly parse an HTTP(S) URL and return scheme, hostname, and port."""
        if not isinstance(url, str) or not url:
            raise UnsafeUrlError("URL must be a non-empty string")
        if re.search(r"[\x00-\x20\x7f]", url):
            raise UnsafeUrlError("URL contains control characters or whitespace")
        # WHATWG-style parsers may treat backslashes as separators while
        # urllib.parse does not. Reject them so every layer sees one authority.
        if "\\" in url:
            raise UnsafeUrlError("URL contains an ambiguous backslash")

        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise UnsafeUrlError(f"Invalid URL authority: {exc}") from exc

        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise UnsafeUrlError(f"URL scheme not allowed: {parsed.scheme}")
        if not parsed.netloc or not parsed.hostname:
            raise UnsafeUrlError("URL is missing a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeUrlError("Embedded URL credentials are not allowed")

        raw_hostname = parsed.hostname.lower()
        if raw_hostname.endswith(".."):
            raise UnsafeUrlError("URL contains an ambiguous hostname")
        hostname = raw_hostname.rstrip(".")
        if not hostname or ".." in hostname or "%" in hostname:
            raise UnsafeUrlError("URL contains an ambiguous hostname")
        if hostname in _METADATA_HOSTNAMES:
            raise UnsafeUrlError("Cloud metadata hostnames are not allowed")

        # IPv6 literals must be bracketed. Numeric-looking IPv4 hosts must use
        # canonical dotted decimal; this rejects octal, hexadecimal, and dword
        # spellings accepted inconsistently by DNS/socket implementations.
        if ":" in hostname and not parsed.netloc.startswith("["):
            raise UnsafeUrlError("IPv6 URL literals must be bracketed")
        numeric_ipv4 = bool(re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9.]+)", hostname))
        if numeric_ipv4:
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError as exc:
                raise UnsafeUrlError("Non-canonical numeric IP address") from exc
            if not isinstance(address, ipaddress.IPv4Address) or str(address) != hostname:
                raise UnsafeUrlError("Non-canonical numeric IP address")

        return scheme, hostname, port or (443 if scheme == "https" else 80)

    def _check_address(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        # Canonicalize IPv4-mapped IPv6 (e.g. ``::ffff:169.254.169.254``) to the
        # IPv4 target the socket layer will actually dial. The mapped form is a
        # distinct address object that is absent from _METADATA_ADDRESSES and
        # reports is_reserved=False/is_global=False, so without this it would
        # bypass the metadata and non-unicast checks under allow_private=True.
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address in _METADATA_ADDRESSES:
            raise UnsafeUrlError(f"Cloud metadata address is not allowed: {address}")
        # These cannot be meaningful unicast HTTP server destinations. They
        # remain blocked even under the explicit private-network exception.
        if address.is_unspecified or address.is_multicast or address.is_reserved:
            raise UnsafeUrlError(f"Non-unicast destination is not allowed: {address}")

        if self.allow_loopback is True:
            if not address.is_loopback:
                raise UnsafeUrlError(f"Non-loopback destination is not allowed: {address}")
            return
        if self.allow_private:
            return
        if not address.is_global or address in _CGN_NETWORK:
            raise UnsafeUrlError(f"Private or non-global destination is not allowed: {address}")

    def validate_records(self, records: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        """Validate every A/AAAA answer before any one of them can be used."""
        if not records:
            raise UnsafeUrlError("Hostname did not resolve to an address")
        validated: list[tuple[Any, ...]] = []
        seen: set[tuple[int, str]] = set()
        for record in records:
            family, _socktype, _proto, _canonname, sockaddr = record
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = ipaddress.ip_address(sockaddr[0])
            self._check_address(address)
            key = (family, str(address))
            if key not in seen:
                seen.add(key)
                validated.append(record)
        if not validated:
            raise UnsafeUrlError("Hostname did not resolve to an A or AAAA address")
        return validated

    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[tuple[Any, ...]]:
        """Resolve and validate all socket addresses for a host."""
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                family=family,
                type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, socket.timeout, OSError) as exc:
            raise UnsafeUrlError(f"Failed to resolve hostname {hostname}: {exc}") from exc
        return self.validate_records(records)

    def validate(self, url: str) -> bool:
        """Parse and resolve a URL, raising UnsafeUrlError on policy failure."""
        _scheme, hostname, port = self.parse(url)
        self.resolve(hostname, port)
        return True

    async def validate_async(self, url: str) -> bool:
        """Asynchronously parse and resolve a URL without blocking the event loop."""
        _scheme, hostname, port = self.parse(url)
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.getaddrinfo(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                ),
                timeout=self.timeout,
            )
        except (asyncio.TimeoutError, socket.gaierror, socket.timeout, OSError) as exc:
            raise UnsafeUrlError(f"Failed to resolve hostname {hostname}: {exc}") from exc
        self.validate_records(records)
        return True


class SafeAiohttpResolver(AbstractResolver):
    """aiohttp resolver that validates the exact addresses used to connect."""

    def __init__(self, policy: SafeUrlPolicy) -> None:
        self.policy = policy

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.getaddrinfo(
                    host,
                    port,
                    family=family,
                    type=socket.SOCK_STREAM,
                ),
                timeout=self.policy.timeout,
            )
            records = self.policy.validate_records(records)
        except (
            UnsafeUrlError,
            asyncio.TimeoutError,
            socket.gaierror,
            socket.timeout,
            OSError,
        ) as exc:
            raise OSError(f"Failed to resolve safe destination {host}: {exc}") from exc
        return [
            {
                "hostname": host,
                "host": str(record[4][0]),
                "port": int(record[4][1]),
                "family": record[0],
                "proto": record[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for record in records
        ]

    async def close(self) -> None:
        return None


def _redirect_target(current_url: str, location: str, policy: SafeUrlPolicy) -> str:
    target = urljoin(current_url, location)
    policy.validate(target)
    return target


def _same_origin(left: str, right: str, policy: SafeUrlPolicy) -> bool:
    return policy.parse(left) == policy.parse(right)


def _headers_for_redirect(
    headers: dict[str, str] | None,
    *,
    same_origin: bool,
) -> dict[str, str] | None:
    if headers is None or same_origin:
        return headers
    # Arbitrary API headers often contain tokens under provider-specific names.
    # On a cross-origin redirect retain only headers known not to be credentials.
    return {key: value for key, value in headers.items() if key.lower() in _SAFE_REDIRECT_HEADERS}


async def safe_aiohttp_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    policy: SafeUrlPolicy,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> aiohttp.ClientResponse:
    """Issue a request with bounded, policy-checked manual redirects."""
    current_url = url
    current_method = method.upper()
    current_headers = headers
    for redirect_count in range(policy.max_redirects + 1):
        await policy.validate_async(current_url)
        response = await session.request(
            current_method,
            current_url,
            headers=current_headers,
            allow_redirects=False,
            **kwargs,
        )
        if response.status not in _REDIRECT_STATUSES or not response.headers.get("Location"):
            return response
        if redirect_count >= policy.max_redirects:
            response.release()
            raise UnsafeUrlError("Too many redirects")

        try:
            target = urljoin(current_url, response.headers["Location"])
            await policy.validate_async(target)
            same_origin = _same_origin(current_url, target, policy)
            current_headers = _headers_for_redirect(current_headers, same_origin=same_origin)
            if not same_origin and (
                current_method not in {"GET", "HEAD"}
                or kwargs.get("json") is not None
                or kwargs.get("data") is not None
            ):
                raise UnsafeUrlError("Cross-origin redirects cannot forward request bodies")
            # Query parameters belong only to the originally configured endpoint.
            # A redirect's Location carries its own query string.
            kwargs.pop("params", None)
            if response.status == 303 or (
                response.status in {301, 302} and current_method == "POST"
            ):
                current_method = "GET"
                kwargs.pop("json", None)
                kwargs.pop("data", None)
        finally:
            response.release()
        current_url = target
    raise UnsafeUrlError("Too many redirects")


def _pinned_connection_classes(
    policy: SafeUrlPolicy,
) -> tuple[type[HTTPConnectionPool], type[HTTPSConnectionPool]]:
    def _new_connection(connection: HTTPConnection) -> socket.socket:
        hostname = connection.host
        port = connection.port or connection.default_port
        if port is None:
            raise NewConnectionError(connection, "Destination has no port")
        try:
            records = policy.resolve(hostname, port)
            last_error: OSError | None = None
            for record in records:
                try:
                    return create_connection(
                        (record[4][0], port),
                        timeout=connection.timeout,
                        source_address=connection.source_address,
                        socket_options=connection.socket_options,
                    )
                except OSError as exc:
                    last_error = exc
            raise last_error or OSError("No validated destination was connectable")
        except (UnsafeUrlError, OSError) as exc:
            raise NewConnectionError(connection, f"Blocked or failed destination {hostname}: {exc}") from exc

    class PinnedHTTPConnection(HTTPConnection):
        def _new_conn(self) -> socket.socket:
            return _new_connection(self)

    class PinnedHTTPSConnection(HTTPSConnection):
        def _new_conn(self) -> socket.socket:
            return _new_connection(self)

    class PinnedHTTPConnectionPool(HTTPConnectionPool):
        ConnectionCls = PinnedHTTPConnection

    class PinnedHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPSConnection

    return PinnedHTTPConnectionPool, PinnedHTTPSConnectionPool


class SafeRequestsAdapter(HTTPAdapter):
    """requests adapter that validates DNS answers at socket creation time."""

    def __init__(self, policy: SafeUrlPolicy, *args: Any, **kwargs: Any) -> None:
        self.policy = policy
        super().__init__(*args, **kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        manager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs)
        http_pool, https_pool = _pinned_connection_classes(self.policy)
        manager.pool_classes_by_scheme = {"http": http_pool, "https": https_pool}
        self.poolmanager = manager


def create_safe_requests_session(policy: SafeUrlPolicy) -> requests.Session:
    """Create a no-proxy requests session with connect-time destination enforcement."""
    session = requests.Session()
    session.trust_env = False
    adapter = SafeRequestsAdapter(policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_requests_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    policy: SafeUrlPolicy,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Issue a requests call with bounded, policy-checked manual redirects."""
    current_url = url
    current_method = method.upper()
    current_headers = headers
    for redirect_count in range(policy.max_redirects + 1):
        policy.validate(current_url)
        response = session.request(
            current_method,
            current_url,
            headers=current_headers,
            allow_redirects=False,
            **kwargs,
        )
        if response.status_code not in _REDIRECT_STATUSES or not response.headers.get("Location"):
            return response
        if redirect_count >= policy.max_redirects:
            response.close()
            raise UnsafeUrlError("Too many redirects")

        try:
            target = _redirect_target(current_url, response.headers["Location"], policy)
            same_origin = _same_origin(current_url, target, policy)
            current_headers = _headers_for_redirect(current_headers, same_origin=same_origin)
            if not same_origin and (
                current_method not in {"GET", "HEAD"}
                or kwargs.get("json") is not None
                or kwargs.get("data") is not None
            ):
                raise UnsafeUrlError("Cross-origin redirects cannot forward request bodies")
            kwargs.pop("params", None)
            if response.status_code == 303 or (
                response.status_code in {301, 302} and current_method == "POST"
            ):
                current_method = "GET"
                kwargs.pop("json", None)
                kwargs.pop("data", None)
        finally:
            response.close()
        current_url = target
    raise UnsafeUrlError("Too many redirects")


def _is_nix_environment() -> bool:
    """
    Detect if running in a Nix environment

    Returns:
        True if running in Nix build or NixOS
    """
    # Check for Nix store path (most reliable indicator)
    if 'NIX_STORE' in os.environ:
        return True

    # Check if we're in a Nix store path
    try:
        current_path = Path.cwd().resolve()
        # Nix store paths typically look like /nix/store/<hash>-<name>
        if '/nix/store/' in str(current_path):
            return True
    except Exception:
        pass

    # Check for Nix-related environment variables
    nix_env_vars = ['NIX_PATH', 'NIX_REMOTE', 'IN_NIX_SHELL']
    return bool(any(var in os.environ for var in nix_env_vars))


def validate_external_url(
    url: str,
    allow_private: bool = False,
    allow_loopback: bool | None = None,  # Deprecated: use allow_private=True instead
    timeout: float = 2.0,
) -> bool:
    """
    Validate that URL points to safe external resource (SSRF protection)

    Args:
        url: URL to validate
        allow_private: Whether to allow private/internal IPs (default: False)
        allow_loopback: If True, only loopback addresses are permitted. Deprecated for
            broad internal access; use allow_private=True instead.
        timeout: Retained for API compatibility. The system resolver does not
            provide a portable per-call DNS timeout.

    Returns:
        True if URL is safe, False otherwise

    Raises:
        ValueError: If URL is invalid or unsafe

    Note:
        - allow_loopback=True only permits loopback addresses (127.0.0.1, ::1)
        - allow_private=True permits all internal ranges (loopback, RFC1918, CGN, link-local)
    """
    try:
        return SafeUrlPolicy(
            allow_private=allow_private,
            allow_loopback=allow_loopback,
            timeout=timeout,
        ).validate(url)
    except (UnsafeUrlError, ValueError) as e:
        logger.warning(f"URL validation failed: {e}")
        return False


def validate_safe_path(file_path: str, base_dir: str = '.', allow_absolute: bool = False) -> Path:
    """
    Validate that path is safe with configurable restrictions

    When allow_absolute=False (default):
    - Paths must be within base_dir (prevents path traversal)
    - Blocks dangerous system directories

    When allow_absolute=True:
    - Allows paths outside base_dir (for user-configured database/log locations)
    - Still blocks dangerous system directories
    - In Nix environments, dangerous directory checks are relaxed (Nix provides isolation)

    Args:
        file_path: Path to validate
        base_dir: Base directory that path must be within (when allow_absolute=False)
        allow_absolute: Whether to allow absolute paths outside base_dir

    Returns:
        Resolved Path object if safe

    Raises:
        ValueError: If path is unsafe or attempts traversal
    """
    try:
        # Resolve base directory to absolute path
        base = Path(base_dir).resolve()

        # Resolve target path relative to base_dir (not current working directory)
        # If file_path is absolute, use it directly; otherwise join with base_dir
        if Path(file_path).is_absolute():
            target = Path(file_path).resolve()
        else:
            # Join with base_dir first, then resolve to handle relative paths correctly
            target = (base / file_path).resolve()

        # If absolute paths are not allowed, ensure target is within base
        if not allow_absolute:
            # Check if target is within base directory
            try:
                target.relative_to(base)
            except ValueError:
                raise ValueError(
                    f"Path traversal detected: {file_path} is not within {base_dir}"
                )

        # Check for dangerous system paths (OS-specific)
        # In Nix environments, skip this check as Nix provides strong isolation
        is_nix = _is_nix_environment()

        if not is_nix:
            system = platform.system()
            if system == 'Windows':
                dangerous_prefixes = [
                    'C:\\Windows\\System32',
                    'C:\\Windows\\SysWOW64',
                    'C:\\Program Files',
                    'C:\\ProgramData',
                    'C:\\Windows\\System',
                ]
                # Check against both forward and backslash paths
                target_str = str(target).lower()
                dangerous = any(target_str.startswith(prefix.lower()) for prefix in dangerous_prefixes)
            elif system == 'Darwin':  # macOS
                dangerous_prefixes = [
                    '/System',
                    '/Library',
                    '/usr/bin',
                    '/usr/sbin',
                    '/sbin',
                    '/bin',
                    '/private/etc',
                    '/private/var/root',
                    '/private/var/db',
                ]
                target_str = str(target)
                dangerous = any(target_str.startswith(prefix) for prefix in dangerous_prefixes)
            else:  # Linux and other Unix-like systems
                dangerous_prefixes = ['/etc', '/sys', '/proc', '/dev', '/bin', '/sbin', '/boot']
                target_str = str(target)
                dangerous = any(target_str.startswith(prefix) for prefix in dangerous_prefixes)

            if dangerous:
                raise ValueError(f"Access to system directory denied: {file_path}")

        return target

    except ValueError:
        # Re-raise ValueError as-is (these are our validation errors)
        raise
    except Exception as e:
        raise ValueError(f"Invalid or unsafe file path: {file_path} - {e}")


def sanitize_input(content: str, max_length: Optional[int] = 500, strip_controls: bool = True) -> str:
    """
    Sanitize user input to prevent injection attacks

    Args:
        content: Input string to sanitize
        max_length: Maximum allowed length (default: 500 chars, None to disable length check)
        strip_controls: Whether to remove control characters (default: True)

    Returns:
        Sanitized string

    Raises:
        ValueError: If max_length is negative
    """
    if not isinstance(content, str):
        content = str(content)

    # Validate max_length if provided
    if max_length is not None:
        if max_length < 0:
            raise ValueError(f"max_length must be non-negative, got {max_length}")
        # Limit length to prevent DoS
        if len(content) > max_length:
            content = content[:max_length]
            logger.debug(f"Input truncated to {max_length} characters")

    # Remove control characters except newline, carriage return, tab
    if strip_controls:
        # Keep only printable characters plus common whitespace
        content = ''.join(
            char for char in content
            if ord(char) >= 32 or char in '\n\r\t'
        )

    # Remove null bytes (can cause issues in C libraries)
    content = content.replace('\x00', '')

    return content.strip()


def sanitize_name(name: object, max_length: int = 64) -> str:
    """
    Sanitize a short identifier (node name, channel name, etc.) for safe logging and storage.

    Strips all control characters including newlines, carriage returns, tabs,
    null bytes, and ANSI escape sequences.  Truncates to max_length.

    Args:
        name: Value to sanitize (coerced to str if not already).
        max_length: Maximum character length (default: 64).

    Returns:
        Sanitized string.

    Raises:
        ValueError: If max_length is negative.
    """
    if max_length < 0:
        raise ValueError(f"max_length must be non-negative, got {max_length}")
    text = str(name) if not isinstance(name, str) else name
    # Strip all control characters (including \n \r \t \x00 and ANSI escapes)
    text = re.sub(r'[\x00-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]', '', text)
    return text[:max_length]


# Valid SQLite journal modes for PRAGMA journal_mode validation
VALID_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}

# Valid SQLite synchronous levels for PRAGMA synchronous validation
VALID_SYNCHRONOUS_MODES = {"OFF", "NORMAL", "FULL", "EXTRA"}


def validate_sql_identifier(identifier: str) -> str:
    """
    Validate a SQL identifier (table or column name) for safe interpolation.

    Only allows alphanumeric characters and underscores, must start with
    a letter or underscore. This is intentionally strict to prevent SQL injection
    when parameterized queries cannot be used (e.g., PRAGMA, REINDEX).

    Args:
        identifier: The SQL identifier to validate

    Returns:
        The validated identifier string

    Raises:
        ValueError: If the identifier contains unsafe characters
    """
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier


def validate_api_key_format(api_key: str, min_length: int = 16) -> bool:
    """
    Validate API key format

    Args:
        api_key: API key to validate
        min_length: Minimum required length (default: 16)

    Returns:
        True if format is valid, False otherwise
    """
    if not isinstance(api_key, str):
        return False

    # Check minimum length
    if len(api_key) < min_length:
        return False

    # Check for obviously invalid patterns
    invalid_patterns = [
        'your_api_key_here',
        'placeholder',
        'example',
        'test_key',
        '12345',
        'aaaa',
    ]

    api_key_lower = api_key.lower()
    if any(pattern in api_key_lower for pattern in invalid_patterns):
        return False

    # Check that it's not all the same character
    return not len(set(api_key)) < 3


def validate_pubkey_format(pubkey: str, expected_length: int = 64) -> bool:
    """
    Validate public key format (hex string)

    Args:
        pubkey: Public key to validate
        expected_length: Expected length in characters (default: 64 for ed25519)

    Returns:
        True if format is valid, False otherwise
    """
    if not isinstance(pubkey, str):
        return False

    # Check exact length
    if len(pubkey) != expected_length:
        return False

    # Check hex format
    return bool(re.match(r'^[0-9a-fA-F]+$', pubkey))


def validate_port_number(port: int, allow_privileged: bool = False) -> bool:
    """
    Validate port number

    Args:
        port: Port number to validate
        allow_privileged: Whether to allow privileged ports <1024 (default: False)

    Returns:
        True if port is valid, False otherwise
    """
    if not isinstance(port, int):
        return False

    min_port = 1 if allow_privileged else 1024
    max_port = 65535

    return min_port <= port <= max_port


def validate_integer_range(value: int, min_value: int, max_value: int, name: str = "value") -> bool:
    """
    Validate integer is within range

    Args:
        value: Integer to validate
        min_value: Minimum allowed value (inclusive)
        max_value: Maximum allowed value (inclusive)
        name: Name of the value for error messages

    Returns:
        True if valid

    Raises:
        ValueError: If value is out of range
    """
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}")

    if value < min_value or value > max_value:
        raise ValueError(
            f"{name} must be between {min_value} and {max_value}, got {value}"
        )

    return True
