"""Typed Plan C non-raw domain models, error taxonomy, and canonical byte extractors for GitHub."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from career_memory_api.auth.redaction import redact_text, redact_url_query
from career_memory_api.canonicalization import CANONICALIZATION_VERSION
from career_memory_api.schema_contracts.enums import (
    PrAssociationState,
    ResourceVisibility,
    SourceObjectType,
)
from career_memory_api.schema_contracts.metadata_guard import (
    validate_normalized_metadata,
    validate_sha256_hash,
)

GITHUB_CONNECTOR_VERSION = "cm-github-rest-v1"
GITHUB_METADATA_SCHEMA_VERSION = "cm-github-meta-v1"
DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"

DEFAULT_BACKFILL_DAYS = 365
DEFAULT_MAX_OBJECTS = 5000
MAX_OBJECTS_HARD_CAP = 5000
DEFAULT_MAX_REQUESTS = 500
DEFAULT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_ITEM_BYTES = 5 * 1024 * 1024
DEFAULT_PER_PAGE = 100

_SHA_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class GitHubStreamKind(StrEnum):
    """Logical GitHub read streams for single-repository initial backfill (Snapshot Import §2)."""

    REPOSITORY_METADATA = "REPOSITORY_METADATA"
    MERGED_PULL_REQUESTS = "MERGED_PULL_REQUESTS"
    PULL_REQUEST_REVIEWS = "PULL_REQUEST_REVIEWS"
    PULL_REQUEST_COMMENTS = "PULL_REQUEST_COMMENTS"
    DEFAULT_BRANCH_COMMITS = "DEFAULT_BRANCH_COMMITS"
    COMMIT_PR_ASSOCIATIONS = "COMMIT_PR_ASSOCIATIONS"
    REFERENCED_ISSUES = "REFERENCED_ISSUES"


REQUIRED_IMPORT_STREAMS: tuple[GitHubStreamKind, ...] = (
    GitHubStreamKind.REPOSITORY_METADATA,
    GitHubStreamKind.MERGED_PULL_REQUESTS,
    GitHubStreamKind.PULL_REQUEST_REVIEWS,
    GitHubStreamKind.PULL_REQUEST_COMMENTS,
    GitHubStreamKind.DEFAULT_BRANCH_COMMITS,
    GitHubStreamKind.COMMIT_PR_ASSOCIATIONS,
)

OPTIONAL_CONTEXT_STREAMS: tuple[GitHubStreamKind, ...] = (
    GitHubStreamKind.REFERENCED_ISSUES,
)


class GitHubErrorCode(StrEnum):
    """Deterministic classification of GitHub HTTP, policy, and transport failures."""

    AUTH_REVOKED = "AUTH_REVOKED"
    FORBIDDEN_POLICY = "FORBIDDEN_POLICY"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND_OR_INACCESSIBLE = "NOT_FOUND_OR_INACCESSIBLE"
    PARTIAL_RESPONSE = "PARTIAL_RESPONSE"
    BYTE_LIMIT_EXCEEDED = "BYTE_LIMIT_EXCEEDED"
    REDIRECT_BLOCKED = "REDIRECT_BLOCKED"
    ENDPOINT_BLOCKED = "ENDPOINT_BLOCKED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    MISSING_CREDENTIALS = "MISSING_CREDENTIALS"


class ImportPlanValidationError(ValueError):
    """Raised when an initial import plan request violates window, limit, or identity bounds."""


class RepositoryPolicyViolationError(ValueError):
    """Raised when a repository violates MVP public-only or policy constraints."""


class GitHubProviderError(Exception):
    """Safe provider error guaranteed never to retain raw response bodies or credentials."""

    def __init__(
        self,
        code: GitHubErrorCode,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        rate_limit_reset_at: datetime | None = None,
        endpoint_path: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        safe_message = redact_text(message)
        safe_path = redact_url_query(endpoint_path) if endpoint_path else None
        super().__init__(safe_message)
        self.code = code
        self.message = safe_message
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_reset_at = rate_limit_reset_at
        self.endpoint_path = safe_path
        self.reason_code = reason_code

    def __repr__(self) -> str:
        return (
            "GitHubProviderError("
            f"code={self.code.value!r}, "
            f"http_status={self.http_status!r}, "
            f"retryable={self.retryable!r}, "
            f"retry_after_seconds={self.retry_after_seconds!r}, "
            f"endpoint_path={self.endpoint_path!r}, "
            f"reason_code={self.reason_code!r})"
        )

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"


def format_utc_iso(value: datetime) -> str:
    """Format a datetime as canonical UTC ISO-8601 ending in 'Z'."""
    dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return dt.isoformat().replace("+00:00", "Z")


def parse_github_iso_timestamp(value: Any) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def validate_safe_path_segment(value: str, field_name: str) -> str:
    """Reject path traversal or unsafe URL path characters in provider identifiers."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    cleaned = value.strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
        or not _SAFE_SEGMENT_RE.match(cleaned)
    ):
        raise ValueError(f"Unsafe provider identifier segment for {field_name}.")
    return cleaned


