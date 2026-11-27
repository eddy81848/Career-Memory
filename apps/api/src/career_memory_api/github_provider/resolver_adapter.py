"""T-103B ProviderContentFetcherPort implementation backed by SafeGitHubHttpTransport (M2-T202)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from career_memory_api.auth.redaction import RedactingLogFilter
from career_memory_api.github_provider.models import (
    GitHubErrorCode,
    GitHubProviderError,
    extract_canonical_source_bytes,
    extract_provider_visibility_from_payload,
)
from career_memory_api.github_provider.transport import (
    SafeGitHubHttpTransport,
)
from career_memory_api.resolver.models import (
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    ProviderFetchResponse,
)

logger = logging.getLogger(__name__)
if not any(isinstance(f, RedactingLogFilter) for f in logger.filters):
    logger.addFilter(RedactingLogFilter())

_REDIRECT_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


class GitHubResolverContentFetcher:
    """Real GitHub implementation of M1 ``ProviderContentFetcherPort``.

    Guarantees:
    - Fetches strictly ``request.endpoint_url`` (server-owned URL constructed by
      ``RefetchResolverService`` from persisted GitHub provider IDs).
    - NEVER follows redirects automatically; returns ``ProviderFetchOutcomeKind.REDIRECT``
      with ``redirect_location`` so ``RefetchResolverService`` validates every hop.
    - Extracts canonicalizable source bytes in memory with the exact same extractor used
      during initial import, and NEVER caches, persists, or logs raw bytes or tokens.
    """

    def __init__(
        self,
        *,
        transport: SafeGitHubHttpTransport,
        require_credentials: bool = False,
    ) -> None:
        self._transport = transport
        self._require_credentials = require_credentials

    def __repr__(self) -> str:
        return (
            "GitHubResolverContentFetcher("
            f"require_credentials={self._require_credentials!r})"
        )

    def fetch(self, request: ProviderFetchRequest) -> ProviderFetchResponse:
        """Fetch raw source content ephemerally into memory for live hash/span verification."""
        if request.provider.strip().upper() != "GITHUB":
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.POLICY_DENIED,
                reason_code="UNSUPPORTED_PROVIDER",
                retryable=False,
            )

        endpoint_check = self._transport.security_policy.validate_endpoint_url(request.endpoint_url)
        if not endpoint_check.allowed or endpoint_check.normalized_url is None:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.POLICY_DENIED,
                reason_code=endpoint_check.reason_code or "ENDPOINT_POLICY_BLOCKED",
                retryable=False,
            )

        accept = (
            "application/vnd.github.v3.diff"
            if request.is_diff
            else "application/vnd.github+json"
        )

        try:
            wire_resp = self._transport.get(
                endpoint_check.normalized_url,
                token_secret_ref=request.token_secret_ref,
                accept=accept,
                timeout_seconds=request.timeout_seconds,
                max_bytes=request.max_bytes,
                follow_safe_redirects=False,
                require_credentials=self._require_credentials,
            )
        except GitHubProviderError as exc:
            return self._map_provider_error(exc)

        if wire_resp.status_code in _REDIRECT_STATUS_CODES:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.REDIRECT,
                http_status=wire_resp.status_code,
                bytes_read=len(wire_resp.body),
                redirect_location=wire_resp.get_header("Location"),
                retryable=False,
            )

        content_type = (wire_resp.get_header("Content-Type") or "").lower()
        provider_visibility: str | None = None

        if request.is_diff or "diff" in content_type or (
            "json" not in content_type and content_type.startswith("text/")
        ):
            raw_bytes, mime_type = extract_canonical_source_bytes(
                request.object_type,
                wire_resp.body,
                is_diff=request.is_diff,
            )
        else:
            try:
                decoded = wire_resp.body.decode("utf-8", errors="strict")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ProviderFetchResponse(
                    kind=ProviderFetchOutcomeKind.SERVER_ERROR,
                    http_status=wire_resp.status_code,
                    bytes_read=len(wire_resp.body),
                    reason_code="MALFORMED_JSON_RESPONSE",
                    retryable=False,
                )
            if not isinstance(parsed, Mapping):
                return ProviderFetchResponse(
                    kind=ProviderFetchOutcomeKind.SERVER_ERROR,
                    http_status=wire_resp.status_code,
                    bytes_read=len(wire_resp.body),
                    reason_code="UNEXPECTED_JSON_SHAPE",
                    retryable=False,
                )
            provider_visibility = extract_provider_visibility_from_payload(parsed)
            try:
                raw_bytes, mime_type = extract_canonical_source_bytes(
                    request.object_type,
                    parsed,
                    is_diff=False,
                )
            except (ValueError, TypeError):
                return ProviderFetchResponse(
                    kind=ProviderFetchOutcomeKind.POLICY_DENIED,
                    http_status=wire_resp.status_code,
                    bytes_read=len(wire_resp.body),
                    reason_code="UNSUPPORTED_OBJECT_TYPE",
                    retryable=False,
                )

        if len(raw_bytes) > request.max_bytes:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.LIMIT_EXCEEDED,
                http_status=wire_resp.status_code,
                bytes_read=len(wire_resp.body),
                reason_code="BYTE_LIMIT_EXCEEDED",
                retryable=False,
                truncated=True,
            )

        return ProviderFetchResponse(
            kind=ProviderFetchOutcomeKind.OK,
            http_status=wire_resp.status_code,
            raw_bytes=raw_bytes,
            bytes_read=len(wire_resp.body),
            mime_type=mime_type,
            provider_visibility=provider_visibility,
            retryable=False,
            truncated=False,
        )

    @staticmethod
    def _map_provider_error(exc: GitHubProviderError) -> ProviderFetchResponse:
        code = exc.code
        if code is GitHubErrorCode.NOT_FOUND_OR_INACCESSIBLE:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.NOT_FOUND,
                http_status=exc.http_status,
                reason_code=exc.reason_code or "PROVIDER_OBJECT_NOT_FOUND",
                retryable=False,
            )
        if code is GitHubErrorCode.RATE_LIMITED:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.RATE_LIMITED,
                http_status=exc.http_status,
                reason_code=exc.reason_code or "PROVIDER_RATE_LIMITED",
                retryable=True,
            )
        if code is GitHubErrorCode.PARTIAL_RESPONSE:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.PARTIAL_CONTENT,
                http_status=exc.http_status,
                reason_code=exc.reason_code or "PARTIAL_RESPONSE_TRUNCATED",
                retryable=False,
                truncated=True,
            )
        if code is GitHubErrorCode.BYTE_LIMIT_EXCEEDED:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.LIMIT_EXCEEDED,
                http_status=exc.http_status,
                reason_code=exc.reason_code or "BYTE_LIMIT_EXCEEDED",
                retryable=False,
                truncated=True,
            )
        if code in (
            GitHubErrorCode.AUTH_REVOKED,
            GitHubErrorCode.FORBIDDEN_POLICY,
            GitHubErrorCode.ENDPOINT_BLOCKED,
            GitHubErrorCode.REDIRECT_BLOCKED,
            GitHubErrorCode.MISSING_CREDENTIALS,
        ):
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.POLICY_DENIED,
                http_status=exc.http_status,
                reason_code=exc.reason_code or f"PROVIDER_{code.value}",
                retryable=False,
            )
        if code is GitHubErrorCode.TIMEOUT:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.TIMEOUT,
                http_status=exc.http_status,
                reason_code=exc.reason_code or "PROVIDER_TIMEOUT",
                retryable=True,
            )
        if code is GitHubErrorCode.CONNECTION_ERROR:
            return ProviderFetchResponse(
                kind=ProviderFetchOutcomeKind.CONNECTION_ERROR,
                http_status=exc.http_status,
                reason_code=exc.reason_code or "PROVIDER_CONNECTION_ERROR",
                retryable=True,
            )
        return ProviderFetchResponse(
            kind=ProviderFetchOutcomeKind.SERVER_ERROR,
            http_status=exc.http_status,
            reason_code=exc.reason_code or "PROVIDER_SERVER_ERROR",
            retryable=exc.retryable,
        )
