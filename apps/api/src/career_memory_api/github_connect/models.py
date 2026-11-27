"""Domain models, scope validation, policy enums, and error contracts for M2-T201."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from career_memory_api.auth.redaction import redact_text

GITHUB_CONNECT_NO_STORE_HEADER = "private, no-store"
REQUIRED_GITHUB_OAUTH_SCOPE = "read:user"
ALLOWED_GITHUB_OAUTH_SCOPES: frozenset[str] = frozenset({REQUIRED_GITHUB_OAUTH_SCOPE})

_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Strict key denylist matching DB CHECK constraints in 0020 and 0040 so
# audit_events.redacted_metadata never fails constraints or leaks secrets/raw text.
_FORBIDDEN_AUDIT_KEYS: frozenset[str] = frozenset(
    {
        # Secret keys (0040 cm_jsonb_contains_forbidden_secret_key + OAuth params)
        "access_token",
        "refresh_token",
        "bearer_token",
        "authorization",
        "client_secret",
        "password",
        "api_key",
        "private_key",
        "pkce_code_verifier",
        "code_verifier",
        "oauth_token",
        "github_token",
        "secret_value",
        "session_token",
        "cookie",
        "set_cookie",
        "set-cookie",
        "state",
        "code",
        "token",
        "secret",
        "id_token",
        # Raw-text Plan C keys (0020/0040 cm_jsonb_contains_forbidden_raw_key)
        "body",
        "raw_body",
        "raw_text",
        "text_content",
        "diff",
        "patch",
        "comment_body",
        "message_body",
        "quote",
        "extracted_span",
        "prompt",
        "raw_payload",
        "raw_content",
        "full_text",
        "source_text",
        "raw_diff",
        "commit_message_body",
        "prompt_text",
        "system_prompt",
        "user_prompt",
        "completion",
        "raw_response",
        "content_text",
        "excerpt",
        "snippet",
        "verbatim_quote",
        "pr_body",
        "issue_body",
        "review_body",
        "patch_text",
        "diff_text",
        "hunk_text",
    }
)


class GitHubCredentialMode(StrEnum):
    """Credential architecture behind GitHubCredentialProviderPort (OD-011)."""

    OAUTH_APP = "OAUTH_APP"
    GITHUB_APP = "GITHUB_APP"


class GitHubInspectionOutcome(StrEnum):
    """Normalized outcome of inspecting a GitHub repository with the active credential."""

    PUBLIC_CONFIRMED = "PUBLIC_CONFIRMED"
    NON_PUBLIC_CONFIRMED = "NON_PUBLIC_CONFIRMED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    SCOPE_LOST = "SCOPE_LOST"
    EXCESSIVE_SCOPE_DETECTED = "EXCESSIVE_SCOPE_DETECTED"
    AMBIGUOUS_ACCESS_OR_VISIBILITY = "AMBIGUOUS_ACCESS_OR_VISIBILITY"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class PolicyDecision(StrEnum):
    """Fail-closed processing decision for a SourceResource."""

    ALLOWED = "ALLOWED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


class PolicyGuidanceCode(StrEnum):
    """Client/operator guidance codes for permission & visibility transitions (T-201A)."""

    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    POLICY_REVIEW_REQUIRED = "POLICY_REVIEW_REQUIRED"
    VISIBILITY_OR_ACCESS_AMBIGUOUS = "VISIBILITY_OR_ACCESS_AMBIGUOUS"


class PolicyTransitionKind(StrEnum):
    """Auditable policy verification transition classification."""

    UNCHANGED_PUBLIC = "UNCHANGED_PUBLIC"
    ALREADY_POLICY_REVIEW_REQUIRED = "ALREADY_POLICY_REVIEW_REQUIRED"
    PUBLIC_TO_NON_PUBLIC = "PUBLIC_TO_NON_PUBLIC"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    SCOPE_LOST = "SCOPE_LOST"
    EXCESSIVE_SCOPE_DETECTED = "EXCESSIVE_SCOPE_DETECTED"
    VISIBILITY_OR_ACCESS_AMBIGUOUS = "VISIBILITY_OR_ACCESS_AMBIGUOUS"


class PolicyGuardedOperation(StrEnum):
    """Downstream operations immediately blocked when a resource enters POLICY_BLOCKED."""

    INGESTION = "INGESTION"
    RAW_REFETCH = "RAW_REFETCH"
    AI_TRANSMISSION = "AI_TRANSMISSION"
    EXPORT = "EXPORT"


ALL_BLOCKED_OPERATIONS: tuple[str, ...] = tuple(op.value for op in PolicyGuardedOperation)


class GitHubConnectError(Exception):
    """Structured, redacted domain/HTTP error for the GitHub connect boundary."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_message = redact_text(message)
        super().__init__(safe_message)
        self.status_code = status_code
        self.code = code
        self.message = safe_message
        self.retryable = retryable
        self.details: dict[str, Any] | None = (
            sanitize_audit_metadata(details) if details is not None else None
        )


