"""Pydantic wire models, domain errors, and pure GitHub identity classification helpers (T-401, T-401A)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from career_memory_api.schema_contracts.enums import (
    ExternalIdentityMappingStatus,
    IdentityStatus,
    IdentityType,
    IdentityVerificationMethod,
    SourceProvider,
)

IDENTITIES_NO_STORE_HEADER = "private, no-store"

_GITHUB_NUMERIC_NOREPLY_RE = re.compile(
    r"^(?P<uid>[0-9]+)\+(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)@users\.noreply\.github\.com$",
    flags=re.IGNORECASE,
)
_GITHUB_LOGIN_NOREPLY_RE = re.compile(
    r"^(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)@users\.noreply\.github\.com$",
    flags=re.IGNORECASE,
)
_CO_AUTHOR_LINE_RE = re.compile(
    r"^\s*Co-authored-by:\s*(?P<name>[^<\r\n]+?)\s*<(?P<email>[^>\r\n]+)>\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)

_KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "dependabot",
        "dependabot[bot]",
        "dependabot-preview",
        "dependabot-preview[bot]",
        "renovate",
        "renovate[bot]",
        "github-actions",
        "github-actions[bot]",
        "codecov",
        "codecov[bot]",
        "snyk-bot",
        "mergify",
        "mergify[bot]",
        "allcontributors[bot]",
        "greenkeeper[bot]",
        "pre-commit-ci[bot]",
    }
)


class GitHubActorRole(StrEnum):
    """Distinct GitHub actor roles separated under ID-03 / T-401."""

    PR_AUTHOR = "PR_AUTHOR"
    COMMIT_AUTHOR = "COMMIT_AUTHOR"
    COMMITTER = "COMMITTER"
    CO_AUTHOR = "CO_AUTHOR"
    REVIEWER = "REVIEWER"
    BOT = "BOT"


class IdentityAttributionBlockReason(StrEnum):
    """Stable block reason codes returned by evaluate_identity_attribution (ID-04 / ID-05)."""

    IDENTITY_HEURISTIC_ONLY = "IDENTITY_HEURISTIC_ONLY"
    IDENTITY_CONFLICTED = "IDENTITY_CONFLICTED"
    IDENTITY_REVOKED = "IDENTITY_REVOKED"
    IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"
    IDENTITY_IS_BOT = "IDENTITY_IS_BOT"
    IDENTITY_NOT_FOUND = "IDENTITY_NOT_FOUND"


class IdentityError(Exception):
    """Structured HTTP/domain error raised by identity mapping operations."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


@dataclass(frozen=True, slots=True)
class GitHubNoreplyInfo:
    """Parsed metadata from a GitHub noreply address (never retains raw email)."""

    numeric_user_id: str | None
    login: str | None
    has_numeric_id: bool
    is_web_flow: bool = False


@dataclass(frozen=True, slots=True)
class CoAuthorTrailer:
    """Parsed Co-authored-by trailer storing only sha256 email_hash and noreply hints."""

    display_name: str
    email_hash: str
    noreply_numeric_user_id: str | None = None
    noreply_login: str | None = None


@dataclass(frozen=True, slots=True)
class ActorContributionEligibility:
    """Role-level personal contribution eligibility (ID-03 / ID-04)."""

    actor_role: GitHubActorRole
    identity_type: IdentityType
    eligible_for_implemented: bool
    eligible_for_reviewed: bool
    can_auto_verify_role: bool
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubActorObservation:
    """Transient GitHub actor observation passed into identity resolution.

    ``email`` is strictly transient input: it is normalized and hashed to
    ``sha256:<hex>`` before any database or audit/outbox persistence.
    """

    actor_role: GitHubActorRole
    external_user_id: str | None = None
    username: str | None = None
    display_name: str | None = None
    email: str | None = None
    email_hash: str | None = None
    provider_actor_type: str | None = None
    provider_associated: bool = False
    is_oauth_account: bool = False
    canonical_user_id: str | None = None
    source_object_id: str | None = None
    source_resource_id: str | None = None
    target_identity_id: str | None = None


def hash_email(email: str) -> str:
    """Normalize an email (trim + lowercase) and return ``sha256:<64-hex>``."""
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("Email must not be blank.")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_github_noreply_email(email: str | None) -> GitHubNoreplyInfo | None:
    """Parse a GitHub noreply email without retaining plaintext email."""
    if not email:
        return None
    cleaned = email.strip()
    if not cleaned:
        return None
    if cleaned.lower() == "noreply@github.com":
        return GitHubNoreplyInfo(
            numeric_user_id=None,
            login=None,
            has_numeric_id=False,
            is_web_flow=True,
        )
    num_match = _GITHUB_NUMERIC_NOREPLY_RE.match(cleaned)
    if num_match:
        return GitHubNoreplyInfo(
            numeric_user_id=num_match.group("uid"),
            login=num_match.group("login"),
            has_numeric_id=True,
            is_web_flow=False,
        )
    login_match = _GITHUB_LOGIN_NOREPLY_RE.match(cleaned)
    if login_match:
        return GitHubNoreplyInfo(
            numeric_user_id=None,
            login=login_match.group("login"),
            has_numeric_id=False,
            is_web_flow=False,
        )
    return None


