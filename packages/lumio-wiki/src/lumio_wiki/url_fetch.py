"""Safe, bounded URL fetch boundary for URL Knowledge Source ingestion (issue #178).

The ONE network seam behind ``lumio-wiki ingest-url`` / ``ingest-research``.
Stdlib-only (``urllib`` + ``socket``) so the base wheel stays dependency-free
and model-free. Fail-closed safety by default (the command must not become a
general SSRF primitive):

* **HTTPS only** by default — plain ``http://`` is rejected unless an
  intranet deployment explicitly passes ``--allow-http``.
* **Private destinations rejected** — loopback (all of 127/8 and ::1),
  link-local (169.254/16, fe80::/10, cloud metadata), private networks
  (10/8, 172.16/12, 192.168/16, fc00::/7), and unspecified addresses. Every
  address the host resolves to is checked BEFORE connecting, and every
  redirect hop is re-validated (scheme + host + resolved addresses), so a
  public URL cannot bounce through or into an internal endpoint.
* **Credential-bearing URLs rejected** — ``https://user:pass@host/`` never
  runs.
* **Bounded bytes / time / redirects** — ``max_bytes`` is enforced against
  ``Content-Length`` when declared AND while streaming (a server that lies
  about length is still cut off), the socket timeout bounds each hop, and the
  redirect budget is finite.
* **Truthful provenance** — the result records the FINAL URL after redirects,
  the content hash over the exact bytes read, the retrieval time, a safe
  filename, the declared media type, and the converter/distiller identity
  stays "host agent" (managed ingest; no converter runs).

Raw fetched bytes never enter the Knowledge Base; they are registered in the
PRIVATE Source Registry (hash identity) and optionally retained as a private
Source Artifact when retention is configured (ADR-0020). Publication always
goes through the ordinary staged-proposal review pipeline.
"""

from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "SAFE_MEDIA_TYPES",
    "UrlFetchError",
    "UrlFetchPolicy",
    "UrlFetchResult",
    "fetch_url",
]

# ponytail: 10 MiB / 20 s / 5 hops cover ordinary article/docs pages; raise the
# knobs for heavier sources when a real need appears.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_REDIRECTS = 5

# Media types the fetch boundary accepts for ingestion. The fetched payload is
# provenance + optional private artifact retention, never automatic page
# content, so this bounds what may be retained — text-ish documents and the
# document formats the [documents] converters know. Anything else fails with
# an actionable error naming the observed type.
SAFE_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "application/xhtml+xml",
        "application/pdf",
        "application/json",
        "application/xml",
        "text/xml",
        # Common parameters observed in the wild pass through unharmed.
        "text/html; charset=utf-8",
        "text/plain; charset=utf-8",
    }
)


class UrlFetchError(ValueError):
    """A URL was rejected by policy or failed bounded retrieval."""


@dataclass(frozen=True)
class UrlFetchPolicy:
    """Bounds and escape hatches for :func:`fetch_url`.

    ``allow_http`` and ``allow_private_destinations`` exist for explicit
    intranet deployments (MinIO-style local endpoints); both default OFF so
    the public command fails closed against SSRF.
    """

    max_bytes: int = DEFAULT_MAX_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    allow_http: bool = False
    allow_private_destinations: bool = False
    allowed_media_types: frozenset[str] = field(default=SAFE_MEDIA_TYPES)


@dataclass(frozen=True)
class UrlFetchResult:
    """Truthful provenance for one fetched URL Knowledge Source."""

    original_url: str
    final_url: str
    body: bytes
    media_type: str
    filename: str
    content_hash: str
    retrieved_at: str
    redirects: int


def _validate_scheme(url: str, *, allow_http: bool) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise UrlFetchError(f"unsupported URL scheme {scheme!r}: https:// is required")
    if scheme == "http" and not allow_http:
        raise UrlFetchError(
            "http:// is rejected by default (https:// only); pass --allow-http "
            "explicitly for a trusted local endpoint"
        )
    return parsed