def normalize_commit_sha(value: str, field_name: str = "commit_sha") -> str:
    """Validate and normalize a 40-character hexadecimal Git commit SHA."""
    if not isinstance(value, str) or not _SHA_HEX_RE.match(value.strip()):
        raise ValueError(f"{field_name} must be a 40-character hexadecimal commit SHA.")
    return value.strip().lower()


def classify_github_visibility(
    *,
    private: Any,
    visibility: Any = None,
) -> ResourceVisibility:
    """Classify GitHub repository visibility fail-closed (never PUBLIC if private=True)."""
    vis_text = str(visibility).strip().lower() if isinstance(visibility, str) else ""
    if vis_text == "internal":
        return ResourceVisibility.INTERNAL
    if private is True or vis_text == "private":
        return ResourceVisibility.PRIVATE
    if private is False and vis_text in ("", "public"):
        return ResourceVisibility.PUBLIC
    # Fail closed on ambiguous/unknown visibility metadata.
    return ResourceVisibility.PRIVATE


def extract_provider_visibility_from_payload(payload: Mapping[str, Any]) -> str | None:
    """Extract repository visibility from a GitHub REST JSON payload if present."""
    repo_obj: Any = None
    if isinstance(payload.get("base"), Mapping) and isinstance(payload["base"].get("repo"), Mapping):
        repo_obj = payload["base"]["repo"]
    elif isinstance(payload.get("repository"), Mapping):
        repo_obj = payload["repository"]
    elif "private" in payload or "visibility" in payload:
        repo_obj = payload

    if not isinstance(repo_obj, Mapping):
        return None
    if "private" not in repo_obj and "visibility" not in repo_obj:
        return None
    return classify_github_visibility(
        private=repo_obj.get("private"),
        visibility=repo_obj.get("visibility"),
    ).value


def extract_canonical_source_bytes(
    object_type: SourceObjectType | str,
    payload: Mapping[str, Any] | bytes,
    *,
    is_diff: bool = False,
) -> tuple[bytes, str]:
    """Deterministically extract canonicalizable UTF-8 source bytes and MIME type.

    Shared 1:1 between ``HttpGitHubImportClient`` (initial import hash calculation) and
    ``GitHubResolverContentFetcher`` (live refetch verification) so revision hashes match.
    """
    obj_type = (
        object_type.value
        if isinstance(object_type, SourceObjectType)
        else str(object_type).strip().upper()
    )

    if is_diff:
        if isinstance(payload, bytes):
            return payload, "text/x-diff"
        if isinstance(payload, Mapping):
            diff_val = payload.get("diff")
            if diff_val is None:
                diff_val = payload.get("patch")
            return str(diff_val or "").encode("utf-8"), "text/x-diff"
        raise TypeError("Diff payload must be bytes or a mapping.")

    if isinstance(payload, bytes):
        mime = "text/plain" if obj_type == SourceObjectType.COMMIT.value else "text/markdown"
        return payload, mime

    if not isinstance(payload, Mapping):
        raise TypeError("GitHub JSON payload must be a mapping.")

    if obj_type == SourceObjectType.PR.value:
        body = payload.get("body")
        return (body if isinstance(body, str) else "").encode("utf-8"), "text/markdown"

    if obj_type == SourceObjectType.ISSUE.value:
        body = payload.get("body")
        return (body if isinstance(body, str) else "").encode("utf-8"), "text/markdown"

    if obj_type == SourceObjectType.COMMIT.value:
        commit_obj = payload.get("commit")
        if isinstance(commit_obj, Mapping) and isinstance(commit_obj.get("message"), str):
            message = commit_obj["message"]
        elif isinstance(payload.get("message"), str):
            message = payload["message"]
        else:
            message = ""
        return message.encode("utf-8"), "text/plain"

    if obj_type in (SourceObjectType.COMMENT.value, SourceObjectType.REVIEW.value):
        body = payload.get("body")
        return (body if isinstance(body, str) else "").encode("utf-8"), "text/markdown"

    raise ValueError(f"Unsupported GitHub source object_type: {obj_type!r}")


