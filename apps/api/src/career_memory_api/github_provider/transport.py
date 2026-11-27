"""Injectable read-only GitHub HTTP wire transport, SSRF/redirect guard, and error classification."""

from __future__ import annotations

import json
import logging
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from career_memory_api.auth.oauth import SecretStorePort
from career_memory_api.auth.redaction import RedactingLogFilter, redact_headers, redact_url_query
from career_memory_api.github_provider.models import (
    DEFAULT_GITHUB_API_VERSION,
    DEFAULT_MAX_ITEM_BYTES,
    GITHUB_CONNECTOR_VERSION,
    GitHubErrorCode,
    GitHubProviderError,
)
from career_memory_api.resolver.policy import EndpointSecurityPolicy
from career_memory_api.schema_contracts.metadata_guard import validate_token_secret_ref

logger = logging.getLogger(__name__)
if not any(isinstance(f, RedactingLogFilter) for f in logger.filters):
    logger.addFilter(RedactingLogFilter())

GITHUB_API_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.github.com"})
_REDIRECT_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_LINK_REL_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')
_CHUNK_SIZE = 65536


@dataclass(frozen=True, slots=True)
class WireHttpRequest:
    """Strictly read-only (GET) HTTP wire request with credential-redacted repr/str."""

    method: str
    url: str
    headers: Mapping[str, str]
    timeout_seconds: float
    max_bytes: int

    def __post_init__(self) -> None:
        if self.method.strip().upper() != "GET":
            raise ValueError(
                f"GitHub read-only adapter strictly permits GET requests (got {self.method!r})."
            )
        object.__setattr__(self, "method", "GET")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0.")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be >= 1.")

    def __repr__(self) -> str:
        safe_url = redact_url_query(self.url)
        safe_headers = redact_headers(self.headers)
        return (
            "WireHttpRequest("
            f"method={self.method!r}, "
            f"url={safe_url!r}, "
            f"headers={safe_headers!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_bytes={self.max_bytes!r})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class WireHttpResponse:
    """HTTP wire response whose raw body bytes are strictly redacted from repr/str."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes = field(default=b"", repr=False)
    truncated: bool = False
    content_length_mismatch: bool = False

    def get_header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None

    def __repr__(self) -> str:
        safe_headers = redact_headers(self.headers)
        return (
            "WireHttpResponse("
            f"status_code={self.status_code!r}, "
            f"headers={safe_headers!r}, "
            f"body=<REDACTED_WIRE_BYTES len={len(self.body)}>, "
            f"truncated={self.truncated!r}, "
            f"content_length_mismatch={self.content_length_mismatch!r})"
        )

    def __str__(self) -> str:
        return repr(self)


class GitHubWireTransportPort(Protocol):
    """Injectable low-level HTTP wire port (MUST NOT auto-follow redirects)."""

    def send(self, request: WireHttpRequest) -> WireHttpResponse:
        ...


class InMemoryWireTransport:
    """Deterministic offline ``GitHubWireTransportPort`` for unit & protocol tests."""

    def __init__(self, handler: Callable[[WireHttpRequest], WireHttpResponse]) -> None:
        self._handler = handler
        self.requests: list[WireHttpRequest] = []

    def send(self, request: WireHttpRequest) -> WireHttpResponse:
        self.requests.append(request)
        return self._handler(request)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from automatically following 30x redirects so SSRF policy checks every hop."""

    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _read_bounded_stream(stream: Any, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining_plus_one = (max_bytes - total) + 1
        chunk = stream.read(min(_CHUNK_SIZE, remaining_plus_one))
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > max_bytes:
            return b"", True
        chunks.append(chunk)


def _check_content_length_mismatch(headers: Mapping[str, str], body_len: int) -> bool:
    for key, val in headers.items():
        if key.lower() == "content-length":
            try:
                expected = int(val.strip())
            except ValueError:
                return True
            return expected != body_len
    return False


class UrllibGitHubWireTransport:
    """Stdlib ``urllib.request`` wire transport with no-redirect and bounded streaming."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def send(self, request: WireHttpRequest) -> WireHttpResponse:
        urllib_req = urllib.request.Request(
            url=request.url,
            headers=dict(request.headers),
            method="GET",
        )
        try:
            with self._opener.open(urllib_req, timeout=request.timeout_seconds) as resp:
                status = int(getattr(resp, "status", resp.getcode()))
                headers = {str(k): str(v) for k, v in resp.headers.items()}
                body, truncated = _read_bounded_stream(resp, request.max_bytes)
                mismatch = (not truncated) and _check_content_length_mismatch(headers, len(body))
                return WireHttpResponse(
                    status_code=status,
                    headers=headers,
                    body=body,
                    truncated=truncated,
                    content_length_mismatch=mismatch,
                )
        except urllib.error.HTTPError as err:
            status = int(err.code)
            headers = {str(k): str(v) for k, v in err.headers.items()} if err.headers else {}
            try:
                body, truncated = _read_bounded_stream(err, request.max_bytes)
            except Exception:  # noqa: BLE001
                body, truncated = b"", False
            finally:
                try:
                    err.close()
                except Exception:  # noqa: BLE001
                    pass
            return WireHttpResponse(
                status_code=status,
                headers=headers,
                body=body,
                truncated=truncated,
                content_length_mismatch=False,
            )
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("GitHub wire request timed out.") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("GitHub wire request timed out.") from exc
            raise ConnectionError("GitHub wire connection failed.") from exc


class Httpx2GitHubWireTransport:
    """Adapter bridging any ``httpx2.Client`` (including ``httpx2.MockTransport``) to the wire port."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def send(self, request: WireHttpRequest) -> WireHttpResponse:
        try:
            with self._client.stream(
                "GET",
                request.url,
                headers=dict(request.headers),
                timeout=request.timeout_seconds,
                follow_redirects=False,
            ) as resp:
                status = int(resp.status_code)
                headers = {str(k): str(v) for k, v in resp.headers.items()}
                chunks: list[bytes] = []
                total = 0
                truncated = False
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > request.max_bytes:
                        truncated = True
                        chunks.clear()
                        break
                    chunks.append(chunk)
                body = b"" if truncated else b"".join(chunks)
                mismatch = (
                    (not truncated)
                    and status == 200
                    and _check_content_length_mismatch(headers, len(body))
                )
                return WireHttpResponse(
                    status_code=status,
                    headers=headers,
                    body=body,
                    truncated=truncated,
                    content_length_mismatch=mismatch,
                )
        except Exception as exc:  # noqa: BLE001
            cls_name = type(exc).__name__.lower()
            if isinstance(exc, (TimeoutError, socket.timeout)) or "timeout" in cls_name:
                raise TimeoutError("GitHub httpx2 wire request timed out.") from exc
            raise ConnectionError("GitHub httpx2 wire connection failed.") from exc


def parse_retry_after_seconds(value: str | None, *, now: datetime) -> int | None:
    """Parse HTTP Retry-After header (delta-seconds or HTTP-date) into non-negative seconds."""
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned.isdigit():
        return max(0, int(cleaned))
    try:
        target = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError):
        return None
    target_utc = target.replace(tzinfo=UTC) if target.tzinfo is None else target.astimezone(UTC)
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return max(0, int((target_utc - now_utc).total_seconds()))


def parse_rate_limit_reset_at(value: str | None) -> datetime | None:
    """Parse GitHub X-RateLimit-Reset epoch seconds header into a UTC datetime."""
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    try:
        epoch = int(cleaned)
    except ValueError:
        return None
    if epoch < 0:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def parse_github_next_link(
    link_header: str | None,
    *,
    security_policy: EndpointSecurityPolicy,
) -> str | None:
    """Extract and SSRF-validate the ``rel="next"`` URL from a GitHub Link header."""
    if not link_header or not link_header.strip():
        return None
    for candidate_url, rel in _LINK_REL_RE.findall(link_header):
        if rel.strip().lower() == "next":
            check = security_policy.validate_endpoint_url(candidate_url.strip())
            if not check.allowed or check.normalized_url is None:
                raise GitHubProviderError(
                    GitHubErrorCode.ENDPOINT_BLOCKED,
                    "Pagination Link rel='next' target failed endpoint security policy.",
                    retryable=False,
                    reason_code=check.reason_code or "ENDPOINT_POLICY_BLOCKED",
                )
            return check.normalized_url
    return None


def _safe_endpoint_path(url: str) -> str:
    parts = urlsplit(url)
    return redact_url_query(parts.path or "/")


class SafeGitHubHttpTransport:
    """SSRF-guarded, token-redacting, error-classifying GitHub REST v3 HTTP transport.

    Never persists, caches, or logs raw response bodies or Authorization credentials.
    """

    def __init__(
        self,
        *,
        wire_transport: GitHubWireTransportPort | None = None,
        secret_store: SecretStorePort | None = None,
        token_resolver: Callable[[str], str | None] | None = None,
        security_policy: EndpointSecurityPolicy | None = None,
        default_timeout_seconds: float = 10.0,
        default_max_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        user_agent: str = GITHUB_CONNECTOR_VERSION,
        api_version: str = DEFAULT_GITHUB_API_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._wire = wire_transport or UrllibGitHubWireTransport()
        self._secret_store = secret_store
        self._token_resolver = token_resolver
        self._security_policy = security_policy or EndpointSecurityPolicy(
            allowed_hosts=GITHUB_API_ALLOWED_HOSTS,
            allow_redirects=True,
            max_redirects=3,
        )
        self._default_timeout_seconds = default_timeout_seconds
        self._default_max_bytes = default_max_bytes
        self._user_agent = user_agent
        self._api_version = api_version
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def security_policy(self) -> EndpointSecurityPolicy:
        return self._security_policy

    def __repr__(self) -> str:
        return (
            "SafeGitHubHttpTransport("
            f"allowed_hosts={sorted(self._security_policy.allowed_hosts)!r}, "
            f"default_timeout_seconds={self._default_timeout_seconds!r}, "
            f"default_max_bytes={self._default_max_bytes!r})"
        )

    def _resolve_bearer_token(
        self,
        token_secret_ref: str | None,
        *,
        require_credentials: bool = False,
    ) -> str | None:
        if token_secret_ref is None:
            if require_credentials:
                raise GitHubProviderError(
                    GitHubErrorCode.MISSING_CREDENTIALS,
                    "Provider token_secret_ref is required.",
                    retryable=False,
                    reason_code="MISSING_PROVIDER_CREDENTIALS",
                )
            return None

        try:
            validated_ref = validate_token_secret_ref(token_secret_ref)
        except ValueError as exc:
            raise GitHubProviderError(
                GitHubErrorCode.MISSING_CREDENTIALS,
                "Invalid token_secret_ref format.",
                retryable=False,
                reason_code="INVALID_TOKEN_SECRET_REF",
            ) from exc

        token: str | None = None
        if self._token_resolver is not None:
            token = self._token_resolver(validated_ref)
        elif self._secret_store is not None:
            token = self._secret_store.get_secret(validated_ref)

        if token is None:
            if require_credentials:
                raise GitHubProviderError(
                    GitHubErrorCode.MISSING_CREDENTIALS,
                    "Provider credentials could not be resolved from secret store.",
                    retryable=False,
                    reason_code="MISSING_PROVIDER_CREDENTIALS",
                )
            return None

        cleaned = token.strip()
        if not cleaned:
            if require_credentials:
                raise GitHubProviderError(
                    GitHubErrorCode.MISSING_CREDENTIALS,
                    "Resolved provider credential is empty.",
                    retryable=False,
                    reason_code="MISSING_PROVIDER_CREDENTIALS",
                )
            return None
        return cleaned

    def get(
        self,
        url: str,
        *,
        token_secret_ref: str | None = None,
        accept: str = "application/vnd.github+json",
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
        follow_safe_redirects: bool = True,
        require_credentials: bool = False,
    ) -> WireHttpResponse:
        """Perform a strictly read-only GET with SSRF, redirect, byte-cap, and error checks."""
        initial_check = self._security_policy.validate_endpoint_url(url)
        if not initial_check.allowed or initial_check.normalized_url is None:
            raise GitHubProviderError(
                GitHubErrorCode.ENDPOINT_BLOCKED,
                "GitHub request URL blocked by EndpointSecurityPolicy.",
                retryable=False,
                endpoint_path=_safe_endpoint_path(url),
                reason_code=initial_check.reason_code or "ENDPOINT_POLICY_BLOCKED",
            )

        token = self._resolve_bearer_token(
            token_secret_ref, require_credentials=require_credentials
        )
        eff_timeout = (
            timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        )
        eff_max_bytes = max_bytes if max_bytes is not None else self._default_max_bytes

        current_url = initial_check.normalized_url
        initial_host = (urlsplit(current_url).hostname or "").lower()
        redirects_used = 0

        while True:
            current_host = (urlsplit(current_url).hostname or "").lower()
            headers: dict[str, str] = {
                "Accept": accept,
                "User-Agent": self._user_agent,
                "X-GitHub-Api-Version": self._api_version,
            }
            if token is not None and current_host == initial_host:
                headers["Authorization"] = f"Bearer {token}"

            wire_req = WireHttpRequest(
                method="GET",
                url=current_url,
                headers=headers,
                timeout_seconds=eff_timeout,
                max_bytes=eff_max_bytes,
            )
            safe_path = _safe_endpoint_path(current_url)

            try:
                response = self._wire.send(wire_req)
            except (TimeoutError, socket.timeout) as exc:
                logger.debug("github_http_timeout path=%s", safe_path)
                raise GitHubProviderError(
                    GitHubErrorCode.TIMEOUT,
                    "GitHub API request timed out.",
                    retryable=True,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_TIMEOUT",
                ) from exc
            except Exception as exc:  # noqa: BLE001
                logger.debug("github_http_connection_error path=%s", safe_path)
                raise GitHubProviderError(
                    GitHubErrorCode.CONNECTION_ERROR,
                    "GitHub API connection error.",
                    retryable=True,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_CONNECTION_ERROR",
                ) from exc

            status = response.status_code
            logger.debug(
                "github_http_response path=%s status=%d bytes=%d truncated=%s",
                safe_path,
                status,
                len(response.body),
                response.truncated,
            )

            # 1. Redirects (301/302/303/307/308)
            if status in _REDIRECT_STATUS_CODES:
                if not follow_safe_redirects:
                    return response
                if redirects_used >= self._security_policy.max_redirects:
                    raise GitHubProviderError(
                        GitHubErrorCode.REDIRECT_BLOCKED,
                        "GitHub API redirect hop limit exceeded.",
                        http_status=status,
                        retryable=False,
                        endpoint_path=safe_path,
                        reason_code="REDIRECT_LIMIT_EXCEEDED",
                    )
                location = response.get_header("Location")
                redir_check = self._security_policy.validate_redirect_target(current_url, location)
                if not redir_check.allowed or redir_check.normalized_url is None:
                    raise GitHubProviderError(
                        GitHubErrorCode.REDIRECT_BLOCKED,
                        "GitHub API redirect target blocked by EndpointSecurityPolicy.",
                        http_status=status,
                        retryable=False,
                        endpoint_path=safe_path,
                        reason_code=redir_check.reason_code or "REDIRECT_BLOCKED",
                    )
                current_url = redir_check.normalized_url
                redirects_used += 1
                continue

            # 2. Byte limit overflow / partial or truncated responses
            if status == 206:
                raise GitHubProviderError(
                    GitHubErrorCode.PARTIAL_RESPONSE,
                    "GitHub API returned 206 Partial Content; truncated responses are rejected.",
                    http_status=206,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="PARTIAL_RESPONSE_TRUNCATED",
                )

            if response.truncated or len(response.body) > eff_max_bytes:
                raise GitHubProviderError(
                    GitHubErrorCode.BYTE_LIMIT_EXCEEDED,
                    "GitHub API response exceeded configured max_bytes limit.",
                    http_status=status,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="BYTE_LIMIT_EXCEEDED",
                )

            if response.content_length_mismatch or (
                status == 200 and _check_content_length_mismatch(response.headers, len(response.body))
            ):
                raise GitHubProviderError(
                    GitHubErrorCode.PARTIAL_RESPONSE,
                    "GitHub API response Content-Length mismatch detected (truncated body).",
                    http_status=status,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="PARTIAL_RESPONSE_TRUNCATED",
                )

            # 3. HTTP status classification (never include raw body text in error message!)
            now = self._clock()
            if status == 429:
                retry_after = parse_retry_after_seconds(response.get_header("Retry-After"), now=now)
                reset_at = parse_rate_limit_reset_at(response.get_header("X-RateLimit-Reset"))
                raise GitHubProviderError(
                    GitHubErrorCode.RATE_LIMITED,
                    "GitHub API rate limit exceeded (HTTP 429).",
                    http_status=429,
                    retryable=True,
                    retry_after_seconds=retry_after,
                    rate_limit_reset_at=reset_at,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_RATE_LIMITED",
                )

            if status == 401:
                raise GitHubProviderError(
                    GitHubErrorCode.AUTH_REVOKED,
                    "GitHub API rejected credentials (HTTP 401).",
                    http_status=401,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_AUTH_REVOKED",
                )

            if status == 403:
                remaining = (response.get_header("X-RateLimit-Remaining") or "").strip()
                retry_after_hdr = response.get_header("Retry-After")
                if remaining == "0" or (retry_after_hdr is not None and retry_after_hdr.strip() != ""):
                    retry_after = parse_retry_after_seconds(retry_after_hdr, now=now)
                    reset_at = parse_rate_limit_reset_at(response.get_header("X-RateLimit-Reset"))
                    raise GitHubProviderError(
                        GitHubErrorCode.RATE_LIMITED,
                        "GitHub API rate limit exhausted (HTTP 403).",
                        http_status=403,
                        retryable=True,
                        retry_after_seconds=retry_after,
                        rate_limit_reset_at=reset_at,
                        endpoint_path=safe_path,
                        reason_code="PROVIDER_RATE_LIMITED",
                    )
                raise GitHubProviderError(
                    GitHubErrorCode.FORBIDDEN_POLICY,
                    "GitHub API denied access due to permissions or policy (HTTP 403).",
                    http_status=403,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_ACCESS_DENIED",
                )

            if status in (404, 410):
                raise GitHubProviderError(
                    GitHubErrorCode.NOT_FOUND_OR_INACCESSIBLE,
                    "GitHub API object not found or inaccessible (ambiguous; not concluded as deleted).",
                    http_status=status,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_OBJECT_NOT_FOUND",
                )

            if 500 <= status <= 599:
                raise GitHubProviderError(
                    GitHubErrorCode.SERVER_ERROR,
                    f"GitHub API returned transient server error (HTTP {status}).",
                    http_status=status,
                    retryable=True,
                    endpoint_path=safe_path,
                    reason_code="PROVIDER_SERVER_ERROR",
                )

            if status != 200:
                raise GitHubProviderError(
                    GitHubErrorCode.INVALID_RESPONSE,
                    f"GitHub API returned unexpected HTTP status {status}.",
                    http_status=status,
                    retryable=False,
                    endpoint_path=safe_path,
                    reason_code="UNEXPECTED_HTTP_STATUS",
                )

            return response

    def get_json(
        self,
        url: str,
        *,
        token_secret_ref: str | None = None,
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
        require_credentials: bool = False,
    ) -> tuple[Any, WireHttpResponse]:
        """Execute a safe read-only GET and parse JSON without ever leaking raw body on errors."""
        response = self.get(
            url,
            token_secret_ref=token_secret_ref,
            accept="application/vnd.github+json",
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            follow_safe_redirects=True,
            require_credentials=require_credentials,
        )
        safe_path = _safe_endpoint_path(url)
        try:
            decoded = response.body.decode("utf-8", errors="strict")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubProviderError(
                GitHubErrorCode.INVALID_RESPONSE,
                "GitHub API returned malformed UTF-8 or JSON payload.",
                http_status=response.status_code,
                retryable=False,
                endpoint_path=safe_path,
                reason_code="MALFORMED_JSON_RESPONSE",
            ) from exc
        return parsed, response
