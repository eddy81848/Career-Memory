"""Typed models for M5 Closed Pilot & 3-Tier Go/No-Go Release Gate Evaluation (`T-506`)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

T506_RELEASE_GATE_SCHEMA_VERSION = "t506-release-gate-v1"


class ReleaseGateTier(StrEnum):
    """Strict 3-tier release gate separation (ADR-018, ADR-019, ADR-020)."""

    LOCAL_DETERMINISTIC_MVP_GATE = "LOCAL_DETERMINISTIC_MVP_GATE"
    PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE = "PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE"
    LIVE_SUBSCRIPTION_AI_PILOT_GATE = "LIVE_SUBSCRIPTION_AI_PILOT_GATE"


class ReleaseGateDecision(StrEnum):
    """Binary release gate decision."""

    GO = "GO"
    NO_GO = "NO-GO"


class ReleaseGateStatusCode(StrEnum):
    """Explicit machine-readable status code explaining GO or NO-GO hold."""

    PASSED = "PASSED"
    HELD_PENDING_OD013_OD015 = "HELD_PENDING_OD013_OD015"
    HELD_PENDING_OD002_OD014 = "HELD_PENDING_OD002_OD014"
    FAILED = "FAILED"


class BundleVerificationResult(BaseModel):
    """Programmatic verification outcome for a single committed evidence bundle."""

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    relative_path: str
    present: bool
    verified: bool
    file_sha256: str | None = None
    checks_passed: int = Field(ge=0)
    checks_total: int = Field(ge=0)
    findings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class InvariantVerificationItem(BaseModel):
    """Verification outcome for a required Career Memory release invariant group."""

    model_config = ConfigDict(extra="forbid")

    invariant_id: str
    category: str
    passed: bool
    evidence_sources: list[str]
    summary: str


class TierGateEvaluation(BaseModel):
    """Evaluation outcome for one of the 3 release gate tiers."""

    model_config = ConfigDict(extra="forbid")

    tier: ReleaseGateTier
    decision: ReleaseGateDecision
    status_code: ReleaseGateStatusCode
    blocking_open_decisions: list[str] = Field(default_factory=list)
    rationale: str
    required_operator_actions: list[str] = Field(default_factory=list)


class T506ReleaseGateBundle(BaseModel):
    """Canonical committed evidence bundle for `T-506` Closed Pilot & Go/No-Go Evaluation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = T506_RELEASE_GATE_SCHEMA_VERSION
    task_id: str = "T-506"
    issue_number: int = 33
    openapi_import_v1_blob_sha: str
    openapi_pin_verified: bool
    external_network_calls: int = 0
    spend_krw: int = 0
    evidence_bundles: list[BundleVerificationResult]
    invariants: list[InvariantVerificationItem]
    release_gates: list[TierGateEvaluation]
    summary: dict[str, Any]
    deterministic_gate_sha256: str


def compute_gate_sha256(payload: Any) -> str:
    """Compute canonical `sha256:<hex>` digest over sorted JSON."""
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def normalize_t506_gate_for_drift_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Return canonical representation for zero-drift `--verify` checks."""
    return json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=True))


__all__ = [
    "T506_RELEASE_GATE_SCHEMA_VERSION",
    "BundleVerificationResult",
    "InvariantVerificationItem",
    "ReleaseGateDecision",
    "ReleaseGateStatusCode",
    "ReleaseGateTier",
    "T506ReleaseGateBundle",
    "TierGateEvaluation",
    "compute_gate_sha256",
    "normalize_t506_gate_for_drift_check",
]