@dataclass(frozen=True, slots=True)
class GitHubRepositorySnapshot:
    """Observed non-raw GitHub repository metadata and default branch HEAD snapshot."""

    provider_resource_id: str
    owner: str
    name: str
    full_name: str
    canonical_url: str
    visibility: ResourceVisibility
    default_branch: str
    default_branch_head_sha: str
    observed_at: datetime
    provider_updated_at: datetime | None = None
    is_archived: bool = False
    is_fork: bool = False

    def __post_init__(self) -> None:
        validate_safe_path_segment(self.provider_resource_id, "provider_resource_id")
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError("owner must be a non-empty string.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string.")
        if not isinstance(self.full_name, str) or not self.full_name.strip():
            raise ValueError("full_name must be a non-empty string.")
        if not isinstance(self.canonical_url, str) or not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must be an HTTPS URL.")
        if not isinstance(self.default_branch, str) or not self.default_branch.strip():
            raise ValueError("default_branch must be a non-empty string from provider metadata.")
        normalized_sha = normalize_commit_sha(
            self.default_branch_head_sha, "default_branch_head_sha"
        )
        object.__setattr__(self, "default_branch_head_sha", normalized_sha)
        obs = (
            self.observed_at.replace(tzinfo=UTC)
            if self.observed_at.tzinfo is None
            else self.observed_at.astimezone(UTC)
        )
        object.__setattr__(self, "observed_at", obs)
        if self.provider_updated_at is not None:
            upd = (
                self.provider_updated_at.replace(tzinfo=UTC)
                if self.provider_updated_at.tzinfo is None
                else self.provider_updated_at.astimezone(UTC)
            )
            object.__setattr__(self, "provider_updated_at", upd)

    def to_metadata_dict(self) -> dict[str, Any]:
        return {
            "provider_resource_id": self.provider_resource_id,
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "canonical_url": self.canonical_url,
            "visibility": self.visibility.value,
            "default_branch": self.default_branch,
            "default_branch_head_sha": self.default_branch_head_sha,
            "observed_at": format_utc_iso(self.observed_at),
            "provider_updated_at": (
                format_utc_iso(self.provider_updated_at)
                if self.provider_updated_at is not None
                else None
            ),
            "is_archived": self.is_archived,
            "is_fork": self.is_fork,
        }


@dataclass(frozen=True, slots=True)
class MergedPullRequestCue:
    """Deterministic non-raw fingerprint of a merged PR used by the 2nd-pass completion gate."""

    pr_number: int
    head_sha: str
    merged_at: datetime
    updated_at: datetime
    merge_commit_sha: str | None = None
    state: str = "closed"

    def __post_init__(self) -> None:
        if isinstance(self.pr_number, bool) or self.pr_number < 1:
            raise ValueError("pr_number must be an integer >= 1.")
        object.__setattr__(self, "head_sha", normalize_commit_sha(self.head_sha, "head_sha"))
        if self.merge_commit_sha is not None:
            object.__setattr__(
                self,
                "merge_commit_sha",
                normalize_commit_sha(self.merge_commit_sha, "merge_commit_sha"),
            )
        merged = (
            self.merged_at.replace(tzinfo=UTC)
            if self.merged_at.tzinfo is None
            else self.merged_at.astimezone(UTC)
        )
        updated = (
            self.updated_at.replace(tzinfo=UTC)
            if self.updated_at.tzinfo is None
            else self.updated_at.astimezone(UTC)
        )
        object.__setattr__(self, "merged_at", merged)
        object.__setattr__(self, "updated_at", updated)

    def comparison_tuple(self) -> tuple[int, str, str, str]:
        """Return canonical 4-tuple (pr_number, head_sha, merged_at, updated_at)."""
        return (
            self.pr_number,
            self.head_sha,
            format_utc_iso(self.merged_at),
            format_utc_iso(self.updated_at),
        )