def _validate_no_credentials(parsed: urllib.parse.ParseResult) -> None:
    # ``user:pass@`` / ``token@`` in the URL itself are credentials in the
    # address; reject them rather than transmit (or persist) them.
    if parsed.username or parsed.password:
        raise UrlFetchError(
            "credential-bearing URLs are rejected: pass credentials through "
            "the environment, not the URL"
        )


def _resolve_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlFetchError(f"could not resolve host {host!r}: {exc}") from exc
    return [str(info[4][0]) for info in infos]


def _reject_private_addresses(addresses: list[str]) -> None:
    for address in addresses:
        if _is_private_address(address):
            raise UrlFetchError(
                "destination resolves to a private/loopback/link-local address "
                f"({address}); pass --allow-private-destination explicitly for a "
                "trusted local endpoint"
            )


def _is_private_address(address: str) -> bool:
    """Reject every non-public-unicast address (fail closed).

    Built on the stdlib ``ipaddress`` registry rather than hand-rolled CIDR
    checks (standards review: hand-rolling missed TEST-NET 192.0.2.0/24,
    benchmarking 198.18.0.0/15, documentation 2001:db8::/32, reserved
    240.0.0.0/4, and others that can be internally routed). ``not is_global``
    covers loopback/link-local/private/ULA/unspecified/reserved/special
    ranges; multicast is explicitly rejected because some multicast space
    reports ``is_global``; the deprecated IPv6 site-local prefix ``fec0::/10``
    is not classified by ``ipaddress`` at all, so it is checked explicitly.
    """
    import ipaddress

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False  # not an IP literal; policy applied via resolution
    if ip.version == 6 and ip in ipaddress.IPv6Network("fec0::/10"):
        return True  # deprecated site-local — never routed publicly
    return bool(ip.is_multicast or not ip.is_global)


def _safe_filename(url: str, media_type: str) -> str:
    """Derive a safe, portable filename from the FINAL URL path."""
    from lumio_wiki.source_registry import safe_artifact_filename

    path = urllib.parse.urlparse(url).path
    base = path.rsplit("/", 1)[-1] if path else ""
    name = safe_artifact_filename(base)
    if name and "." in name:
        return name
    # No usable name in the URL: name it from the media type.
    subtype = media_type.split(";", 1)[0].split("/", 1)[-1]
    ext = {"html": "html", "plain": "txt", "markdown": "md"}.get(subtype, "bin")
    host = urllib.parse.urlparse(url).netloc.split("@")[-1]
    return safe_artifact_filename(f"{host}.{ext}") or f"source.{ext}"


def _normalize_media_type(raw: str | None) -> str:
    return (raw or "application/octet-stream").split(",", 1)[0].split(";", 1)[0].strip().lower()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-validated address.

    ``connect`` dials the ALREADY-VALIDATED IP (closing the DNS-rebinding gap
    between validate-then-connect) while TLS keeps SNI and certificate
    verification against the ORIGINAL hostname, not the IP.
    """

    def __init__(
        self,
        address: str,
        host: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(address, port=port, timeout=timeout, context=context)
        self._pinned_address = address
        self._sni_host = host
        self._pinned_context = context

    def connect(self) -> None:  # pragma: no cover - exercised via local servers
        raw = socket.create_connection((self._pinned_address, self.port), timeout=self.timeout)
        self.sock = self._pinned_context.wrap_socket(raw, server_hostname=self._sni_host)


def _request_once(
    parsed: urllib.parse.ParseResult,
    address: str,
    policy: UrlFetchPolicy,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Issue ONE GET over a connection pinned to the validated address.

    Returns the connection alongside the response so the caller can keep
    shrinking the socket timeout against the per-hop deadline while reading
    the body (a trickling peer cannot outlast the deadline).
    """
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
            address,
            parsed.hostname or "",
            parsed.port or 443,
            policy.timeout_seconds,
            ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(
            address, port=parsed.port or 80, timeout=policy.timeout_seconds
        )
    target = urllib.parse.urlunparse(parsed._replace(scheme="", netloc="")) or "/"
    try:
        conn.request(
            "GET",
            target,
            headers={
                "Host": parsed.netloc,
                "User-Agent": "lumio-wiki-url-ingest/1.0",
                "Accept": "*/*",
            },
        )
        return conn, conn.getresponse()
    except Exception:
        conn.close()
        raise