def is_bot_actor(
    *,
    username: str | None = None,
    display_name: str | None = None,
    provider_actor_type: str | None = None,
    email: str | None = None,
    actor_role: GitHubActorRole | None = None,
) -> bool:
    """Return True when an actor is a GitHub Bot account (ID-03)."""
    if actor_role is GitHubActorRole.BOT:
        return True
    if provider_actor_type and provider_actor_type.strip().lower() == "bot":
        return True
    if username:
        uname = username.strip().lower()
        if uname.endswith("[bot]") or uname in _KNOWN_BOT_LOGINS:
            return True
    if display_name:
        dname = display_name.strip().lower()
        if dname.endswith("[bot]") or dname in _KNOWN_BOT_LOGINS:
            return True
    if email:
        noreply = parse_github_noreply_email(email)
        if noreply and noreply.login:
            login_l = noreply.login.lower()
            if login_l.endswith("[bot]") or login_l in _KNOWN_BOT_LOGINS:
                return True
    return False


def parse_co_author_trailers(commit_message: str) -> list[CoAuthorTrailer]:
    """Extract Co-authored-by trailers and hash raw emails immediately (Plan C)."""
    results: list[CoAuthorTrailer] = []
    seen_hashes: set[str] = set()
    for match in _CO_AUTHOR_LINE_RE.finditer(commit_message or ""):
        name = match.group("name").strip()
        raw_email = match.group("email").strip()
        if not name or not raw_email:
            continue
        digest = hash_email(raw_email)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        noreply = parse_github_noreply_email(raw_email)
        results.append(
            CoAuthorTrailer(
                display_name=name,
                email_hash=digest,
                noreply_numeric_user_id=noreply.numeric_user_id if noreply else None,
                noreply_login=noreply.login if noreply else None,
            )
        )
    return results


def classify_actor_contribution_eligibility(
    *,
    actor_role: GitHubActorRole,
    identity_type: IdentityType,
) -> ActorContributionEligibility:
    """Enforce ID-03 / ID-04 separation of PR author, commit author, committer, co-author, reviewer, and bot."""
    if identity_type is IdentityType.BOT or actor_role is GitHubActorRole.BOT:
        return ActorContributionEligibility(
            actor_role=actor_role,
            identity_type=identity_type,
            eligible_for_implemented=False,
            eligible_for_reviewed=False,
            can_auto_verify_role=False,
            reason_code=IdentityAttributionBlockReason.IDENTITY_IS_BOT.value,
        )
    if identity_type is not IdentityType.PERSON:
        return ActorContributionEligibility(
            actor_role=actor_role,
            identity_type=identity_type,
            eligible_for_implemented=False,
            eligible_for_reviewed=False,
            can_auto_verify_role=False,
            reason_code=IdentityAttributionBlockReason.IDENTITY_AMBIGUOUS.value,
        )
    if actor_role in (GitHubActorRole.PR_AUTHOR, GitHubActorRole.COMMIT_AUTHOR):
        return ActorContributionEligibility(
            actor_role=actor_role,
            identity_type=identity_type,
            eligible_for_implemented=True,
            eligible_for_reviewed=False,
            can_auto_verify_role=True,
        )
    if actor_role is GitHubActorRole.REVIEWER:
        return ActorContributionEligibility(
            actor_role=actor_role,
            identity_type=identity_type,
            eligible_for_implemented=False,
            eligible_for_reviewed=True,
            can_auto_verify_role=True,
        )
    if actor_role is GitHubActorRole.COMMITTER:
        return ActorContributionEligibility(
            actor_role=actor_role,
            identity_type=identity_type,
            eligible_for_implemented=False,
            eligible_for_reviewed=False,
            can_auto_verify_role=False,
            reason_code="COMMITTER_NOT_PERSONAL_AUTHOR",
        )
    # CO_AUTHOR trailer: never VERIFIED IMPLEMENTED proof on its own
    return ActorContributionEligibility(
        actor_role=actor_role,
        identity_type=identity_type,
        eligible_for_implemented=False,
        eligible_for_reviewed=False,
        can_auto_verify_role=False,
        reason_code="CO_AUTHOR_REQUIRES_EXPLICIT_CONFIRMATION",
    )