@dataclass(frozen=True, slots=True)
class ObservedGitHubRevision:
    """Strictly Plan C non-raw revision metadata extracted from a GitHub REST object.

    Raw source text is hashed via ``canonicalize_source_bytes`` in memory and immediately
    discarded; ``normalized_metadata`` is strictly validated against
    ``ALLOWED_NORMALIZED_METADATA_KEYS`` (zero raw text keys, zero raw text bytes).
    """

    source_object_type: SourceObjectType
    stream_kind: GitHubStreamKind
    provider_resource_id: str
    provider_object_id: str
    provider_revision_id: str | None
    content_hash: str
    hash_policy_version: str
    observed_at: datetime
    provider_updated_at: datetime | None
    normalized_metadata: dict[str, Any]
    connector_version: str = GITHUB_CONNECTOR_VERSION
    schema_version: str = GITHUB_METADATA_SCHEMA_VERSION
    parent_shas: tuple[str, ...] = ()
    associated_pr_numbers: tuple[int, ...] = ()
    pr_association_state: PrAssociationState | None = None
    referenced_issue_numbers: tuple[int, ...] = ()
    parent_pr_number: int | None = None
    canonicalization_status: str = "CANONICALIZED"
    skip_reason_code: str | None = None

    def __post_init__(self) -> None:
        validate_safe_path_segment(self.provider_resource_id, "provider_resource_id")
        validate_safe_path_segment(self.provider_object_id, "provider_object_id")
        validate_sha256_hash(self.content_hash)
        validated = validate_normalized_metadata(self.normalized_metadata, strict_whitelist=True)
        object.__setattr__(self, "normalized_metadata", validated)
        obs = (
            self.observed_at.replace(tzinfo=UTC)
            if self.observed_at.tzinfo is None
            else self.observed_at.astimezone(UTC)
        )
        object.__setattr__(self, "observed_at", obs)
        if self.provider_updated_at is not None:
            upd = (
                self.provider_updated_at.replace(tzinfo=UTC)
                if self.provider_updated_at.tzinfo is None
                else self.provider_updated_at.astimezone(UTC)
            )
            object.__setattr__(self, "provider_updated_at", upd)

    def to_metadata_record_dict(self) -> dict[str, Any]:
        """Serialize strictly non-raw provenance fields safe for JSONB / audit / logging."""
        return {
            "source_object_type": self.source_object_type.value,
            "stream_kind": self.stream_kind.value,
            "provider_resource_id": self.provider_resource_id,
            "provider_object_id": self.provider_object_id,
            "provider_revision_id": self.provider_revision_id,
            "content_hash": self.content_hash,
            "hash_policy_version": self.hash_policy_version,
            "observed_at": format_utc_iso(self.observed_at),
            "provider_updated_at": (
                format_utc_iso(self.provider_updated_at)
                if self.provider_updated_at is not None
                else None
            ),
            "normalized_metadata": dict(self.normalized_metadata),
            "connector_version": self.connector_version,
            "schema_version": self.schema_version,
            "parent_shas": list(self.parent_shas),
            "associated_pr_numbers": list(self.associated_pr_numbers),
            "pr_association_state": (
                self.pr_association_state.value if self.pr_association_state is not None else None
            ),
            "referenced_issue_numbers": list(self.referenced_issue_numbers),
            "parent_pr_number": self.parent_pr_number,
            "canonicalization_status": self.canonicalization_status,
            "skip_reason_code": self.skip_reason_code,
        }


@dataclass(frozen=True, slots=True)
class ObservedRevisionPage:
    """Single paginated page of non-raw ObservedGitHubRevision records."""

    stream_kind: GitHubStreamKind
    items: tuple[ObservedGitHubRevision, ...]
    page: int
    has_next: bool
    next_page: int | None
    next_url: str | None = None
    requests_used: int = 1
    bytes_read: int = 0
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class MergedPullRequestPage:
    """Single paginated page of in-window merged PR revisions and completion-gate cues."""

    items: tuple[ObservedGitHubRevision, ...]
    cues: tuple[MergedPullRequestCue, ...]
    page: int
    has_next: bool
    next_page: int | None
    next_url: str | None = None
    reached_window_floor: bool = False
    requests_used: int = 1
    bytes_read: int = 0
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class CommitPullRequestAssociation:
    """Result of querying GitHub /commits/{sha}/pulls for provider PR association."""

    commit_sha: str
    associated_pr_numbers: tuple[int, ...]
    pr_association_state: PrAssociationState
    observed_at: datetime
    requests_used: int = 1
    bytes_read: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "commit_sha", normalize_commit_sha(self.commit_sha, "commit_sha"))


def safe_json_dumps(data: Any) -> str:
    """Canonical compact JSON serialization helper."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
