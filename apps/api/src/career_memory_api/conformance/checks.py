"""T-106A UML / OpenAPI / AI JSON Schema / Python Enum / PostgreSQL 18 drift conformance engine.

Never auto-mutates committed specifications or snapshots during verification. Every detected
violation reports:
- Stable error code
- Actionable unified diff or set diff
- Authoritative Source of Truth reference (Notion Data Model, MVP API, 방안 C §7, ADR-020, T-106/T-106A)
- Exact remediation hint
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from career_memory_api.auth.oauth import OAuthProvider, OrphanSecretCleanupStatus
from career_memory_api.auth.postgres_guard import WorkspaceRole
from career_memory_api.backfill_engine.models import (
    AssociationMatchMechanism,
    BackfillStreamName,
    ItemOutcomeKind,
)
from career_memory_api.canonicalization.models import (
    ContentUnitType as CanonicalContentUnitType,
)
from career_memory_api.github_connect.models import (
    GitHubCredentialMode,
    PolicyDecision,
    PolicyGuardedOperation,
    PolicyGuidanceCode,
    PolicyTransitionKind,
)
from career_memory_api.conformance.ai_schemas import (
    AI_SCHEMA_BUILDERS,
    generate_ai_schema_files,
)
from career_memory_api.conformance.db_catalog import (
    diff_db_catalog_snapshots,
    render_db_catalog_snapshot_json,
    run_fresh_and_staged_pg18_catalog_smoke,
)
from career_memory_api.conformance.openapi_spec import (
    FORBIDDEN_OPENAPI_PATH_FRAGMENTS,
    generate_integrated_openapi_spec,
    render_integrated_openapi_json,
)
from career_memory_api.contracts_api.models import (
    FORBIDDEN_PROJECT_SCOPE_FIELDS,
    FORBIDDEN_VERIFICATION_WRITE_FIELDS,
    SearchEntityType,
)
from career_memory_api.jobs.models import (
    AuditActorType,
    DeletionJobStatus,
    JobErrorClass,
    JobStatus,
    OutboxStatus,
)
from career_memory_api.resolver.models import ResolutionStatus
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    CompositeMembershipStatus,
    ContentUnitPolicyState,
    ContentUnitType,
    ContributionRole,
    ContributionStatus,
    EventMembershipOrigin,
    EventMembershipRelationClass,
    ExternalIdentityMappingStatus,
    FactStatus,
    FactType,
    GenerationRunStatus,
    GenerationRunType,
    IdentityStatus,
    IdentityType,
    IdentityVerificationMethod,
    PrAssociationState,
    ProcessingMode,
    RawStorageMode,
    RecordOrigin,
    ResourceVisibility,
    RetentionClass,
    ReviewActionType,
    ReviewTargetType,
    SourceAccountStatus,
    SourceObjectLifecycleState,
    SourceObjectType,
    SourceProvider,
    SourceResourceStatus,
    SourceResourceType,
    SupportType,
    SupportValidator,
    WorkEventAnchorMode,
    WorkEventStatus,
    LinkStatus,
)
from career_memory_api.schema_contracts.metadata_guard import FORBIDDEN_RAW_TEXT_KEYS
from career_memory_api.sync_runs.models import ProcessingStatus, SyncRunMode, SyncRunStatus

M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1 = "07594d75425744d6149250a9f7ff95ac24659f80"

AUTHORITY_REFS: dict[str, str] = {
    "DATA_MODEL": "Notion Data Model (https://app.notion.com/p/3e3f86591b5f811bb88be70b33e10af5)",
    "MVP_API": "Notion MVP API Specification (https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa)",
    "PLAN_C": "Notion 방안 C §7 (https://app.notion.com/p/41df86591b5f8115a789f93759ea1d28)",
    "T106": "Notion T-106 (https://app.notion.com/p/3e3f86591b5f8148908dd84514f1fbee)",
    "T106A": "Notion T-106A (https://app.notion.com/p/3eff86591b5f81528bf7d356385d3c99)",
    "ADR_020": "ADR-020 / M1 Foundation (https://app.notion.com/p/3e3f86591b5f811fa85dfdb2ba8383e0)",
}


@dataclass(frozen=True, slots=True)
class ConformanceViolation:
    """Actionable drift or contract invariant violation."""

    axis: str
    code: str
    target: str
    message: str
    authority: str
    diff: str = ""
    remediation: str = ""


class ConformanceError(RuntimeError):
    """Raised when one or more conformance gates fail."""

    def __init__(self, violations: list[ConformanceViolation]) -> None:
        self.violations = tuple(violations)
        summary = "\n".join(
            f"[{v.axis}] {v.code} ({v.target}): {v.message} | Authority: {v.authority}"
            for v in violations
        )
        super().__init__(f"Conformance check failed with {len(violations)} violation(s):\n{summary}")


@dataclass(frozen=True, slots=True)
class EnumParityBinding:
    """Cross-layer enum registration linking Python, SQL migrations, OpenAPI, and AI JSON Schemas."""

    name: str
    enum_cls: type[StrEnum]
    sql_targets: tuple[tuple[str, str], ...]  # (migration_filename, constraint_or_column_marker)
    openapi_schema_name: str | None = None
    ai_schema_paths: tuple[tuple[str, tuple[str, ...]], ...] = ()
    authority: str = AUTHORITY_REFS["DATA_MODEL"]


# Authoritative cross-layer parity manifest covering migrations 0010, 0020, 0030, and 0040.
ENUM_PARITY_MANIFEST: tuple[EnumParityBinding, ...] = (
    # 0010
    EnumParityBinding(
        name="WorkspaceRole",
        enum_cls=WorkspaceRole,
        sql_targets=(("0010_identity_workspace_source_auth.sql", "INLINE:workspace_memberships.role"),),
    ),
    EnumParityBinding(
        name="OAuthProvider",
        enum_cls=OAuthProvider,
        sql_targets=(("0010_identity_workspace_source_auth.sql", "INLINE:oauth_connect_states.provider"),),
    ),
    EnumParityBinding(
        name="OrphanSecretCleanupStatus",
        enum_cls=OrphanSecretCleanupStatus,
        sql_targets=(("0010_identity_workspace_source_auth.sql", "INLINE:orphan_secret_cleanups.status"),),
    ),
    # 0020
    EnumParityBinding(
        name="SourceProvider",
        enum_cls=SourceProvider,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_accounts_provider"),),
    ),
    EnumParityBinding(
        name="SourceAccountStatus",
        enum_cls=SourceAccountStatus,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_accounts_status"),),
    ),
    EnumParityBinding(
        name="SourceResourceType",
        enum_cls=SourceResourceType,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_resources_type"),),
    ),
    EnumParityBinding(
        name="ResourceVisibility",
        enum_cls=ResourceVisibility,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_resources_visibility"),),
    ),
    EnumParityBinding(
        name="ProcessingMode",
        enum_cls=ProcessingMode,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_resources_processing_mode"),),
    ),
    EnumParityBinding(
        name="RawStorageMode",
        enum_cls=RawStorageMode,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_resources_raw_storage_mode_none"),),
        authority=AUTHORITY_REFS["PLAN_C"],
    ),
    EnumParityBinding(
        name="SourceResourceStatus",
        enum_cls=SourceResourceStatus,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_resources_status"),),
    ),
    EnumParityBinding(
        name="SourceObjectType",
        enum_cls=SourceObjectType,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_objects_type"),),
    ),
    EnumParityBinding(
        name="SourceObjectLifecycleState",
        enum_cls=SourceObjectLifecycleState,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_source_objects_lifecycle_state"),),
    ),
    EnumParityBinding(
        name="PrAssociationState",
        enum_cls=PrAssociationState,
        sql_targets=(
            ("0020_source_provenance_metadata.sql", "ck_source_objects_pr_association_state"),
            ("0050_m2_backfill_observations.sql", "ck_sync_commit_pr_assoc_state"),
        ),
    ),
    EnumParityBinding(
        name="ContentUnitType",
        enum_cls=ContentUnitType,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_content_units_unit_type"),),
        openapi_schema_name="ContentUnitType",
    ),
    EnumParityBinding(
        name="RetentionClass",
        enum_cls=RetentionClass,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_content_units_retention_class_none"),),
        authority=AUTHORITY_REFS["PLAN_C"],
    ),
    EnumParityBinding(
        name="ContentUnitPolicyState",
        enum_cls=ContentUnitPolicyState,
        sql_targets=(("0020_source_provenance_metadata.sql", "ck_content_units_policy_state"),),
    ),
    # 0030
    EnumParityBinding(
        name="IdentityType",
        enum_cls=IdentityType,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_identities_type"),),
    ),
    EnumParityBinding(
        name="IdentityStatus",
        enum_cls=IdentityStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_identities_status"),),
    ),
    EnumParityBinding(
        name="IdentityVerificationMethod",
        enum_cls=IdentityVerificationMethod,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_external_identity_mappings_verification_method"),),
    ),
    EnumParityBinding(
        name="ExternalIdentityMappingStatus",
        enum_cls=ExternalIdentityMappingStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_external_identity_mappings_status"),),
    ),
    EnumParityBinding(
        name="WorkEventAnchorMode",
        enum_cls=WorkEventAnchorMode,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_work_events_anchor_mode"),),
    ),
    EnumParityBinding(
        name="WorkEventStatus",
        enum_cls=WorkEventStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_work_events_status"),),
    ),
    EnumParityBinding(
        name="EventMembershipRelationClass",
        enum_cls=EventMembershipRelationClass,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_event_memberships_relation_class"),),
    ),
    EnumParityBinding(
        name="EventMembershipOrigin",
        enum_cls=EventMembershipOrigin,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_event_memberships_origin"),),
    ),
    EnumParityBinding(
        name="LinkStatus",
        enum_cls=LinkStatus,
        sql_targets=(
            ("0030_events_facts_claims_contributions.sql", "ck_event_memberships_status"),
            ("0030_events_facts_claims_contributions.sql", "ck_fact_support_status"),
            ("0030_events_facts_claims_contributions.sql", "ck_contribution_support_status"),
            ("0030_events_facts_claims_contributions.sql", "ck_claim_atom_support_status"),
        ),
    ),
    EnumParityBinding(
        name="CompositeMembershipStatus",
        enum_cls=CompositeMembershipStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_event_composite_memberships_status"),),
    ),
    EnumParityBinding(
        name="ReviewTargetType",
        enum_cls=ReviewTargetType,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_review_actions_target_type"),),
    ),
    EnumParityBinding(
        name="ReviewActionType",
        enum_cls=ReviewActionType,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_review_actions_action"),),
    ),
    EnumParityBinding(
        name="FactType",
        enum_cls=FactType,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_facts_fact_type"),),
        ai_schema_paths=(("fact_candidate.v1.schema.json", ("properties", "fact_type", "enum")),),
    ),
    EnumParityBinding(
        name="FactStatus",
        enum_cls=FactStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_facts_status"),),
    ),
    EnumParityBinding(
        name="SupportType",
        enum_cls=SupportType,
        sql_targets=(
            ("0030_events_facts_claims_contributions.sql", "ck_fact_support_type"),
            ("0030_events_facts_claims_contributions.sql", "ck_claim_atom_support_type"),
        ),
        ai_schema_paths=(
            ("fact_support_validation.v1.schema.json", ("properties", "support_type", "enum")),
            (
                "claim_support_validation.v1.schema.json",
                ("$defs", "AtomFactBinding", "properties", "support_type", "enum"),
            ),
        ),
    ),
    EnumParityBinding(
        name="SupportValidator",
        enum_cls=SupportValidator,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_fact_support_validator"),),
        ai_schema_paths=(
            ("fact_support_validation.v1.schema.json", ("properties", "validator", "enum")),
        ),
    ),
    EnumParityBinding(
        name="ContributionRole",
        enum_cls=ContributionRole,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_contributions_role"),),
        ai_schema_paths=(
            (
                "claim_support_validation.v1.schema.json",
                ("$defs", "RoleContributionGrounding", "properties", "role", "enum"),
            ),
        ),
    ),
    EnumParityBinding(
        name="RecordOrigin",
        enum_cls=RecordOrigin,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_contributions_origin"),),
    ),
    EnumParityBinding(
        name="ContributionStatus",
        enum_cls=ContributionStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_contributions_status"),),
    ),
    EnumParityBinding(
        name="GenerationRunType",
        enum_cls=GenerationRunType,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_generation_runs_run_type"),),
        ai_schema_paths=(
            ("generation_run_manifest.v1.schema.json", ("properties", "run_type", "enum")),
        ),
    ),
    EnumParityBinding(
        name="GenerationRunStatus",
        enum_cls=GenerationRunStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_generation_runs_status"),),
        ai_schema_paths=(
            ("generation_run_manifest.v1.schema.json", ("properties", "status", "enum")),
        ),
    ),
    EnumParityBinding(
        name="ClaimStatus",
        enum_cls=ClaimStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_claims_status"),),
        openapi_schema_name="ClaimStatus",
    ),
    EnumParityBinding(
        name="ClaimVerification",
        enum_cls=ClaimVerification,
        sql_targets=(),
        openapi_schema_name="ClaimVerification",
        ai_schema_paths=(
            (
                "claim_support_validation.v1.schema.json",
                ("properties", "computed_verification", "enum"),
            ),
        ),
    ),
    EnumParityBinding(
        name="ClaimAtomType",
        enum_cls=ClaimAtomType,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_claim_atoms_atom_type"),),
        ai_schema_paths=(
            (
                "claim_atomization.v1.schema.json",
                ("$defs", "ClaimAtomDraft", "properties", "atom_type", "enum"),
            ),
            (
                "claim_support_validation.v1.schema.json",
                ("$defs", "AtomValidationResult", "properties", "atom_type", "enum"),
            ),
        ),
    ),
    EnumParityBinding(
        name="ClaimAtomStatus",
        enum_cls=ClaimAtomStatus,
        sql_targets=(("0030_events_facts_claims_contributions.sql", "ck_claim_atoms_status"),),
        ai_schema_paths=(
            (
                "claim_support_validation.v1.schema.json",
                ("$defs", "AtomValidationResult", "properties", "status", "enum"),
            ),
        ),
    ),
    # 0040
    EnumParityBinding(
        name="SyncRunMode",
        enum_cls=SyncRunMode,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_sync_runs_mode"),),
        openapi_schema_name="SyncRunMode",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="SyncRunStatus",
        enum_cls=SyncRunStatus,
        sql_targets=(
            ("0040_sync_runs_jobs_outbox_audit.sql", "ck_sync_runs_status"),
            ("0040_sync_runs_jobs_outbox_audit.sql", "ck_sync_checkpoints_stream_status"),
        ),
        openapi_schema_name="ImportState",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="ProcessingStatus",
        enum_cls=ProcessingStatus,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_sync_runs_processing_status"),),
        openapi_schema_name="ProcessingStatus",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="JobStatus",
        enum_cls=JobStatus,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_jobs_status"),),
    ),
    EnumParityBinding(
        name="JobErrorClass",
        enum_cls=JobErrorClass,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_jobs_error_class"),),
    ),
    EnumParityBinding(
        name="OutboxStatus",
        enum_cls=OutboxStatus,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_outbox_status"),),
    ),
    EnumParityBinding(
        name="AuditActorType",
        enum_cls=AuditActorType,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_audit_events_actor_type"),),
    ),
    EnumParityBinding(
        name="DeletionJobStatus",
        enum_cls=DeletionJobStatus,
        sql_targets=(("0040_sync_runs_jobs_outbox_audit.sql", "ck_deletion_jobs_status"),),
    ),
    # Resolver & Search API enums
    EnumParityBinding(
        name="ResolutionStatus",
        enum_cls=ResolutionStatus,
        sql_targets=(),
        openapi_schema_name="ResolutionStatus",
        authority=AUTHORITY_REFS["PLAN_C"],
    ),
    EnumParityBinding(
        name="SearchEntityType",
        enum_cls=SearchEntityType,
        sql_targets=(),
        openapi_schema_name="SearchEntityType",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    # 0050 M2 Backfill Observations
    EnumParityBinding(
        name="BackfillStreamName",
        enum_cls=BackfillStreamName,
        sql_targets=(
            ("0050_m2_backfill_observations.sql", "ck_sync_item_outcomes_stream_name"),
            ("0050_m2_backfill_observations.sql", "ck_sync_run_page_obs_stream_name"),
        ),
        authority=AUTHORITY_REFS["DATA_MODEL"],
    ),
    EnumParityBinding(
        name="ItemOutcomeKind",
        enum_cls=ItemOutcomeKind,
        sql_targets=(("0050_m2_backfill_observations.sql", "ck_sync_item_outcomes_outcome_kind"),),
        authority=AUTHORITY_REFS["DATA_MODEL"],
    ),
    EnumParityBinding(
        name="AssociationMatchMechanism",
        enum_cls=AssociationMatchMechanism,
        sql_targets=(("0050_m2_backfill_observations.sql", "ck_sync_commit_pr_assoc_mechanism"),),
        authority=AUTHORITY_REFS["DATA_MODEL"],
    ),
    # M2-T201 GitHub Connect OpenAPI enums
    EnumParityBinding(
        name="GitHubCredentialMode",
        enum_cls=GitHubCredentialMode,
        sql_targets=(),
        openapi_schema_name="GitHubCredentialMode",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="PolicyDecision",
        enum_cls=PolicyDecision,
        sql_targets=(),
        openapi_schema_name="PolicyDecision",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="PolicyGuidanceCode",
        enum_cls=PolicyGuidanceCode,
        sql_targets=(),
        openapi_schema_name="PolicyGuidanceCode",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="PolicyTransitionKind",
        enum_cls=PolicyTransitionKind,
        sql_targets=(),
        openapi_schema_name="PolicyTransitionKind",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
    EnumParityBinding(
        name="PolicyGuardedOperation",
        enum_cls=PolicyGuardedOperation,
        sql_targets=(),
        openapi_schema_name="PolicyGuardedOperation",
        authority=AUTHORITY_REFS["MVP_API"],
    ),
)


def default_repo_root() -> Path:
    """Resolve repository root from this module path."""
    # .../apps/api/src/career_memory_api/conformance/checks.py -> parents[5]
    return Path(__file__).resolve().parents[5]


def compute_git_blob_sha1(raw_bytes: bytes) -> str:
    """Compute exact git blob SHA-1 without invoking git subprocesses."""
    header = f"blob {len(raw_bytes)}\0".encode("ascii")
    return hashlib.sha1(header + raw_bytes).hexdigest()


def _unified_diff(expected_text: str, actual_text: str, *, label: str) -> str:
    lines = list(
        difflib.unified_diff(
            expected_text.splitlines(keepends=True),
            actual_text.splitlines(keepends=True),
            fromfile=f"expected/{label}",
            tofile=f"actual/{label}",
            n=3,
        )
    )
    if len(lines) > 80:
        lines = lines[:80] + [f"... ({len(lines) - 80} more diff lines truncated)\n"]
    return "".join(lines)


def _extract_sql_constraint_literals(sql_text: str, marker: str) -> set[str]:
    """Parse string literal enum values from a named CHECK constraint or inline column check."""
    if marker.startswith("INLINE:"):
        table_name, col_name = marker.split(":", 1)[1].split(".", 1)
        table_match = re.search(
            rf"CREATE TABLE IF NOT EXISTS public\.{re.escape(table_name)}\s*\((.*?)\);",
            sql_text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not table_match:
            raise ValueError(f"Could not locate table public.{table_name} in SQL")
        body = table_match.group(1)
        col_match = re.search(
            rf"\b{re.escape(col_name)}\b[^\n]*CHECK\s*\((.+)\)\s*,?\s*$",
            body,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if not col_match:
            raise ValueError(f"Could not locate inline CHECK for {table_name}.{col_name}")
        expr = col_match.group(1)
    else:
        constraint_match = re.search(
            rf"CONSTRAINT\s+{re.escape(marker)}\s+CHECK\s*\((.*?)\)(?:\s*,|\s*\n\s*\);)",
            sql_text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not constraint_match:
            raise ValueError(f"Could not locate CONSTRAINT {marker} in SQL")
        expr = constraint_match.group(1)

    in_match = re.search(r"\bIN\s*\((.*?)\)", expr, flags=re.DOTALL | re.IGNORECASE)
    if in_match:
        return set(re.findall(r"'([^']+)'", in_match.group(1)))
    eq_match = re.search(r"=\s*'([^']+)'", expr)
    if eq_match:
        return {eq_match.group(1)}
    raise ValueError(f"Could not parse enum literals from CHECK expression for {marker}: {expr!r}")


def _extract_sql_forbidden_raw_keys(sql_text: str) -> set[str]:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION public\.cm_jsonb_contains_forbidden_raw_key.*?AND lower\(key_name\) IN \((.*?)\)",
        sql_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise ValueError("Could not locate public.cm_jsonb_contains_forbidden_raw_key in SQL")
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _lookup_nested_key(obj: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = obj
    for part in path:
        cur = cur[part]
    return cur


def _scan_openapi_for_forbidden_keys(
    node: Any,
    forbidden: frozenset[str],
    path: str = "$",
) -> list[str]:
    hits: list[str] = []
    if isinstance(node, Mapping):
        for k, v in node.items():
            k_str = str(k)
            child = f"{path}.{k_str}"
            if k_str.strip().lower() in forbidden:
                hits.append(child)
            # Also check OpenAPI parameter 'name' fields
            if k_str == "name" and isinstance(v, str) and v.strip().lower() in forbidden:
                hits.append(f"{path}[name={v}]")
            hits.extend(_scan_openapi_for_forbidden_keys(v, forbidden, child))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            hits.extend(_scan_openapi_for_forbidden_keys(item, forbidden, f"{path}[{idx}]"))
    return hits


def validate_openapi_invariants(
    spec: Mapping[str, Any],
    *,
    m0_yaml_text: str | None = None,
) -> list[ConformanceViolation]:
    """Validate M1 OpenAPI invariants (M0 4-route compatibility, OD-008, OD-003, Plan C, no BYOK)."""
    violations: list[ConformanceViolation] = []
    paths = spec.get("paths", {})
    schemas = spec.get("components", {}).get("schemas", {})

    # 1. Required M1 paths
    required_routes: tuple[tuple[str, str], ...] = (
        ("/healthz", "get"),
        ("/v1/sync-runs", "post"),
        ("/v1/sync-runs", "get"),
        ("/v1/sync-runs/{id}", "get"),
        ("/v1/sync-runs/{id}/retry", "post"),
        ("/v1/content-units/{content_unit_id}/content", "get"),
        ("/v1/claims/drafts", "post"),
        ("/v1/claims/{claim_id}", "patch"),
        ("/v1/claims/{claim_id}/approve", "post"),
        ("/v1/search", "get"),
        ("/v1/source-resources/{source_resource_id}/deletion-preview", "post"),
        ("/v1/source-resources/{source_resource_id}/deletion-confirm", "post"),
        ("/v1/sources/github/oauth/start", "post"),
        ("/v1/sources/github/oauth/callback", "post"),
        ("/v1/source-resources", "post"),
        ("/v1/source-resources", "get"),
        ("/v1/source-accounts", "get"),
        ("/v1/source-resources/{source_resource_id}/verify-policy", "post"),
        ("/v1/source-accounts/{source_account_id}/disconnect", "post"),
    )
    for route_path, method in required_routes:
        if route_path not in paths or method not in paths[route_path]:
            violations.append(
                ConformanceViolation(
                    axis="openapi_invariants",
                    code="MISSING_REQUIRED_OPENAPI_ROUTE",
                    target=f"{method.upper()} {route_path}",
                    message=f"Required M1 endpoint {method.upper()} {route_path} is missing from OpenAPI.",
                    authority=AUTHORITY_REFS["MVP_API"],
                )
            )

    # 2. Zero BYOK / external LLM subscription callback routes (T-106)
    for route_path in paths.keys():
        lowered = route_path.lower()
        for fragment in FORBIDDEN_OPENAPI_PATH_FRAGMENTS:
            if fragment in lowered:
                violations.append(
                    ConformanceViolation(
                        axis="openapi_invariants",
                        code="PROHIBITED_OPENAPI_ROUTE",
                        target=route_path,
                        message=(
                            f"Route {route_path!r} matches prohibited M1 fragment {fragment!r} "
                            "(BYOK and external subscription LLM callbacks are forbidden in M1)."
                        ),
                        authority=AUTHORITY_REFS["T106"],
                    )
                )

    # 3. OD-008: zero project_id anywhere in OpenAPI
    project_hits = _scan_openapi_for_forbidden_keys(spec, FORBIDDEN_PROJECT_SCOPE_FIELDS)
    if project_hits:
        violations.append(
            ConformanceViolation(
                axis="openapi_invariants",
                code="OD008_PROJECT_ID_DETECTED",
                target="contracts/openapi/career-memory-v1.openapi.json",
                message=(
                    f"OD-008 violation: prohibited project_id found in OpenAPI at: {', '.join(project_hits)}"
                ),
                authority=AUTHORITY_REFS["MVP_API"],
            )
        )

    # 4. Claim write request schemas MUST NOT accept verification or SOURCE_VERIFIED
    for req_schema_name in (
        "ClaimDraftCreateRequest",
        "ClaimEditRequest",
        "ClaimApproveRequest",
    ):
        req_schema = schemas.get(req_schema_name, {})
        if req_schema.get("additionalProperties") is not False:
            violations.append(
                ConformanceViolation(
                    axis="openapi_invariants",
                    code="CLAIM_WRITE_SCHEMA_NOT_CLOSED",
                    target=req_schema_name,
                    message=f"{req_schema_name} must set additionalProperties: false.",
                    authority=AUTHORITY_REFS["DATA_MODEL"],
                )
            )
        props = {k.lower() for k in req_schema.get("properties", {}).keys()}
        bad_props = sorted(props & FORBIDDEN_VERIFICATION_WRITE_FIELDS)
        if bad_props:
            violations.append(
                ConformanceViolation(
                    axis="openapi_invariants",
                    code="PROHIBITED_WRITABLE_VERIFICATION_SCHEMA",
                    target=req_schema_name,
                    message=f"{req_schema_name} must never expose writable verification field(s): {bad_props}",
                    authority=AUTHORITY_REFS["DATA_MODEL"],
                )
            )

    # 5. Plan C content-unit error codes
    err_codes = set(
        schemas.get("SourceResolutionErrorCode", {}).get("enum", [])
    )
    required_plan_c_codes = {
        "SOURCE_REVISION_MISMATCH",
        "SOURCE_LOCATOR_INVALID",
        "SOURCE_POLICY_BLOCKED",
        "SOURCE_UNAVAILABLE",
        "SOURCE_FETCH_LIMIT",
    }
    missing_plan_c_codes = sorted(required_plan_c_codes - err_codes)
    if missing_plan_c_codes:
        violations.append(
            ConformanceViolation(
                axis="openapi_invariants",
                code="MISSING_PLAN_C_ERROR_CODES",
                target="SourceResolutionErrorCode",
                message=f"Missing Plan C §7 error codes in OpenAPI: {missing_plan_c_codes}",
                authority=AUTHORITY_REFS["PLAN_C"],
            )
        )

    # 6. M0 contracts/import-v1/openapi.yaml 4-route compatibility parity
    if m0_yaml_text is not None:
        m0_import_states = re.search(r"ImportState:\s*\n\s*type:\s*string\s*\n\s*enum:\s*\[([^\]]+)\]", m0_yaml_text)
        m0_proc_states = re.search(r"ProcessingStatus:\s*\n\s*type:\s*string\s*\n\s*enum:\s*\[([^\]]+)\]", m0_yaml_text)
        if m0_import_states:
            expected_states = [s.strip() for s in m0_import_states.group(1).split(",")]
            actual_states = schemas.get("ImportState", {}).get("enum", [])
            if expected_states != actual_states:
                violations.append(
                    ConformanceViolation(
                        axis="openapi_invariants",
                        code="M0_IMPORT_STATE_DRIFT",
                        target="components.schemas.ImportState",
                        message=(
                            f"Integrated OpenAPI ImportState {actual_states} drifted from M0 "
                            f"contracts/import-v1/openapi.yaml {expected_states}."
                        ),
                        authority=AUTHORITY_REFS["MVP_API"],
                    )
                )
        if m0_proc_states:
            expected_proc = [s.strip() for s in m0_proc_states.group(1).split(",")]
            actual_proc = schemas.get("ProcessingStatus", {}).get("enum", [])
            if expected_proc != actual_proc:
                violations.append(
                    ConformanceViolation(
                        axis="openapi_invariants",
                        code="M0_PROCESSING_STATE_DRIFT",
                        target="components.schemas.ProcessingStatus",
                        message=(
                            f"Integrated OpenAPI ProcessingStatus {actual_proc} drifted from M0 "
                            f"contracts/import-v1/openapi.yaml {expected_proc}."
                        ),
                        authority=AUTHORITY_REFS["MVP_API"],
                    )
                )

    return violations


def check_artifact_drift(repo_root: Path) -> list[ConformanceViolation]:
    """Axis 1: Verify committed OpenAPI & 6 AI JSON Schema snapshots match generated code byte-for-byte."""
    violations: list[ConformanceViolation] = []

    # 1. Immutable M0 snapshot check (exact git blob hash)
    m0_path = repo_root / "contracts" / "import-v1" / "openapi.yaml"
    if not m0_path.is_file():
        violations.append(
            ConformanceViolation(
                axis="artifact_drift",
                code="M0_OPENAPI_MISSING",
                target=str(m0_path),
                message="Immutable M0 OpenAPI snapshot contracts/import-v1/openapi.yaml is missing.",
                authority=AUTHORITY_REFS["MVP_API"],
            )
        )
        m0_yaml_text = None
    else:
        m0_bytes = m0_path.read_bytes()
        actual_blob = compute_git_blob_sha1(m0_bytes)
        m0_yaml_text = m0_bytes.decode("utf-8")
        if actual_blob != M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1:
            violations.append(
                ConformanceViolation(
                    axis="artifact_drift",
                    code="M0_OPENAPI_MUTATED",
                    target="contracts/import-v1/openapi.yaml",
                    message=(
                        f"Immutable M0 OpenAPI blob hash mutated: expected "
                        f"{M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1}, got {actual_blob}."
                    ),
                    authority=AUTHORITY_REFS["MVP_API"],
                    remediation="Restore contracts/import-v1/openapi.yaml verbatim; never edit it.",
                )
            )

    # 2. Integrated M1 OpenAPI JSON snapshot drift
    expected_openapi_text = render_integrated_openapi_json()
    openapi_path = repo_root / "contracts" / "openapi" / "career-memory-v1.openapi.json"
    if not openapi_path.is_file():
        violations.append(
            ConformanceViolation(
                axis="artifact_drift",
                code="OPENAPI_SNAPSHOT_MISSING",
                target="contracts/openapi/career-memory-v1.openapi.json",
                message="Integrated M1 OpenAPI snapshot file is missing.",
                authority=AUTHORITY_REFS["T106"],
                remediation="Run: cd apps/api && uv run python ../../scripts/run_conformance_ci.py --write-snapshots",
            )
        )
    else:
        committed_openapi_text = openapi_path.read_text(encoding="utf-8")
        if committed_openapi_text != expected_openapi_text:
            violations.append(
                ConformanceViolation(
                    axis="artifact_drift",
                    code="OPENAPI_SNAPSHOT_DRIFT",
                    target="contracts/openapi/career-memory-v1.openapi.json",
                    message="Committed integrated OpenAPI snapshot differs from code-generated specification.",
                    authority=AUTHORITY_REFS["T106"],
                    diff=_unified_diff(
                        expected_openapi_text,
                        committed_openapi_text,
                        label="contracts/openapi/career-memory-v1.openapi.json",
                    ),
                    remediation="Review API changes against Notion MVP API, then regenerate with --write-snapshots.",
                )
            )

    # Validate invariants on generated OpenAPI spec
    violations.extend(
        validate_openapi_invariants(
            generate_integrated_openapi_spec(),
            m0_yaml_text=m0_yaml_text,
        )
    )

    # 3. 6 AI JSON Schemas + MANIFEST.json drift
    expected_ai_files = generate_ai_schema_files()
    ai_dir = repo_root / "contracts" / "ai-schemas" / "v1"
    for filename, expected_text in expected_ai_files.items():
        file_path = ai_dir / filename
        rel_target = f"contracts/ai-schemas/v1/{filename}"
        if not file_path.is_file():
            violations.append(
                ConformanceViolation(
                    axis="artifact_drift",
                    code="AI_SCHEMA_SNAPSHOT_MISSING",
                    target=rel_target,
                    message=f"Expected AI JSON Schema snapshot {rel_target} is missing.",
                    authority=AUTHORITY_REFS["T106"],
                    remediation="Run: cd apps/api && uv run python ../../scripts/run_conformance_ci.py --write-snapshots",
                )
            )
            continue
        actual_text = file_path.read_text(encoding="utf-8")
        if actual_text != expected_text:
            violations.append(
                ConformanceViolation(
                    axis="artifact_drift",
                    code="AI_SCHEMA_SNAPSHOT_DRIFT",
                    target=rel_target,
                    message=f"Committed AI schema snapshot {rel_target} differs from generated schema.",
                    authority=AUTHORITY_REFS["T106"],
                    diff=_unified_diff(expected_text, actual_text, label=rel_target),
                    remediation="Review schema evolution compatibility, then regenerate with --write-snapshots.",
                )
            )

    return violations


def check_enum_parity(
    repo_root: Path,
    *,
    override_enum_values: Mapping[str, set[str]] | None = None,
) -> list[ConformanceViolation]:
    """Axis 2: Verify 1:1 parity across Python StrEnums, OpenAPI enums, AI JSON Schemas, and SQL 0010~0040."""
    violations: list[ConformanceViolation] = []
    migrations_dir = repo_root / "db" / "migrations"
    sql_cache: dict[str, str] = {}
    openapi_spec = generate_integrated_openapi_spec()
    openapi_schemas = openapi_spec.get("components", {}).get("schemas", {})
    ai_schemas = {fn: builder() for fn, builder in AI_SCHEMA_BUILDERS.items()}

    def _get_sql(filename: str) -> str:
        if filename not in sql_cache:
            sql_cache[filename] = (migrations_dir / filename).read_text(encoding="utf-8")
        return sql_cache[filename]

    # Cross-package duplicate enum equality checks
    if {e.value for e in ContentUnitType} != {e.value for e in CanonicalContentUnitType}:
        violations.append(
            ConformanceViolation(
                axis="enum_parity",
                code="DUPLICATE_PYTHON_ENUM_DRIFT",
                target="ContentUnitType",
                message="schema_contracts.enums.ContentUnitType differs from canonicalization.models.ContentUnitType.",
                authority=AUTHORITY_REFS["DATA_MODEL"],
            )
        )
    if {e.value for e in SourceProvider} != {e.value for e in OAuthProvider}:
        violations.append(
            ConformanceViolation(
                axis="enum_parity",
                code="DUPLICATE_PYTHON_ENUM_DRIFT",
                target="SourceProvider/OAuthProvider",
                message="schema_contracts.enums.SourceProvider differs from auth.oauth.OAuthProvider.",
                authority=AUTHORITY_REFS["DATA_MODEL"],
            )
        )
    if {e.value for e in FactType} != {e.value for e in ClaimAtomType}:
        violations.append(
            ConformanceViolation(
                axis="enum_parity",
                code="FACT_ATOM_TYPE_DRIFT",
                target="FactType/ClaimAtomType",
                message="FactType and ClaimAtomType must share the exact 6 official types.",
                authority=AUTHORITY_REFS["DATA_MODEL"],
            )
        )

    # Plan C forbidden raw-text key set parity between Python and SQL (0020 & 0040)
    for mig in ("0020_source_provenance_metadata.sql", "0040_sync_runs_jobs_outbox_audit.sql"):
        sql_keys = _extract_sql_forbidden_raw_keys(_get_sql(mig))
        if sql_keys != set(FORBIDDEN_RAW_TEXT_KEYS):
            violations.append(
                ConformanceViolation(
                    axis="enum_parity",
                    code="PLAN_C_FORBIDDEN_KEY_DRIFT",
                    target=f"db/migrations/{mig}",
                    message=(
                        f"SQL cm_jsonb_contains_forbidden_raw_key in {mig} drifted from "
                        f"Python FORBIDDEN_RAW_TEXT_KEYS: "
                        f"only_in_python={sorted(set(FORBIDDEN_RAW_TEXT_KEYS) - sql_keys)}, "
                        f"only_in_sql={sorted(sql_keys - set(FORBIDDEN_RAW_TEXT_KEYS))}."
                    ),
                    authority=AUTHORITY_REFS["PLAN_C"],
                )
            )

    # Check every registered binding
    for binding in ENUM_PARITY_MANIFEST:
        py_values = (
            set(override_enum_values[binding.name])
            if override_enum_values and binding.name in override_enum_values
            else {member.value for member in binding.enum_cls}
        )

        # 1. SQL CHECK constraints
        for mig_filename, marker in binding.sql_targets:
            try:
                sql_values = _extract_sql_constraint_literals(_get_sql(mig_filename), marker)
            except Exception as exc:  # noqa: BLE001
                violations.append(
                    ConformanceViolation(
                        axis="enum_parity",
                        code="SQL_CHECK_PARSE_FAILURE",
                        target=f"{mig_filename}:{marker}",
                        message=str(exc),
                        authority=binding.authority,
                    )
                )
                continue

            if py_values != sql_values:
                violations.append(
                    ConformanceViolation(
                        axis="enum_parity",
                        code="SQL_ENUM_DRIFT",
                        target=f"{binding.name} <-> {mig_filename}:{marker}",
                        message=(
                            f"Enum {binding.name} drifted between Python and SQL: "
                            f"only_in_python={sorted(py_values - sql_values)}, "
                            f"only_in_sql={sorted(sql_values - py_values)}."
                        ),
                        authority=binding.authority,
                        remediation="Align Python StrEnum and SQL CHECK constraint with the Notion Data Model.",
                    )
                )

        # 2. OpenAPI component schema enum
        if binding.openapi_schema_name is not None:
            oa_schema = openapi_schemas.get(binding.openapi_schema_name, {})
            oa_values = set(oa_schema.get("enum", []))
            if py_values != oa_values:
                violations.append(
                    ConformanceViolation(
                        axis="enum_parity",
                        code="OPENAPI_ENUM_DRIFT",
                        target=f"{binding.name} <-> OpenAPI:{binding.openapi_schema_name}",
                        message=(
                            f"Enum {binding.name} drifted between Python and OpenAPI: "
                            f"only_in_python={sorted(py_values - oa_values)}, "
                            f"only_in_openapi={sorted(oa_values - py_values)}."
                        ),
                        authority=binding.authority,
                    )
                )

        # 3. AI JSON Schema enum(s)
        for ai_file, key_path in binding.ai_schema_paths:
            ai_values = set(_lookup_nested_key(ai_schemas[ai_file], key_path))
            if py_values != ai_values:
                violations.append(
                    ConformanceViolation(
                        axis="enum_parity",
                        code="AI_SCHEMA_ENUM_DRIFT",
                        target=f"{binding.name} <-> {ai_file}:{'.'.join(key_path)}",
                        message=(
                            f"Enum {binding.name} drifted between Python and AI JSON Schema: "
                            f"only_in_python={sorted(py_values - ai_values)}, "
                            f"only_in_ai_schema={sorted(ai_values - py_values)}."
                        ),
                        authority=binding.authority,
                    )
                )

    return violations


def check_postgres_catalog_conformance(
    repo_root: Path,
    *,
    fresh_snapshot: dict[str, Any] | None = None,
    staged_snapshot: dict[str, Any] | None = None,
) -> list[ConformanceViolation]:
    """Axis 3: Verify Fresh (0010->0040) & Staged Upgrade PG18 catalogs against committed snapshot."""
    violations: list[ConformanceViolation] = []
    snapshot_path = (
        repo_root / "contracts" / "conformance" / "expected_db_catalog_snapshot.json"
    )
    if not snapshot_path.is_file():
        violations.append(
            ConformanceViolation(
                axis="postgres_catalog",
                code="DB_SNAPSHOT_MISSING",
                target="contracts/conformance/expected_db_catalog_snapshot.json",
                message="Expected PostgreSQL 18 catalog snapshot file is missing.",
                authority=AUTHORITY_REFS["T106A"],
                remediation="Run: cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres --write-snapshots",
            )
        )
        return violations

    expected_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    if fresh_snapshot is None or staged_snapshot is None:
        fresh_snapshot, staged_snapshot = run_fresh_and_staged_pg18_catalog_smoke(
            repo_root / "db" / "migrations"
        )

    # Fresh vs Staged upgrade parity (staged upgrade + idempotent re-run MUST yield identical catalog!)
    if fresh_snapshot != staged_snapshot:
        violations.append(
            ConformanceViolation(
                axis="postgres_catalog",
                code="FRESH_VS_STAGED_CATALOG_DIVERGENCE",
                target="db/migrations/0010..0040",
                message=(
                    "Fresh install (0010->0040) catalog diverges from Staged Upgrade + Re-Apply catalog."
                ),
                authority=AUTHORITY_REFS["T106A"],
                diff=_unified_diff(
                    render_db_catalog_snapshot_json(fresh_snapshot),
                    render_db_catalog_snapshot_json(staged_snapshot),
                    label="fresh_vs_staged_catalog.json",
                ),
            )
        )

    for label, actual_snapshot in (("fresh", fresh_snapshot), ("staged", staged_snapshot)):
        for drift in diff_db_catalog_snapshots(expected_snapshot, actual_snapshot):
            violations.append(
                ConformanceViolation(
                    axis="postgres_catalog",
                    code=drift.code,
                    target=f"[{label}] {drift.object_name}",
                    message=drift.message,
                    authority=drift.authority,
                )
            )

    return violations


def write_conformance_snapshots(
    repo_root: Path,
    *,
    with_postgres: bool = False,
) -> list[Path]:
    """Explicitly regenerate committed M1 contract & conformance snapshot artifacts.

    NEVER called during normal verification checks; only invoked when a developer explicitly
    runs ``run_conformance_ci.py --write-snapshots``.
    """
    written: list[Path] = []

    ai_dir = repo_root / "contracts" / "ai-schemas" / "v1"
    ai_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in generate_ai_schema_files().items():
        path = ai_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)

    openapi_path = repo_root / "contracts" / "openapi" / "career-memory-v1.openapi.json"
    openapi_path.parent.mkdir(parents=True, exist_ok=True)
    openapi_path.write_text(render_integrated_openapi_json(), encoding="utf-8")
    written.append(openapi_path)

    if with_postgres:
        fresh_snapshot, staged_snapshot = run_fresh_and_staged_pg18_catalog_smoke(
            repo_root / "db" / "migrations"
        )
        if fresh_snapshot != staged_snapshot:
            raise ConformanceError(
                [
                    ConformanceViolation(
                        axis="postgres_catalog",
                        code="FRESH_VS_STAGED_CATALOG_DIVERGENCE",
                        target="db/migrations/0010..0040",
                        message="Refusing to write DB snapshot: fresh and staged upgrade catalogs differ.",
                        authority=AUTHORITY_REFS["T106A"],
                    )
                ]
            )
        db_snapshot_path = (
            repo_root / "contracts" / "conformance" / "expected_db_catalog_snapshot.json"
        )
        db_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        db_snapshot_path.write_text(
            render_db_catalog_snapshot_json(fresh_snapshot), encoding="utf-8"
        )
        written.append(db_snapshot_path)

    return written


def run_all_conformance_checks(
    repo_root: Path | None = None,
    *,
    with_postgres: bool = False,
) -> list[ConformanceViolation]:
    """Execute all configured T-106 / T-106A conformance axes and return violations."""
    root = default_repo_root() if repo_root is None else repo_root
    violations: list[ConformanceViolation] = []
    violations.extend(check_artifact_drift(root))
    violations.extend(check_enum_parity(root))
    if with_postgres:
        violations.extend(check_postgres_catalog_conformance(root))
    return violations


__all__ = [
    "AUTHORITY_REFS",
    "ConformanceError",
    "ConformanceViolation",
    "ENUM_PARITY_MANIFEST",
    "EnumParityBinding",
    "M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1",
    "check_artifact_drift",
    "check_enum_parity",
    "check_postgres_catalog_conformance",
    "compute_git_blob_sha1",
    "default_repo_root",
    "run_all_conformance_checks",
    "validate_openapi_invariants",
    "write_conformance_snapshots",
]