class IdentityAttributionEligibility(BaseModel):
    """Computed downstream attribution eligibility for an Identity (ID-01..ID-05)."""

    model_config = ConfigDict(extra="forbid")

    identity_id: str | None = None
    identity_type: IdentityType | None = None
    identity_status: IdentityStatus | None = None
    can_support_source_verified: bool
    can_support_user_confirmed: bool
    block_reason_code: IdentityAttributionBlockReason | None = None
    active_verified_mapping_count: int = Field(default=0, ge=0)
    active_user_confirmed_mapping_count: int = Field(default=0, ge=0)
    active_heuristic_mapping_count: int = Field(default=0, ge=0)
    active_conflicted_mapping_count: int = Field(default=0, ge=0)
    revoked_mapping_count: int = Field(default=0, ge=0)


class ExternalIdentityMappingRecord(BaseModel):
    """Wire projection of ``public.external_identity_mappings`` (email_hash only, never raw email)."""

    model_config = ConfigDict(extra="forbid")

    mapping_id: str
    identity_id: str
    provider: SourceProvider
    external_user_id: str | None = None
    email_hash: str | None = None
    username: str | None = None
    verification_method: IdentityVerificationMethod
    status: ExternalIdentityMappingStatus
    confidence_reason: str | None = None
    verified_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class IdentityWithMappingsRecord(BaseModel):
    """Workspace identity with redacted external mappings and attribution eligibility."""

    model_config = ConfigDict(extra="forbid")

    identity_id: str
    workspace_id: str
    display_name: str
    identity_type: IdentityType
    canonical_user_id: str | None = None
    status: IdentityStatus
    created_at: datetime
    updated_at: datetime
    mappings: list[ExternalIdentityMappingRecord] = Field(default_factory=list)
    attribution_eligibility: IdentityAttributionEligibility


class IdentityListResponse(BaseModel):
    """Response payload for ``GET /v1/identities``."""

    model_config = ConfigDict(extra="forbid")

    items: list[IdentityWithMappingsRecord] = Field(default_factory=list)


class IdentityInvalidationCounts(BaseModel):
    """Atomic downstream invalidation counts produced by revocation or conflict (ID-05)."""

    model_config = ConfigDict(extra="forbid")

    contributions: int = Field(default=0, ge=0)
    facts: int = Field(default=0, ge=0)
    claim_atoms: int = Field(default=0, ge=0)
    claims: int = Field(default=0, ge=0)
    exports: int = Field(default=0, ge=0)


class ConfirmIdentityMappingRequest(BaseModel):
    """Request body for ``POST /v1/identities/mappings/{mapping_id}/actions/confirm``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ConfirmIdentityMappingResponse(BaseModel):
    """Response body for ``POST /v1/identities/mappings/{mapping_id}/actions/confirm``."""

    model_config = ConfigDict(extra="forbid")

    identity_id: str
    mapping: ExternalIdentityMappingRecord
    attribution_eligibility: IdentityAttributionEligibility
    idempotent_replay: bool = False


class RevokeIdentityMappingRequest(BaseModel):
    """Request body for ``POST /v1/identities/mappings/{mapping_id}/actions/revoke``."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reason must not be blank.")
        return cleaned


class RevokeIdentityMappingResponse(BaseModel):
    """Response body for ``POST /v1/identities/mappings/{mapping_id}/actions/revoke``."""

    model_config = ConfigDict(extra="forbid")

    identity_id: str
    mapping: ExternalIdentityMappingRecord
    attribution_eligibility: IdentityAttributionEligibility
    invalidated_counts: IdentityInvalidationCounts
    idempotent_replay: bool = False


class ResolveGitHubIdentityResult(BaseModel):
    """Result of resolving/upserting an observed GitHub actor into workspace identity tables."""

    model_config = ConfigDict(extra="forbid")

    identity_id: str
    identity_type: IdentityType
    identity_status: IdentityStatus
    mapping: ExternalIdentityMappingRecord
    attribution_eligibility: IdentityAttributionEligibility
    created_identity: bool = False
    renamed_username: bool = False
    previous_username: str | None = None
    conflict_detected: bool = False


__all__ = [
    "IDENTITIES_NO_STORE_HEADER",
    "ActorContributionEligibility",
    "CoAuthorTrailer",
    "ConfirmIdentityMappingRequest",
    "ConfirmIdentityMappingResponse",
    "ExternalIdentityMappingRecord",
    "GitHubActorObservation",
    "GitHubActorRole",
    "GitHubNoreplyInfo",
    "IdentityAttributionBlockReason",
    "IdentityAttributionEligibility",
    "IdentityError",
    "IdentityInvalidationCounts",
    "IdentityListResponse",
    "IdentityWithMappingsRecord",
    "ResolveGitHubIdentityResult",
    "RevokeIdentityMappingRequest",
    "RevokeIdentityMappingResponse",
    "classify_actor_contribution_eligibility",
    "hash_email",
    "is_bot_actor",
    "parse_co_author_trailers",
    "parse_github_noreply_email",
]