def parse_oauth_scopes(raw_scopes: str | tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Parse GitHub comma/whitespace-separated OAuth scope list into sorted unique tokens."""
    if raw_scopes is None:
        return ()
    tokens: list[str] = []
    if isinstance(raw_scopes, str):
        parts = re.split(r"[\s,]+", raw_scopes.strip())
        tokens.extend(p.strip().lower() for p in parts if p.strip())
    else:
        for item in raw_scopes:
            if not isinstance(item, str):
                continue
            parts = re.split(r"[\s,]+", item.strip())
            tokens.extend(p.strip().lower() for p in parts if p.strip())
    return tuple(sorted(set(tokens)))


@dataclass(frozen=True, slots=True)
class GitHubScopeValidationResult:
    """Result of validating granted GitHub OAuth scopes against OD-011 minimum policy."""

    is_valid: bool
    granted_scopes: tuple[str, ...]
    disallowed_scopes: tuple[str, ...]
    missing_required_scopes: tuple[str, ...]


def validate_minimum_github_scopes(
    raw_scopes: str | tuple[str, ...] | list[str] | None,
) -> GitHubScopeValidationResult:
    """Enforce OD-011 minimum scope invariant: ``read:user`` ONLY (0 repo/write scopes)."""
    granted = parse_oauth_scopes(raw_scopes)
    granted_set = frozenset(granted)
    disallowed = tuple(sorted(granted_set - ALLOWED_GITHUB_OAUTH_SCOPES))
    missing = tuple(sorted(ALLOWED_GITHUB_OAUTH_SCOPES - granted_set))
    return GitHubScopeValidationResult(
        is_valid=(len(disallowed) == 0 and len(missing) == 0),
        granted_scopes=granted,
        disallowed_scopes=disallowed,
        missing_required_scopes=missing,
    )


def parse_repository_full_name(repository_full_name: str) -> tuple[str, str]:
    """Validate and split a single explicit GitHub ``owner/repo`` identifier.

    Rejects wildcards, URLs, query strings, path traversal, or extra slashes so
    selection remains strictly a single explicit repository allowlist entry.
    """
    if not isinstance(repository_full_name, str):
        raise GitHubConnectError(
            status_code=400,
            code="INVALID_REPOSITORY_NAME",
            message="Repository name must be a valid 'owner/repo' string.",
        )
    candidate = repository_full_name.strip()
    if (
        not candidate
        or "/" not in candidate
        or "://" in candidate
        or "?" in candidate
        or "#" in candidate
        or "*" in candidate
        or ".." in candidate
    ):
        raise GitHubConnectError(
            status_code=400,
            code="INVALID_REPOSITORY_NAME",
            message="Repository name must match 'owner/repo' without URLs or wildcards.",
        )
    parts = candidate.split("/")
    if len(parts) != 2:
        raise GitHubConnectError(
            status_code=400,
            code="INVALID_REPOSITORY_NAME",
            message="Repository name must contain exactly one '/' separating owner and repo.",
        )
    owner, repo = parts[0].strip(), parts[1].strip()
    if (
        not _REPO_SEGMENT_RE.match(owner)
        or not _REPO_SEGMENT_RE.match(repo)
        or owner in {".", ".."}
        or repo in {".", ".."}
    ):
        raise GitHubConnectError(
            status_code=400,
            code="INVALID_REPOSITORY_NAME",
            message="Repository owner or name contains invalid characters.",
        )
    return owner, repo


def _sanitize_audit_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return sanitize_audit_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_audit_value(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def sanitize_audit_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively strip forbidden secret/raw-text keys and redact string values."""
    sanitized: dict[str, Any] = {}
    for raw_key, raw_val in metadata.items():
        key = str(raw_key)
        if key.strip().lower() in _FORBIDDEN_AUDIT_KEYS:
            continue
        sanitized[key] = _sanitize_audit_value(raw_val)
    return sanitized


@dataclass(frozen=True, slots=True)
class GitHubAccountIdentity:
    """Verified GitHub user identity resolved from the OAuth token."""

    external_account_id: str
    login: str
    header_scopes: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class GitHubExchangedCredential:
    """Exchanged GitHub credential with ``access_token`` strictly hidden from repr/str."""

    external_account_id: str
    login: str
    access_token: str = field(repr=False)
    scopes: tuple[str, ...] = ()
    token_type: str = "bearer"

    def __repr__(self) -> str:
        return (
            f"GitHubExchangedCredential(external_account_id={self.external_account_id!r}, "
            f"login={self.login!r}, access_token=<redacted>, scopes={self.scopes!r})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class GitHubRepositoryMetadata:
    """Normalized GitHub repository metadata returned by provider inspection."""

    provider_resource_id: str
    full_name: str
    canonical_url: str
    private: bool
    visibility: str
    default_branch: str | None = None

    @property
    def is_strictly_public(self) -> bool:
        return (not self.private) and self.visibility.strip().lower() == "public"


@dataclass(frozen=True, slots=True)
class GitHubRepositoryInspection:
    """Normalized outcome of checking repository visibility and credential validity."""

    outcome: GitHubInspectionOutcome
    http_status: int
    observed_visibility: str | None = None
    repository: GitHubRepositoryMetadata | None = None
    observed_scopes: tuple[str, ...] | None = None
    retry_after_seconds: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class GitHubRevokeResult:
    """Outcome of revoking an OAuth grant at GitHub."""

    revoked: bool
    already_revoked: bool
    http_status: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubOAuthStartResult:
    """Response payload for starting the GitHub OAuth App authorization flow."""

    provider: str
    credential_mode: GitHubCredentialMode
    authorization_url: str
    requested_scopes: tuple[str, ...]
    state: str = field(repr=False)
    pkce_code_challenge: str
    return_path: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"GitHubOAuthStartResult(provider={self.provider!r}, "
            f"credential_mode={self.credential_mode!r}, state=<redacted>, "
            f"requested_scopes={self.requested_scopes!r}, return_path={self.return_path!r}, "
            f"expires_at={self.expires_at!r})"
        )

    def to_http_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "credential_mode": self.credential_mode.value,
            "authorization_url": self.authorization_url,
            "requested_scopes": list(self.requested_scopes),
            "state": self.state,
            "pkce_code_challenge": self.pkce_code_challenge,
            "return_path": self.return_path,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SourceAccountView:
    """Safe API view of ``public.source_accounts`` (never exposes ``token_secret_ref``)."""

    id: str
    workspace_id: str
    provider: str
    external_account_id: str
    scopes: tuple[str, ...]
    status: str
    created_at: datetime
    revoked_at: datetime | None = None

    def to_http_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "external_account_id": self.external_account_id,
            "scopes": list(self.scopes),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at is not None else None,
        }


