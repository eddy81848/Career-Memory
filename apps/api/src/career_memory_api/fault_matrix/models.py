"""Typed models and evidence schemas for M5 Cross-Subsystem Fault & Race Matrix (`T-504` / `T-504A`)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

T504A_EVIDENCE_SCHEMA_VERSION = "t504a-fault-matrix-evidence-v1"


class FaultMatrixScenarioId(StrEnum):
    """Canonical 12 cross-subsystem deterministic fault & race scenarios."""

    XFLT_01 = "XFLT-01"
    XFLT_02 = "XFLT-02"
    XFLT_03 = "XFLT-03"
    XFLT_04 = "XFLT-04"
    XFLT_05 = "XFLT-05"
    XFLT_06 = "XFLT-06"
    XFLT_07 = "XFLT-07"
    XFLT_08 = "XFLT-08"
    XFLT_09 = "XFLT-09"
    XFLT_10 = "XFLT-10"
    XFLT_11 = "XFLT-11"
    XFLT_12 = "XFLT-12"


class CrossSubsystemFaultPoint(StrEnum):
    """Named deterministic fault & barrier injection points (ZERO time.sleep race timing)."""

    JOB_POISON_MAX_ATTEMPTS = "JOB_POISON_MAX_ATTEMPTS"
    OUTBOX_PUBLISH_POISON = "OUTBOX_PUBLISH_POISON"
    CONCURRENT_JOB_REPLAY_BARRIER = "CONCURRENT_JOB_REPLAY_BARRIER"
    CLAIM_APPROVAL_BEFORE_LOCK_BARRIER = "CLAIM_APPROVAL_BEFORE_LOCK_BARRIER"
    EXPORT_BEFORE_PUBLISH_BARRIER = "EXPORT_BEFORE_PUBLISH_BARRIER"
    EXPORT_DOWNLOAD_REVISION_DRIFT = "EXPORT_DOWNLOAD_REVISION_DRIFT"
    FOUR_WORKER_PRE_COMMIT_DELETION_BARRIER = "FOUR_WORKER_PRE_COMMIT_DELETION_BARRIER"
    DELETION_COMPONENT_FTS_FAILURE = "DELETION_COMPONENT_FTS_FAILURE"
    DELETION_COMPONENT_DOMAIN_FAILURE = "DELETION_COMPONENT_DOMAIN_FAILURE"
    SEARCH_INDEX_LAG_SKIP_UPDATE = "SEARCH_INDEX_LAG_SKIP_UPDATE"
    SEARCH_CURSOR_VERSION_AND_SCOPE_DRIFT = "SEARCH_CURSOR_VERSION_AND_SCOPE_DRIFT"
    HOSTILE_PAYLOAD_AND_SSRF_REDIRECT = "HOSTILE_PAYLOAD_AND_SSRF_REDIRECT"
    PROVIDER_CIRCUIT_BREAKER_AND_AMBIGUOUS_TIMEOUT = "PROVIDER_CIRCUIT_BREAKER_AND_AMBIGUOUS_TIMEOUT"
    QUOTA_CUTOFF_95_PERCENT = "QUOTA_CUTOFF_95_PERCENT"
    EDGE_ORIGIN_BYPASS_AND_MANUAL_PAUSE = "EDGE_ORIGIN_BYPASS_AND_MANUAL_PAUSE"
    REFETCH_GATE_MATRIX_AND_BUDGET_CAP = "REFETCH_GATE_MATRIX_AND_BUDGET_CAP"
    MID_LIFECYCLE_ENCRYPTED_BACKUP_RESTORE = "MID_LIFECYCLE_ENCRYPTED_BACKUP_RESTORE"
    MULTI_STAGE_TRANSIENT_RECOVERY_CONVERGENCE = "MULTI_STAGE_TRANSIENT_RECOVERY_CONVERGENCE"


class PlanCAuditSummary(BaseModel):
    """46-table PostgreSQL 18 Plan C, secret-leak, and canary verification summary."""

    model_config = ConfigDict(extra="forbid")

    audited_table_count: int = Field(ge=1)
    audited_jsonb_column_count: int = Field(ge=0)
    raw_text_violations: int = Field(ge=0)
    secret_key_violations: int = Field(ge=0)
    canary_string_hits: int = Field(ge=0)
    log_canary_hits: int = Field(ge=0)
    export_canary_hits: int = Field(ge=0)
    passed: bool


class FaultMatrixScenarioResult(BaseModel):
    """Deterministic per-scenario result emitted by the T-504A fault/race runner."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: FaultMatrixScenarioId
    title: str
    requirement_ids: list[str]
    subsystems: list[str]
    fault_points: list[CrossSubsystemFaultPoint]
    barrier_synchronized: bool
    passed: bool
    assertions_checked: int = Field(ge=1)
    assertions_failed: int = Field(ge=0)
    observed_metrics: dict[str, Any]
    plan_c_audit: PlanCAuditSummary
    notes: list[str] = Field(default_factory=list)


class T504AEvidenceBundle(BaseModel):
    """Redacted, deterministic cross-subsystem fault & race matrix evidence bundle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = T504A_EVIDENCE_SCHEMA_VERSION
    task_ids: list[str] = Field(default_factory=lambda: ["T-504", "T-504A"])
    issue_number: int = 33
    postgres_major_version: int = 18
    schema_migrations: list[str]
    openapi_import_v1_blob_sha: str
    external_network_calls: int = 0
    spend_krw: int = 0
    sleep_based_race_waits: int = 0
    rls_catalog_verified: dict[str, Any]
    scenarios: list[FaultMatrixScenarioResult]
    summary: dict[str, Any]
    deterministic_matrix_sha256: str


def compute_canonical_sha256(payload: Any) -> str:
    """Compute a deterministic `sha256:<hex>` digest over canonical sorted JSON."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def normalize_t504a_bundle_for_drift_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Return canonical representation suitable for zero-drift `--verify` comparison."""
    return json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=True))


__all__ = [
    "T504A_EVIDENCE_SCHEMA_VERSION",
    "CrossSubsystemFaultPoint",
    "FaultMatrixScenarioId",
    "FaultMatrixScenarioResult",
    "PlanCAuditSummary",
    "T504AEvidenceBundle",
    "compute_canonical_sha256",
    "normalize_t504a_bundle_for_drift_check",
]
