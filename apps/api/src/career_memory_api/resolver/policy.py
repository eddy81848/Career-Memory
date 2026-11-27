"""SSRF, redirect, visibility/deletion fence, and operation budget policies (REF-05, REF-08)."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from career_memory_api.resolver.models import PersistedUnitResolutionTarget

_SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_METADATA_V4_NETWORK = ipaddress.ip_network("169.254.0.0/16")

DEFAULT_ALLOWED_PROVIDER_HOSTS: frozenset[str] = frozenset(
    {
        "api.github.com",
        "raw.githubusercontent.com",
    }
)


@dataclass(frozen=True, slots=True)
class OperationBudgetConfig:
    """Operation-scoped hard resource limits for refetch resolution (REF-08)."""

    max_requests: int = 20
    max_total_bytes: int = 5 * 1024 * 1024
    max_unit_bytes: int = 1 * 1024 * 1024
    max_retries: int = 3
    max_concurrency: int = 4
    timeout_seconds: float = 10.0
    max_redirects: int = 3
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_requests < 0:
            raise ValueError("max_requests must be >= 0.")
        if self.max_total_bytes < 0 or self.max_unit_bytes < 0:
            raise ValueError("byte limits must be >= 0.")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0.")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0.")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be >= 0.")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1 when specified.")


@dataclass(frozen=True, slots=True)
class EndpointValidationResult:
    """Result of validating a server-constructed endpoint or redirect target URL."""

    allowed: bool
    normalized_url: str | None = None
    reason_code: str | None = None


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for loopback, private, link-local, metadata, CGNAT, or reserved IPs."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        if _is_forbidden_ip(ip_obj.ipv4_mapped):
            return True

    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return True

    if isinstance(ip_obj, ipaddress.IPv4Address):
        if ip_obj in _CGNAT_NETWORK or ip_obj in _METADATA_V4_NETWORK:
            return True

    return False


def _try_parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    stripped = host.strip("[]")
    try:
        return ipaddress.ip_address(stripped)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class EndpointSecurityPolicy:
    """Strict HTTPS, host allowlist, credential, IP-range, DNS, and redirect guard (REF-05)."""

    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_PROVIDER_HOSTS
    allow_redirects: bool = True
    max_redirects: int = 3
    dns_resolver: Callable[[str], Sequence[str]] | None = None

    def validate_endpoint_url(self, url: str) -> EndpointValidationResult:
        """Validate a server-owned provider endpoint URL before any network call is attempted."""
        if not isinstance(url, str) or not url.strip():
            return EndpointValidationResult(allowed=False, reason_code="INVALID_URL")

        raw = url.strip()
        if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in raw):
            return EndpointValidationResult(allowed=False, reason_code="INVALID_URL_CHARACTERS")

        try:
            parts = urlsplit(raw)
        except ValueError:
            return EndpointValidationResult(allowed=False, reason_code="INVALID_URL")

        if parts.scheme.lower() != "https":
            return EndpointValidationResult(allowed=False, reason_code="NON_HTTPS_SCHEME")

        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return EndpointValidationResult(allowed=False, reason_code="URL_CREDENTIALS_FORBIDDEN")

        try:
            port = parts.port
        except ValueError:
            return EndpointValidationResult(allowed=False, reason_code="INVALID_PORT")

        if port is not None and port != 443:
            return EndpointValidationResult(allowed=False, reason_code="NON_STANDARD_PORT_FORBIDDEN")

        hostname = (parts.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            return EndpointValidationResult(allowed=False, reason_code="MISSING_HOST")

        ip_literal = _try_parse_ip(hostname)
        if ip_literal is not None:
            if _is_forbidden_ip(ip_literal):
                return EndpointValidationResult(allowed=False, reason_code="FORBIDDEN_IP_ADDRESS")
            return EndpointValidationResult(allowed=False, reason_code="IP_LITERAL_FORBIDDEN")

        normalized_allowlist = {h.strip().lower().rstrip(".") for h in self.allowed_hosts}
        if hostname not in normalized_allowlist:
            return EndpointValidationResult(allowed=False, reason_code="HOST_NOT_ALLOWLISTED")

        if self.dns_resolver is not None:
            try:
                resolved_ips = tuple(self.dns_resolver(hostname))
            except Exception:  # noqa: BLE001
                return EndpointValidationResult(allowed=False, reason_code="DNS_RESOLUTION_FAILED")
            if not resolved_ips:
                return EndpointValidationResult(allowed=False, reason_code="DNS_RESOLUTION_EMPTY")
            for ip_text in resolved_ips:
                ip_obj = _try_parse_ip(str(ip_text))
                if ip_obj is None or _is_forbidden_ip(ip_obj):
                    return EndpointValidationResult(allowed=False, reason_code="RESOLVED_IP_FORBIDDEN")

        return EndpointValidationResult(allowed=True, normalized_url=raw, reason_code=None)

    def validate_redirect_target(
        self, current_url: str, location: str | None
    ) -> EndpointValidationResult:
        """Validate a 30x redirect target against the same strict SSRF/allowlist policy."""
        if not self.allow_redirects:
            return EndpointValidationResult(allowed=False, reason_code="REDIRECT_BLOCKED")
        if not isinstance(location, str) or not location.strip():
            return EndpointValidationResult(allowed=False, reason_code="REDIRECT_BLOCKED")

        candidate = urljoin(current_url, location.strip())
        checked = self.validate_endpoint_url(candidate)
        if not checked.allowed:
            return EndpointValidationResult(allowed=False, reason_code="REDIRECT_BLOCKED")
        return checked


def _validate_provider_segment(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
        or not _SAFE_PATH_SEGMENT_RE.match(cleaned)
    ):
        raise ValueError(f"Unsafe provider identifier segment for {field_name}.")
    return cleaned


def build_provider_endpoint_url(target: PersistedUnitResolutionTarget) -> str:
    """Construct a server-owned HTTPS provider endpoint strictly from persisted provider IDs.

    Never accepts or trusts user-supplied or LLM-supplied URLs.
    """
    provider = target.source_account_provider.strip().upper()
    if provider != "GITHUB":
        raise ValueError(f"Unsupported provider for refetch endpoint construction: {provider!r}")

    repo_id = _validate_provider_segment(target.provider_resource_id, "provider_resource_id")
    obj_id = _validate_provider_segment(target.provider_object_id, "provider_object_id")
    obj_type = target.object_type.strip().upper()

    if obj_type == "PR":
        return f"https://api.github.com/repositories/{repo_id}/pulls/{obj_id}"
    if obj_type == "ISSUE":
        return f"https://api.github.com/repositories/{repo_id}/issues/{obj_id}"
    if obj_type == "COMMIT":
        return f"https://api.github.com/repositories/{repo_id}/commits/{obj_id}"
    if obj_type == "COMMENT":
        return f"https://api.github.com/repositories/{repo_id}/issues/comments/{obj_id}"
    if obj_type == "REVIEW":
        return f"https://api.github.com/repositories/{repo_id}/pulls/comments/{obj_id}"

    raise ValueError(f"Unsupported source object_type for endpoint construction: {obj_type!r}")


def evaluate_target_policy(target: PersistedUnitResolutionTarget) -> tuple[bool, str | None]:
    """Evaluate workspace/account/resource/deletion/Option-C policy BEFORE any remote fetch."""
    if (
        target.deletion_fence_active
        or target.resource_status in ("DELETED", "DELETION_PENDING")
        or target.object_lifecycle_state == "DELETED"
    ):
        return False, "RESOURCE_DELETED_OR_FENCED"

    if target.unit_policy_state != "ACTIVE":
        return False, "UNIT_POLICY_BLOCKED"

    if (
        target.raw_storage_mode != "NONE"
        or target.retention_class != "NONE"
        or target.text_content is not None
        or target.content_ref is not None
    ):
        return False, "RAW_STORAGE_POLICY_VIOLATION"

    if target.source_account_status != "ACTIVE":
        return False, "SOURCE_ACCOUNT_REVOKED"

    if (
        target.resource_status == "POLICY_REVIEW_REQUIRED"
        or target.object_lifecycle_state == "POLICY_REVIEW_REQUIRED"
    ):
        return False, "POLICY_REVIEW_REQUIRED"

    if target.resource_status != "ACTIVE":
        return False, "RESOURCE_NOT_ACTIVE"

    if target.visibility != "PUBLIC":
        return False, "RESOURCE_NOT_PUBLIC"

    if target.revision_is_tombstone:
        return False, "REVISION_TOMBSTONED"

    return True, None
