"""Domain enum contracts and schema metadata constants for M1-T102 & M1-T104.

Every StrEnum in this module is kept in strict 1:1 parity with Notion Data Model
§3.1 - §3.10 and the PostgreSQL CHECK constraints defined in:
- db/migrations/0020_source_provenance_metadata.sql
- db/migrations/0030_events_facts_claims_contributions.sql
"""

from __future__ import annotations

from enum import StrEnum


# =============================================================================
# Role & Schema Identifiers
# =============================================================================
MIGRATOR_ROLE = "cm_migrator"
API_APP_ROLE = "cm_api_app"
WORKER_APP_ROLE = "cm_worker_app"
AUTH_CTX_SCHEMA = "auth_ctx"

SCHEMA_MIGRATION_FILES: tuple[str, ...] = (
    "0020_source_provenance_metadata.sql",
    "0030_events_facts_claims_contributions.sql",
)

MIGRATION_0020_TABLES: tuple[str, ...] = (
    "source_accounts",
    "source_resources",
    "source_objects",
    "source_object_revisions",
    "content_units",
)

MIGRATION_0030_TABLES: tuple[str, ...] = (
    "identities",
    "external_identity_mappings",
    "work_events",
    "event_memberships",
    "event_composite_memberships",
    "review_actions",
    "facts",
    "fact_support",
    "contributions",
    "contribution_support",
    "generation_runs",
    "claims",
    "claim_atoms",
    "claim_atom_support",
)

MIGRATION_0050_TABLES: tuple[str, ...] = (
    "sync_item_outcomes",
    "sync_commit_pr_associations",
    "sync_run_page_observations",
    "sync_run_event_promotions",
)

TENANT_DOMAIN_TABLES: tuple[str, ...] = MIGRATION_0020_TABLES + MIGRATION_0030_TABLES
ALL_TENANT_DOMAIN_TABLES: tuple[str, ...] = TENANT_DOMAIN_TABLES + MIGRATION_0050_TABLES

READONLY_PROVENANCE_VIEWS: tuple[str, ...] = (
    "v_claim_provenance_chain",
    "v_claim_verification_summary",
)

# Intentionally deferred items (OD-007 / Data Model §8) that MUST NOT be created in M1.
DEFERRED_POST_MVP_TABLES: frozenset[str] = frozenset(
    {
        "embeddings",
        "event_cannot_link_pairs",
        "sync_watermarks",
        "resource_leases",
    }
)

# Expected (child_table, fk_column, parent_table) Composite FK pairs on (workspace_id, <col>).
COMPOSITE_FK_SPEC: tuple[tuple[str, str, str], ...] = (
    ("source_resources", "source_account_id", "source_accounts"),
    ("source_objects", "source_resource_id", "source_resources"),
    ("source_object_revisions", "source_object_id", "source_objects"),
    ("content_units", "source_object_revision_id", "source_object_revisions"),
    ("external_identity_mappings", "identity_id", "identities"),
    ("work_events", "primary_anchor_source_object_id", "source_objects"),
    ("event_memberships", "event_id", "work_events"),
    ("event_memberships", "source_object_id", "source_objects"),
    ("event_composite_memberships", "composite_event_id", "work_events"),
    ("event_composite_memberships", "member_event_id", "work_events"),
    ("event_composite_memberships", "added_by_review_action_id", "review_actions"),
    ("facts", "event_id", "work_events"),
    ("facts", "subject_identity_id", "identities"),
    ("fact_support", "fact_id", "facts"),
    ("fact_support", "content_unit_id", "content_units"),
    ("contributions", "event_id", "work_events"),
    ("contributions", "identity_id", "identities"),
    ("contribution_support", "contribution_id", "contributions"),
    ("contribution_support", "fact_id", "facts"),
    ("generation_runs", "event_id", "work_events"),
    ("claims", "event_id", "work_events"),
    ("claims", "generator_run_id", "generation_runs"),
    ("claims", "last_verification_run_id", "generation_runs"),
    ("claims", "supersedes_claim_id", "claims"),
    ("claim_atoms", "claim_id", "claims"),
    ("claim_atom_support", "claim_atom_id", "claim_atoms"),
    ("claim_atom_support", "fact_id", "facts"),
    ("claim_atom_support", "validation_run_id", "generation_runs"),
)

