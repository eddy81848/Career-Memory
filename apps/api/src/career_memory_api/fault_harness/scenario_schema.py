"""T-005A Versioned scenario & expected-snapshot schemas and strict validators for B-004."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

SCENARIO_SCHEMA_VERSION = "cm-b004-scenario-v1"
EXPECTED_SNAPSHOT_SCHEMA_VERSION = "cm-b004-expected-v1"
EVIDENCE_BUNDLE_SCHEMA_VERSION = "cm-b004-evidence-bundle-v1"

DEFAULT_OBSERVED_AT_ISO = "2026-11-27T12:00:00Z"

_SCENARIO_ID_RE = re.compile(r"^S(0[1-9]|1[0-6])$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_CANARY_MARKER_RE = re.compile(r"^CANARY_B004_[A-Z0-9_]+$")

# Reject real credentials / private secrets in scenario specs and snapshots.
_FORBIDDEN_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai_or_llm_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_header", re.compile(r"(?i)\bBearer\s+(?!CANARY_B004_)[A-Za-z0-9._-]{16,}")),
    ("real_email", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!local\.test\b|example\.invalid\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)


class FaultInjectionPoint(StrEnum):
    """Explicit T-005B failure & concurrency injection points."""

    NONE = "NONE"
    BEFORE_PAGE_TX = "BEFORE_PAGE_TX"
    INSIDE_PAGE_TX_BEFORE_COMMIT = "INSIDE_PAGE_TX_BEFORE_COMMIT"
    AFTER_PAGE_COMMIT_BEFORE_ACK = "AFTER_PAGE_COMMIT_BEFORE_ACK"
    BEFORE_FINALIZE_SUCCESS = "BEFORE_FINALIZE_SUCCESS"
    AFTER_FINALIZE_BEFORE_DERIVED_UNITS = "AFTER_FINALIZE_BEFORE_DERIVED_UNITS"
    CONCURRENT_REQUEST_AND_WORKER_RACE = "CONCURRENT_REQUEST_AND_WORKER_RACE"


class ScenarioValidationError(ValueError):
    """Raised when a B-004 scenario spec or expected snapshot violates T-005A rules."""


@dataclass(frozen=True, slots=True)
class CanarySetSpec:
    """Unique synthetic canary tokens planted per scenario for REF-04/06 0-leak auditing."""

    raw_pr_body: str
    raw_commit_msg: str
    raw_review_body: str
    oauth_token: str

    @classmethod
    def for_scenario(cls, scenario_id: str) -> CanarySetSpec:
        sid = scenario_id.strip().upper()
        return cls(
            raw_pr_body=f"CANARY_B004_RAW_PR_BODY_{sid}_9F42A1",
            raw_commit_msg=f"CANARY_B004_RAW_COMMIT_MSG_{sid}_7B31E8",
            raw_review_body=f"CANARY_B004_RAW_REVIEW_BODY_{sid}_5C18D2",
            oauth_token=f"CANARY_B004_OAUTH_TOKEN_{sid}_3E84C9",
        )

    def all_tokens(self) -> tuple[str, ...]:
        return (
            self.raw_pr_body,
            self.raw_commit_msg,
            self.raw_review_body,
            self.oauth_token,
        )


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """Declarative, versioned T-005A fault-harness scenario specification."""

    scenario_id: str
    title: str
    requirements: tuple[str, ...]
    default_branch: str
    observed_at_iso: str
    injection_points: tuple[FaultInjectionPoint, ...]
    description: str
    baseline_equivalence_target: str | None = None
    review_comment_drift_note: str | None = None
    schema_version: str = SCENARIO_SCHEMA_VERSION

    @property
    def canaries(self) -> CanarySetSpec:
        return CanarySetSpec.for_scenario(self.scenario_id)

    def to_metadata_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "title": self.title,
            "requirements": list(self.requirements),
            "default_branch": self.default_branch,
            "observed_at_iso": self.observed_at_iso,
            "injection_points": [p.value for p in self.injection_points],
            "baseline_equivalence_target": self.baseline_equivalence_target,
            "description": self.description,
            "review_comment_drift_note": self.review_comment_drift_note,
        }


def _scan_for_forbidden_secrets(value: Any, *, path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(value, str):
        for label, pattern in _FORBIDDEN_SECRET_PATTERNS:
            if pattern.search(value):
                errors.append(f"{path}: contains forbidden real-secret/private pattern ({label})")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            errors.extend(_scan_for_forbidden_secrets(str(k), path=f"{path}.{k}"))
            errors.extend(_scan_for_forbidden_secrets(v, path=f"{path}.{k}"))
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            errors.extend(_scan_for_forbidden_secrets(item, path=f"{path}[{idx}]"))
    return errors


def _validate_iso_utc(timestamp_iso: str, *, field_name: str) -> None:
    if not isinstance(timestamp_iso, str) or not timestamp_iso.endswith("Z"):
        raise ScenarioValidationError(
            f"{field_name} must be a deterministic UTC ISO-8601 string ending with 'Z'."
        )
    try:
        parsed = datetime.fromisoformat(timestamp_iso[:-1] + "+00:00")
    except ValueError as exc:
        raise ScenarioValidationError(f"{field_name} is not a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ScenarioValidationError(f"{field_name} must include timezone information.")


def validate_scenario_spec(spec: ScenarioSpec) -> None:
    """Strictly validate a B-004 ScenarioSpec according to T-005A rules."""
    if spec.schema_version != SCENARIO_SCHEMA_VERSION:
        raise ScenarioValidationError(
            f"Unsupported scenario schema_version={spec.schema_version!r}; "
            f"expected {SCENARIO_SCHEMA_VERSION!r}."
        )
    if not _SCENARIO_ID_RE.match(spec.scenario_id):
        raise ScenarioValidationError(
            f"Invalid scenario_id={spec.scenario_id!r}; must match S01..S16."
        )
    if not spec.title.strip() or not spec.description.strip():
        raise ScenarioValidationError(f"{spec.scenario_id}: title and description must be non-empty.")
    if not spec.requirements:
        raise ScenarioValidationError(f"{spec.scenario_id}: must list at least one requirement ID.")
    if not _BRANCH_NAME_RE.match(spec.default_branch):
        raise ScenarioValidationError(
            f"{spec.scenario_id}: invalid default_branch={spec.default_branch!r}."
        )
    _validate_iso_utc(spec.observed_at_iso, field_name=f"{spec.scenario_id}.observed_at_iso")

    if not spec.injection_points:
        raise ScenarioValidationError(
            f"{spec.scenario_id}: injection_points must be non-empty (use FaultInjectionPoint.NONE if none)."
        )
    for point in spec.injection_points:
        if not isinstance(point, FaultInjectionPoint):
            raise ScenarioValidationError(
                f"{spec.scenario_id}: invalid injection point {point!r}."
            )

    if spec.baseline_equivalence_target is not None:
        if not _SCENARIO_ID_RE.match(spec.baseline_equivalence_target):
            raise ScenarioValidationError(
                f"{spec.scenario_id}: invalid baseline_equivalence_target={spec.baseline_equivalence_target!r}."
            )

    for token in spec.canaries.all_tokens():
        if not _CANARY_MARKER_RE.match(token):
            raise ScenarioValidationError(
                f"{spec.scenario_id}: canary token {token!r} does not match synthetic CANARY_B004_* format."
            )

    secret_errors = _scan_for_forbidden_secrets(
        spec.to_metadata_dict(), path=f"scenario[{spec.scenario_id}]"
    )
    if secret_errors:
        raise ScenarioValidationError("; ".join(secret_errors))


def validate_expected_snapshot(payload: Mapping[str, Any]) -> None:
    """Strictly validate a static versioned expected snapshot JSON payload (T-005A/T-005C)."""
    if not isinstance(payload, Mapping):
        raise ScenarioValidationError("Expected snapshot must be a JSON object.")

    schema_ver = payload.get("schema_version")
    if schema_ver != EXPECTED_SNAPSHOT_SCHEMA_VERSION:
        raise ScenarioValidationError(
            f"Unsupported expected snapshot schema_version={schema_ver!r}; "
            f"expected {EXPECTED_SNAPSHOT_SCHEMA_VERSION!r}."
        )

    scenario_id = payload.get("scenario_id")
    if not isinstance(scenario_id, str) or not _SCENARIO_ID_RE.match(scenario_id):
        raise ScenarioValidationError(f"Expected snapshot has invalid scenario_id={scenario_id!r}.")

    observed_at = payload.get("observed_at_iso")
    if not isinstance(observed_at, str):
        raise ScenarioValidationError(f"{scenario_id}: expected snapshot missing observed_at_iso.")
    _validate_iso_utc(observed_at, field_name=f"{scenario_id}.observed_at_iso")

    default_branch = payload.get("default_branch")
    if not isinstance(default_branch, str) or not _BRANCH_NAME_RE.match(default_branch):
        raise ScenarioValidationError(
            f"{scenario_id}: expected snapshot invalid default_branch={default_branch!r}."
        )

    snapshot = payload.get("logical_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ScenarioValidationError(f"{scenario_id}: expected snapshot missing logical_snapshot object.")

    required_sections = (
        "source_objects",
        "source_object_revisions",
        "sync_commit_pr_associations",
        "sync_checkpoints",
        "sync_run_page_observations",
        "sync_item_outcomes",
        "sync_runs",
        "outbox_summary",
        "work_events",
        "event_memberships_count",
        "content_units",
        "resource_and_account_policy",
        "gate_checks",
    )
    for section in required_sections:
        if section not in snapshot:
            raise ScenarioValidationError(
                f"{scenario_id}: expected logical_snapshot missing required section {section!r}."
            )

    # Ensure zero canary tokens or real secrets appear anywhere in static expected snapshots!
    raw_text = str(payload)
    if "CANARY_B004_" in raw_text:
        raise ScenarioValidationError(
            f"{scenario_id}: expected snapshot must NEVER contain raw canary strings."
        )
    secret_errors = _scan_for_forbidden_secrets(payload, path=f"expected_snapshot[{scenario_id}]")
    if secret_errors:
        raise ScenarioValidationError("; ".join(secret_errors))


__all__ = [
    "CanarySetSpec",
    "DEFAULT_OBSERVED_AT_ISO",
    "EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "EXPECTED_SNAPSHOT_SCHEMA_VERSION",
    "FaultInjectionPoint",
    "SCENARIO_SCHEMA_VERSION",
    "ScenarioSpec",
    "ScenarioValidationError",
    "validate_expected_snapshot",
    "validate_scenario_spec",
]
