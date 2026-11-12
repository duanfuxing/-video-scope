"""Bounded, SSRF-resistant downloads of public video files."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from video_scope.media import probe

MAX_DOWNLOAD_BYTES = 14 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHUNK_SIZE = 128 * 1024


class DownloadError(ValueError):
    """A safe-to-record download failure which never includes the source URL."""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        context = ssl.create_default_context()
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._address = address
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._address, self.port), self.timeout)
        try:
            # The connection is pinned to the validated address, while SNI and
            # certificate verification continue to use the requested hostname.
            self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DownloadError("media host DNS resolution failed") from exc

    addresses: set[str] = set()
    for record in records:
        raw = record[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise DownloadError("media host resolved to an invalid address") from exc
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise DownloadError("media host did not resolve exclusively to public addresses")
        addresses.add(address.compressed)
    if not addresses:
        raise DownloadError("media host DNS resolution returned no usable addresses")
    return sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))


def _checked_url(url: str) -> tuple[SplitResult, str, int, list[str]]:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise DownloadError("media URL is missing or too long")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("media URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise DownloadError("media URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise DownloadError("media URL must not contain user information")
    raw_host = parsed.hostname
    if not raw_host:
        raise DownloadError("media URL has no hostname")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
    try:
        host = raw_host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DownloadError("media URL hostname is invalid") from exc
    port = port or (443 if scheme == "https" else 80)
    return parsed, host, port, _public_addresses(host, port)


def _request_target(parsed: SplitResult) -> str:
    return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _host_header(host: str, port: int, scheme: str) -> str:
    rendered = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return rendered if port == default else f"{rendered}:{port}"


def _open_response(
    url: str, timeout: float
) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
    parsed, host, port, addresses = _checked_url(url)
    last_error: Exception | None = None
    for address in addresses:
        connection_type = (
            _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection
        )
        connection = connection_type(host, port, address, timeout)
        try:
            connection.request(
                "GET",
                _request_target(parsed),
                headers={
                    "Host": _host_header(host, port, parsed.scheme.lower()),
                    "User-Agent": "VideoScope/0.1",
                    "Accept": "video/*,application/octet-stream",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            return connection.getresponse(), connection
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            connection.close()
    raise DownloadError("media connection failed") from last_error


def _content_length(response: http.client.HTTPResponse, max_bytes: int) -> None:
    raw = response.getheader("Content-Length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError as exc:
        raise DownloadError("media response has an invalid content length") from exc
    if length < 0 or length > max_bytes:
        raise DownloadError("media response exceeds the download size limit")


def download_media(
    url: str,
    destination: Path,
    *,
    timeout: float = 30.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    max_redirects: int = 3,
) -> dict[str, Any]:
    """Download and probe one public HTTP(S) video without re-resolving its DNS.

    Each redirect is independently parsed, DNS-resolved, and restricted to public
    unicast addresses. The returned mapping contains ``path``, ``source_sha256``,
    ``size``, ``duration``, ``has_audio``, and the probed ``media`` mapping.
    ``destination`` is published only after a complete bounded transfer and a
    successful local ffprobe.
    """

    if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ValueError("download limits must be positive")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("download destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    current = url
    response: http.client.HTTPResponse | None = None
    connection: http.client.HTTPConnection | None = None
    try:
        for redirect_count in range(max_redirects + 1):
            response, connection = _open_response(current, timeout)
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                response.close()
                connection.close()
                response = None
                connection = None
                if location is None or redirect_count == max_redirects:
                    raise DownloadError("media redirect limit exceeded or location missing")
                current = urljoin(current, location)
                # Validate before the next request so disallowed redirects fail
                # even if no network operation would otherwise occur.
                _checked_url(current)
                continue
            if not 200 <= response.status < 300:
                raise DownloadError(f"media server returned HTTP status {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise DownloadError("encoded media responses are not supported")
            _content_length(response, max_bytes)
            break
        else:  # pragma: no cover - loop exits by return, break, or explicit error
            raise DownloadError("media redirect limit exceeded")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}-", suffix=".part", dir=destination.parent
        )
        temporary = Path(temporary_name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                assert response is not None
                while chunk := response.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise DownloadError("media response exceeds the download size limit")
                    output.write(chunk)
                    hasher.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise DownloadError("media response was empty")
            try:
                media = probe(temporary)
            except (ValueError, RuntimeError) as exc:
                raise DownloadError("downloaded response is not supported video media") from exc
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ValueError("download destination already exists") from exc
            return {
                "path": str(destination.resolve()),
                "source_sha256": hasher.hexdigest(),
                "size": size,
                "duration": media.duration,
                "has_audio": media.has_audio,
                "media": media.model_dump(),
            }
        finally:
            temporary.unlink(missing_ok=True)
    except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise DownloadError("media transfer failed") from exc
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