MIGRATION_0050_COMPOSITE_FK_SPEC: tuple[tuple[str, str, str], ...] = (
    ("sync_item_outcomes", "sync_run_id", "sync_runs"),
    ("sync_item_outcomes", "source_resource_id", "source_resources"),
    ("sync_item_outcomes", "source_object_id", "source_objects"),
    ("sync_commit_pr_associations", "sync_run_id", "sync_runs"),
    ("sync_commit_pr_associations", "source_resource_id", "source_resources"),
    ("sync_commit_pr_associations", "commit_source_object_id", "source_objects"),
    ("sync_run_page_observations", "sync_run_id", "sync_runs"),
    ("sync_run_event_promotions", "sync_run_id", "sync_runs"),
    ("sync_run_event_promotions", "source_object_id", "source_objects"),
    ("sync_run_event_promotions", "work_event_id", "work_events"),
)


# =============================================================================
# Migration 0020 — Source & Plan C Provenance Enums (Data Model §3.2 - §3.4)
# =============================================================================
class SourceProvider(StrEnum):
    GITHUB = "GITHUB"


class SourceAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class SourceResourceType(StrEnum):
    GITHUB_REPOSITORY = "GITHUB_REPOSITORY"


class ResourceVisibility(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    INTERNAL = "INTERNAL"


class ProcessingMode(StrEnum):
    PUBLIC_CLOUD = "PUBLIC_CLOUD"
    PRIVATE_LOCAL = "PRIVATE_LOCAL"
    PRIVATE_CLOUD_OPT_IN = "PRIVATE_CLOUD_OPT_IN"


class RawStorageMode(StrEnum):
    """MVP Plan C (방안 C) invariant: only NONE is permitted."""

    NONE = "NONE"


class SourceResourceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    POLICY_REVIEW_REQUIRED = "POLICY_REVIEW_REQUIRED"
    DELETED = "DELETED"


class SourceObjectType(StrEnum):
    PR = "PR"
    COMMIT = "COMMIT"
    ISSUE = "ISSUE"
    REVIEW = "REVIEW"
    COMMENT = "COMMENT"


class SourceObjectLifecycleState(StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"
    ORPHANED = "ORPHANED"
    POLICY_REVIEW_REQUIRED = "POLICY_REVIEW_REQUIRED"


class PrAssociationState(StrEnum):
    MATCHED_PR = "MATCHED_PR"
    NO_MATCH_OBSERVED = "NO_MATCH_OBSERVED"
    UNKNOWN = "UNKNOWN"


class ContentUnitType(StrEnum):
    PR_TITLE = "PR_TITLE"
    PR_BODY_SECTION = "PR_BODY_SECTION"
    ISSUE_SECTION = "ISSUE_SECTION"
    COMMENT = "COMMENT"
    REVIEW_COMMENT = "REVIEW_COMMENT"
    COMMIT_MESSAGE = "COMMIT_MESSAGE"
    DIFF_HUNK = "DIFF_HUNK"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class RetentionClass(StrEnum):
    """MVP Plan C (방안 C) invariant: only NONE is permitted."""

    NONE = "NONE"


class ContentUnitPolicyState(StrEnum):
    ACTIVE = "ACTIVE"
    REDACTED = "REDACTED"
    PURGED = "PURGED"
    BLOCKED = "BLOCKED"


# =============================================================================
# Migration 0030 — Identity, WorkEvent, Fact, Contribution & Claim (§3.5 - §3.10)
# =============================================================================
class IdentityType(StrEnum):
    PERSON = "PERSON"
    BOT = "BOT"
    UNKNOWN = "UNKNOWN"


class IdentityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    AMBIGUOUS = "AMBIGUOUS"
    MERGED = "MERGED"
    REVOKED = "REVOKED"


class IdentityVerificationMethod(StrEnum):
    OAUTH_ACCOUNT = "OAUTH_ACCOUNT"
    PROVIDER_ASSOCIATION = "PROVIDER_ASSOCIATION"
    USER_CONFIRMATION = "USER_CONFIRMATION"


class ExternalIdentityMappingStatus(StrEnum):
    VERIFIED = "VERIFIED"
    USER_CONFIRMED = "USER_CONFIRMED"
    HEURISTIC = "HEURISTIC"
    CONFLICTED = "CONFLICTED"
    REVOKED = "REVOKED"


class WorkEventAnchorMode(StrEnum):
    PR = "PR"
    PR_SEGMENT = "PR_SEGMENT"
    DIRECT_COMMIT = "DIRECT_COMMIT"
    USER_COMPOSITE = "USER_COMPOSITE"


class WorkEventStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"


class EventMembershipRelationClass(StrEnum):
    MUST_LINK = "MUST_LINK"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    CANNOT_LINK = "CANNOT_LINK"


class EventMembershipOrigin(StrEnum):
    SOURCE = "SOURCE"
    RULE = "RULE"
    USER = "USER"


class RecordOrigin(StrEnum):
    RULE = "RULE"
    USER = "USER"


class LinkStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"


class CompositeMembershipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class ReviewActionType(StrEnum):
    CONFIRM = "CONFIRM"
    EDIT = "EDIT"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    DISSOLVE = "DISSOLVE"
    REJECT = "REJECT"
    REOPEN = "REOPEN"


class ReviewTargetType(StrEnum):
    WORK_EVENT = "WORK_EVENT"
    FACT = "FACT"
    CONTRIBUTION = "CONTRIBUTION"
    CLAIM = "CLAIM"
    CLAIM_ATOM = "CLAIM_ATOM"
    IDENTITY_MAPPING = "IDENTITY_MAPPING"
    SOURCE_RESOURCE = "SOURCE_RESOURCE"
    SOURCE_OBJECT = "SOURCE_OBJECT"


class FactType(StrEnum):
    PROBLEM = "PROBLEM"
    ACTION = "ACTION"
    TECHNOLOGY = "TECHNOLOGY"
    RESULT = "RESULT"
    ROLE = "ROLE"
    METRIC = "METRIC"


class FactStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    SUPPORTED = "SUPPORTED"
    USER_CONFIRMED = "USER_CONFIRMED"
    CONFLICTED = "CONFLICTED"
    STALE = "STALE"
    REJECTED = "REJECTED"


class SupportType(StrEnum):
    DIRECT = "DIRECT"
    CONTEXT = "CONTEXT"
    CONTRADICTS = "CONTRADICTS"


class SupportValidator(StrEnum):
    RULE = "RULE"
    USER = "USER"
    MODEL = "MODEL"


class ContributionRole(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    REVIEWED = "REVIEWED"
    PROPOSED = "PROPOSED"
    DESIGNED = "DESIGNED"
    OPERATED = "OPERATED"
    MENTORED = "MENTORED"
    MEASURED = "MEASURED"


MVP_RULE_CONTRIBUTION_ROLES: frozenset[ContributionRole] = frozenset(
    {
        ContributionRole.IMPLEMENTED,
        ContributionRole.REVIEWED,
    }
)


class ContributionStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    SOURCE_SUPPORTED = "SOURCE_SUPPORTED"
    USER_CONFIRMED = "USER_CONFIRMED"
    REJECTED = "REJECTED"
    STALE = "STALE"


class GenerationRunType(StrEnum):
    FACT_CANDIDATE_EXTRACTION = "FACT_CANDIDATE_EXTRACTION"
    FACT_SUPPORT_VALIDATION = "FACT_SUPPORT_VALIDATION"
    CLAIM_DRAFT_GENERATION = "CLAIM_DRAFT_GENERATION"
    CLAIM_ATOMIZATION = "CLAIM_ATOMIZATION"
    CLAIM_SUPPORT_VALIDATION = "CLAIM_SUPPORT_VALIDATION"
    REFETCH_VERIFICATION = "REFETCH_VERIFICATION"


class GenerationRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ClaimStatus(StrEnum):
    DRAFT = "DRAFT"
    DRAFT_INVALID = "DRAFT_INVALID"
    SUPPORT_VALIDATED = "SUPPORT_VALIDATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"


class ClaimVerification(StrEnum):
    """Read-only computed verification status (never directly persisted on claims)."""

    SOURCE_VERIFIED = "SOURCE_VERIFIED"
    USER_CONFIRMED = "USER_CONFIRMED"
    MIXED = "MIXED"
    UNVERIFIED = "UNVERIFIED"


class ClaimAtomType(StrEnum):
    PROBLEM = "PROBLEM"
    ACTION = "ACTION"
    TECHNOLOGY = "TECHNOLOGY"
    RESULT = "RESULT"
    ROLE = "ROLE"
    METRIC = "METRIC"


class ClaimAtomStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    USER_CONFIRMED = "USER_CONFIRMED"
    UNSUPPORTED = "UNSUPPORTED"
    STALE = "STALE"