@dataclass(frozen=True, slots=True)
class SourceResourceRecord:
    """Workspace-scoped ``public.source_resources`` record aligned with migration 0020."""

    id: str
    workspace_id: str
    source_account_id: str
    provider_resource_id: str
    resource_type: str
    name: str
    canonical_url: str
    visibility: str
    processing_mode: str
    raw_storage_mode: str
    raw_retention_seconds: int | None
    status: str
    policy_version: str
    consent_version: str | None
    last_visibility_checked_at: datetime | None
    created_at: datetime
    guidance_code: PolicyGuidanceCode | None = None

    def to_http_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "source_account_id": self.source_account_id,
            "provider_resource_id": self.provider_resource_id,
            "resource_type": self.resource_type,
            "name": self.name,
            "canonical_url": self.canonical_url,
            "visibility": self.visibility,
            "processing_mode": self.processing_mode,
            "raw_storage_mode": self.raw_storage_mode,
            "raw_retention_seconds": self.raw_retention_seconds,
            "status": self.status,
            "policy_version": self.policy_version,
            "consent_version": self.consent_version,
            "last_visibility_checked_at": (
                self.last_visibility_checked_at.isoformat()
                if self.last_visibility_checked_at is not None
                else None
            ),
            "created_at": self.created_at.isoformat(),
            "guidance_code": self.guidance_code.value if self.guidance_code is not None else None,
        }