def fetch_url(url: str, policy: UrlFetchPolicy | None = None) -> UrlFetchResult:
    """Fetch one URL under :class:`UrlFetchPolicy` and return truthful provenance.

    Every redirect hop re-validates scheme, credentials, and resolved
    addresses before connecting, and the connection is pinned to the
    validated address. The final result records the final URL, the exact
    bytes read (bounded), their SHA-256, retrieval time, declared media
    type, and a safe filename.
    """
    policy = policy or UrlFetchPolicy()
    current = url
    redirects = 0
    while True:
        parsed = _validate_scheme(current, allow_http=policy.allow_http)
        _validate_no_credentials(parsed)
        if not parsed.hostname:
            raise UrlFetchError("URL has no host")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            # ``parsed.port`` raises for malformed/out-of-range ports.
            raise UrlFetchError(f"invalid URL port: {exc}") from exc
        addresses = _resolve_addresses(parsed.hostname, port)
        if not addresses:
            raise UrlFetchError(f"host {parsed.hostname!r} resolved to no addresses")
        if not policy.allow_private_destinations:
            _reject_private_addresses(addresses)

        # Per-hop END-TO-END deadline: connect, headers, and body must all
        # complete within one timeout window, so a trickling peer cannot
        # outlast per-read timeouts (spec: "bounded bytes/time/redirects").
        deadline = time.monotonic() + policy.timeout_seconds
        conn: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            for address in addresses:
                try:
                    conn, response = _request_once(parsed, address, policy)
                    break
                except OSError:
                    conn = None
                    continue  # try the next resolved address
            if response is None or conn is None:
                raise UrlFetchError(f"failed connecting to {parsed.hostname!r}")
            status = response.status
            if 300 <= status < 400:
                location = response.headers.get("Location")
                if not location:
                    raise UrlFetchError(f"redirect status {status} without a Location header")
                if redirects >= policy.max_redirects:
                    raise UrlFetchError(f"exceeded {policy.max_redirects} redirects fetching {url}")
                current = urllib.parse.urljoin(current, location)
                redirects += 1
                continue
            if status < 200 or status >= 300:
                raise UrlFetchError(f"HTTP {status} fetching {current}")
            media_type = _normalize_media_type(response.headers.get("Content-Type"))
            base_media = media_type.split(";", 1)[0]
            allowed = {t.split(";", 1)[0] for t in policy.allowed_media_types}
            if base_media not in allowed:
                raise UrlFetchError(
                    f"media type {media_type!r} is not accepted for URL ingestion "
                    "(text, HTML, Markdown, PDF, XML, JSON)"
                )
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size: int | None = int(declared)
                except ValueError:
                    declared_size = None  # malformed: streaming bound still applies
                if declared_size is not None and declared_size > policy.max_bytes:
                    raise UrlFetchError(
                        f"response of {declared_size} bytes exceeds the "
                        f"{policy.max_bytes}-byte limit"
                    )
            chunks: list[bytes] = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UrlFetchError(
                        f"exceeded the {policy.timeout_seconds}s deadline fetching {current}"
                    )
                if conn.sock is not None:
                    # read1 performs at most ONE raw socket read, so bounding
                    # the socket timeout here bounds the whole body by the
                    # deadline — not by per-read timeouts.
                    conn.sock.settimeout(remaining)
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > policy.max_bytes:
                    raise UrlFetchError(f"response exceeds the {policy.max_bytes}-byte limit")
                chunks.append(chunk)
            body = b"".join(chunks)
            return UrlFetchResult(
                original_url=url,
                final_url=current,
                body=body,
                media_type=media_type,
                filename=_safe_filename(current, media_type),
                content_hash=hashlib.sha256(body).hexdigest(),
                retrieved_at=datetime.now(UTC).isoformat(),
                redirects=redirects,
            )
        except OSError as exc:
            # socket.timeout is a TimeoutError subclass; either way the hop
            # failed within the bounded window.
            raise UrlFetchError(f"failed fetching {current}: {exc}") from exc
        finally:
            if response is not None:
                response.close()
            if conn is not None:
                conn.close()