@dataclass(frozen=True, slots=True)
class GitHubOAuthCallbackResult:
    """Result returned by ``complete_oauth_callback``."""

    source_account: SourceAccountView
    source_resource: SourceResourceRecord | None
    return_path: str
    idempotent_replay: bool = False

    def to_http_dict(self) -> dict[str, Any]:
        return {
            "source_account": self.source_account.to_http_dict(),
            "source_resource": (
                self.source_resource.to_http_dict()
                if self.source_resource is not None
                else None
            ),
            "return_path": self.return_path,
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True, slots=True)
class PolicyVerificationResult:
    """Result returned by ``verify_resource_policy`` (T-201A / POLICY-01)."""

    resource: SourceResourceRecord
    account_status: str
    policy_decision: PolicyDecision
    guidance_code: PolicyGuidanceCode | None
    transition: PolicyTransitionKind
    tombstone_created: bool
    canceled_job_ids: tuple[str, ...]
    canceled_sync_run_ids: tuple[str, ...]
    blocked_operations: tuple[str, ...]
    checked_at: datetime

    def to_http_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource.to_http_dict(),
            "account_status": self.account_status,
            "policy_decision": self.policy_decision.value,
            "guidance_code": self.guidance_code.value if self.guidance_code is not None else None,
            "transition": self.transition.value,
            "tombstone_created": self.tombstone_created,
            "canceled_job_ids": list(self.canceled_job_ids),
            "canceled_sync_run_ids": list(self.canceled_sync_run_ids),
            "blocked_operations": list(self.blocked_operations),
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SourceAccountDisconnectResult:
    """Result returned by ``disconnect_source_account``."""

    source_account: SourceAccountView
    provider_grant_revoked: bool
    secret_deleted: bool
    already_revoked: bool
    affected_resource_ids: tuple[str, ...]
    canceled_job_ids: tuple[str, ...]
    canceled_sync_run_ids: tuple[str, ...]
    tombstone_created: bool = False

    def to_http_dict(self) -> dict[str, Any]:
        return {
            "source_account": self.source_account.to_http_dict(),
            "provider_grant_revoked": self.provider_grant_revoked,
            "secret_deleted": self.secret_deleted,
            "already_revoked": self.already_revoked,
            "affected_resource_ids": list(self.affected_resource_ids),
            "canceled_job_ids": list(self.canceled_job_ids),
            "canceled_sync_run_ids": list(self.canceled_sync_run_ids),
            "tombstone_created": self.tombstone_created,
        }


@dataclass(frozen=True, slots=True)
class SourceResourcePolicyClearance:
    """Verified processing clearance returned by ``assert_resource_processing_allowed``."""

    resource: SourceResourceRecord
    source_account_id: str
    policy_decision: PolicyDecision
    operation: PolicyGuardedOperation


@dataclass(frozen=True, slots=True)
class ResolvedGitHubCredential:
    """Internal server-only credential bundle for downstream M2 ingestion/refetch."""

    source_account_id: str
    source_resource: SourceResourceRecord
    access_token: str = field(repr=False)
    scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"ResolvedGitHubCredential(source_account_id={self.source_account_id!r}, "
            f"source_resource_id={self.source_resource.id!r}, "
            f"access_token=<redacted>, scopes={self.scopes!r})"
        )

    def __str__(self) -> str:
        return repr(self)
