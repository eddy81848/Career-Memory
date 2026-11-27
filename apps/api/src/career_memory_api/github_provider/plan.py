"""Frozen initial import plan (FrozenImportPlan) and CompletionGateEvaluator (M2-T202)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from career_memory_api.canonicalization import CANONICALIZATION_VERSION
from career_memory_api.github_provider.models import (
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_MAX_ITEM_BYTES,
    DEFAULT_MAX_OBJECTS,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_PER_PAGE,
    GITHUB_CONNECTOR_VERSION,
    MAX_OBJECTS_HARD_CAP,
    OPTIONAL_CONTEXT_STREAMS,
    REQUIRED_IMPORT_STREAMS,
    GitHubRepositorySnapshot,
    GitHubStreamKind,
    ImportPlanValidationError,
    MergedPullRequestCue,
    RepositoryPolicyViolationError,
    format_utc_iso,
    normalize_commit_sha,
    parse_github_iso_timestamp,
    safe_json_dumps,
)
from career_memory_api.schema_contracts.enums import ResourceVisibility
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.sync_runs.models import (
    ProcessingStatus,
    StreamProgress,
    SyncRunMode,
    SyncRunObservation,
    SyncRunStatus,
)


def _ensure_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ImportPlanValidationError(f"{field_name} must be a datetime.")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_backfill_from(
    raw: date | datetime | str | None,
    *,
    cutoff_at: datetime,
) -> datetime:
    if raw is None:
        return cutoff_at - timedelta(days=DEFAULT_BACKFILL_DAYS)
    if isinstance(raw, datetime):
        return _ensure_utc_datetime(raw, field_name="backfill_from")
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, 0, 0, 0, tzinfo=UTC)
    if isinstance(raw, str):
        cleaned = raw.strip()
        if len(cleaned) == 10:
            try:
                parsed_date = date.fromisoformat(cleaned)
            except ValueError as exc:
                raise ImportPlanValidationError(
                    "backfill_from must be a valid ISO YYYY-MM-DD date or ISO-8601 datetime."
                ) from exc
            if parsed_date.isoformat() != cleaned:
                raise ImportPlanValidationError("backfill_from must use canonical ISO YYYY-MM-DD.")
            return datetime(
                parsed_date.year, parsed_date.month, parsed_date.day, 0, 0, 0, tzinfo=UTC
            )
        parsed_dt = parse_github_iso_timestamp(cleaned)
        if parsed_dt is not None:
            return parsed_dt
    raise ImportPlanValidationError(
        "backfill_from must be a YYYY-MM-DD date string, ISO-8601 datetime, date, or None."
    )


@dataclass(frozen=True, slots=True)
class ImportPlanLimits:
    """Hard resource bounds frozen into a single INITIAL_BACKFILL plan."""

    max_objects: int = DEFAULT_MAX_OBJECTS
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES
    per_page: int = DEFAULT_PER_PAGE

    def __post_init__(self) -> None:
        for attr in ("max_objects", "max_requests", "max_total_bytes", "max_item_bytes", "per_page"):
            val = getattr(self, attr)
            if isinstance(val, bool) or not isinstance(val, int):
                raise ImportPlanValidationError(f"{attr} must be an integer.")
        if self.max_objects < 1 or self.max_objects > MAX_OBJECTS_HARD_CAP:
            raise ImportPlanValidationError(
                f"max_objects must be between 1 and {MAX_OBJECTS_HARD_CAP} (got {self.max_objects})."
            )
        if self.max_requests < 1:
            raise ImportPlanValidationError("max_requests must be >= 1.")
        if self.max_total_bytes < 1:
            raise ImportPlanValidationError("max_total_bytes must be >= 1.")
        if self.max_item_bytes < 1 or self.max_item_bytes > self.max_total_bytes:
            raise ImportPlanValidationError("max_item_bytes must be between 1 and max_total_bytes.")
        if self.per_page < 1 or self.per_page > 100:
            raise ImportPlanValidationError("per_page must be between 1 and 100.")

    @classmethod
    def from_request_limits(cls, limits: Mapping[str, int] | None) -> ImportPlanLimits:
        """Build validated ImportPlanLimits from optional API ``limits`` dict."""
        if limits is None:
            return cls()
        if not isinstance(limits, Mapping):
            raise ImportPlanValidationError("limits must be a mapping.")
        allowed_keys = {"max_objects", "max_requests", "max_total_bytes", "max_item_bytes", "per_page"}
        unknown = sorted(str(k) for k in limits.keys() if str(k) not in allowed_keys)
        if unknown:
            raise ImportPlanValidationError(f"Unsupported limit field(s): {', '.join(unknown)}")
        return cls(
            max_objects=limits.get("max_objects", DEFAULT_MAX_OBJECTS),
            max_requests=limits.get("max_requests", DEFAULT_MAX_REQUESTS),
            max_total_bytes=limits.get("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES),
            max_item_bytes=limits.get("max_item_bytes", DEFAULT_MAX_ITEM_BYTES),
            per_page=limits.get("per_page", DEFAULT_PER_PAGE),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_objects": self.max_objects,
            "max_requests": self.max_requests,
            "max_total_bytes": self.max_total_bytes,
            "max_item_bytes": self.max_item_bytes,
            "per_page": self.per_page,
        }


@dataclass(frozen=True, slots=True)
class FrozenImportPlan:
    """Immutable initial import plan anchored to provider default branch and start HEAD SHA."""

    plan_id: str
    mode: SyncRunMode
    source_resource_id: str
    provider_resource_id: str
    repository_full_name: str
    canonical_url: str
    visibility: ResourceVisibility
    default_branch: str
    start_head_sha: str
    backfill_from: datetime
    cutoff_at: datetime
    started_observed_at: datetime
    limits: ImportPlanLimits = field(default_factory=ImportPlanLimits)
    required_streams: tuple[GitHubStreamKind, ...] = REQUIRED_IMPORT_STREAMS
    optional_streams: tuple[GitHubStreamKind, ...] = OPTIONAL_CONTEXT_STREAMS
    connector_version: str = GITHUB_CONNECTOR_VERSION
    hash_policy_version: str = CANONICALIZATION_VERSION

    def __post_init__(self) -> None:
        if self.mode is not SyncRunMode.INITIAL_BACKFILL:
            raise ImportPlanValidationError("FrozenImportPlan only supports INITIAL_BACKFILL mode.")
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ImportPlanValidationError("plan_id must be a non-empty string.")
        if not isinstance(self.source_resource_id, str) or not self.source_resource_id.strip():
            raise ImportPlanValidationError("source_resource_id must be a non-empty string.")
        if self.visibility is not ResourceVisibility.PUBLIC:
            raise RepositoryPolicyViolationError(
                f"MVP initial import requires PUBLIC repository visibility (got {self.visibility.value})."
            )
        if not isinstance(self.default_branch, str) or not self.default_branch.strip():
            raise ImportPlanValidationError(
                "default_branch must be resolved from provider metadata (never empty or hardcoded)."
            )
        normalized_sha = normalize_commit_sha(self.start_head_sha, "start_head_sha")
        object.__setattr__(self, "start_head_sha", normalized_sha)

        bf = _ensure_utc_datetime(self.backfill_from, field_name="backfill_from")
        cutoff = _ensure_utc_datetime(self.cutoff_at, field_name="cutoff_at")
        started = _ensure_utc_datetime(self.started_observed_at, field_name="started_observed_at")
        if bf > cutoff:
            raise ImportPlanValidationError("backfill_from must be <= cutoff_at.")
        object.__setattr__(self, "backfill_from", bf)
        object.__setattr__(self, "cutoff_at", cutoff)
        object.__setattr__(self, "started_observed_at", started)

    def is_timestamp_in_window(self, ts: datetime) -> bool:
        """Return True iff UTC timestamp falls within inclusive [backfill_from, cutoff_at]."""
        normalized = _ensure_utc_datetime(ts, field_name="ts")
        return self.backfill_from <= normalized <= self.cutoff_at

    def to_plan_spec_dict(self) -> dict[str, Any]:
        """Serialize to a non-raw JSONB dict compatible with ``sync_runs.plan_spec``."""
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "mode": self.mode.value,
            "source_resource_id": self.source_resource_id,
            "provider_resource_id": self.provider_resource_id,
            "repository_full_name": self.repository_full_name,
            "canonical_url": self.canonical_url,
            "visibility": self.visibility.value,
            "default_branch": self.default_branch,
            "start_head_sha": self.start_head_sha,
            "backfill_from": format_utc_iso(self.backfill_from),
            "cutoff_at": format_utc_iso(self.cutoff_at),
            "started_observed_at": format_utc_iso(self.started_observed_at),
            "limits": self.limits.to_dict(),
            "required_streams": [s.value for s in self.required_streams],
            "optional_streams": [s.value for s in self.optional_streams],
            "connector_version": self.connector_version,
            "hash_policy_version": self.hash_policy_version,
        }
        assert_no_forbidden_raw_keys(payload, field_name="plan_spec")
        return payload

    def plan_fingerprint(self) -> str:
        """Return deterministic SHA-256 hex fingerprint over the canonical plan spec."""
        return hashlib.sha256(safe_json_dumps(self.to_plan_spec_dict()).encode("utf-8")).hexdigest()


def build_frozen_import_plan(
    *,
    source_resource_id: str,
    repo_snapshot: GitHubRepositorySnapshot,
    cutoff_at: datetime,
    backfill_from: date | datetime | str | None = None,
    limits: ImportPlanLimits | Mapping[str, int] | None = None,
    plan_id: str | None = None,
) -> FrozenImportPlan:
    """Create a validated ``FrozenImportPlan`` from an observed GitHub repository snapshot."""
    if repo_snapshot.visibility is not ResourceVisibility.PUBLIC:
        raise RepositoryPolicyViolationError(
            f"Cannot create initial import plan for non-public repository ({repo_snapshot.visibility.value})."
        )

    cutoff_utc = _ensure_utc_datetime(cutoff_at, field_name="cutoff_at")
    backfill_utc = _parse_backfill_from(backfill_from, cutoff_at=cutoff_utc)
    if backfill_utc > cutoff_utc:
        raise ImportPlanValidationError("backfill_from must be <= cutoff_at.")

    resolved_limits = (
        limits
        if isinstance(limits, ImportPlanLimits)
        else ImportPlanLimits.from_request_limits(limits)
    )
    resolved_plan_id = plan_id.strip() if isinstance(plan_id, str) and plan_id.strip() else str(uuid.uuid4())

    return FrozenImportPlan(
        plan_id=resolved_plan_id,
        mode=SyncRunMode.INITIAL_BACKFILL,
        source_resource_id=source_resource_id.strip(),
        provider_resource_id=repo_snapshot.provider_resource_id,
        repository_full_name=repo_snapshot.full_name,
        canonical_url=repo_snapshot.canonical_url,
        visibility=repo_snapshot.visibility,
        default_branch=repo_snapshot.default_branch,
        start_head_sha=repo_snapshot.default_branch_head_sha,
        backfill_from=backfill_utc,
        cutoff_at=cutoff_utc,
        started_observed_at=repo_snapshot.observed_at,
        limits=resolved_limits,
    )


@dataclass(frozen=True, slots=True)
class CompletionGateResult:
    """Deterministic completion-gate outcome aligned with 0040 sync_runs CHECK constraints."""

    passed: bool
    status: SyncRunStatus
    import_ready: bool
    source_drift: bool
    retry_allowed: bool
    failure_reason_code: str | None
    started_observed_at: datetime
    ended_observed_at: datetime
    drift_reasons: tuple[str, ...] = ()
    drift_details: dict[str, Any] = field(default_factory=dict)
    processed_range: str | None = None
    remaining_range: str | None = None
    has_safe_checkpoint: bool = False

    def to_sync_run_observation(
        self,
        *,
        streams: tuple[StreamProgress, ...] | None = None,
    ) -> SyncRunObservation:
        """Convert completion gate verdict directly into M1 ``SyncRunObservation``."""
        return SyncRunObservation(
            status=self.status,
            failure_reason=self.failure_reason_code,
            streams=streams,
            import_ready=self.import_ready,
            processing_status=ProcessingStatus.NOT_STARTED,
            observed_at=self.ended_observed_at,
            source_drift=self.source_drift,
            processed_range=self.processed_range,
            remaining_range=self.remaining_range,
            retry_allowed=self.retry_allowed,
            has_safe_checkpoint=self.has_safe_checkpoint,
        )


class CompletionGateEvaluator:
    """Evaluates end-of-import snapshot consistency against a ``FrozenImportPlan``."""

    def verify_completion_snapshot(
        self,
        *,
        plan: FrozenImportPlan,
        end_repo_snapshot: GitHubRepositorySnapshot,
        first_pass_merged_prs: Iterable[MergedPullRequestCue],
        second_pass_merged_prs: Iterable[MergedPullRequestCue],
        completed_required_streams: Iterable[GitHubStreamKind] | None = None,
        limit_exceeded: bool = False,
        processed_range: str | None = None,
        remaining_range: str | None = None,
        transient_error_count: int = 0,
        has_safe_checkpoint: bool = False,
    ) -> CompletionGateResult:
        """Deterministically evaluate whether the import reached IMPORT_READY or drifted/failed."""
        ended_at = end_repo_snapshot.observed_at

        # 1. Identity & visibility verification (fail closed on repository mismatch or non-public flip)
        if end_repo_snapshot.provider_resource_id != plan.provider_resource_id:
            return CompletionGateResult(
                passed=False,
                status=SyncRunStatus.FAILED,
                import_ready=False,
                source_drift=False,
                retry_allowed=False,
                failure_reason_code="REPOSITORY_IDENTITY_MISMATCH",
                started_observed_at=plan.started_observed_at,
                ended_observed_at=ended_at,
                processed_range=processed_range,
                remaining_range=remaining_range,
                has_safe_checkpoint=False,
            )

        if end_repo_snapshot.visibility is not ResourceVisibility.PUBLIC:
            return CompletionGateResult(
                passed=False,
                status=SyncRunStatus.FAILED,
                import_ready=False,
                source_drift=False,
                retry_allowed=False,
                failure_reason_code="REPOSITORY_VISIBILITY_CHANGED",
                started_observed_at=plan.started_observed_at,
                ended_observed_at=ended_at,
                processed_range=processed_range,
                remaining_range=remaining_range,
                has_safe_checkpoint=False,
            )

        # 2. Hard import limit check -> PARTIAL_WITH_LIMIT
        if limit_exceeded:
            safe_remaining = (
                remaining_range.strip()
                if isinstance(remaining_range, str) and remaining_range.strip()
                else f"objects:>{plan.limits.max_objects}"
            )
            return CompletionGateResult(
                passed=False,
                status=SyncRunStatus.PARTIAL_WITH_LIMIT,
                import_ready=False,
                source_drift=False,
                retry_allowed=False,
                failure_reason_code="IMPORT_LIMIT_EXCEEDED",
                started_observed_at=plan.started_observed_at,
                ended_observed_at=ended_at,
                processed_range=processed_range,
                remaining_range=safe_remaining,
                has_safe_checkpoint=has_safe_checkpoint,
            )

        # 3. Frozen snapshot drift checks: default branch, HEAD SHA, and 2nd-pass PR set comparison
        drift_reasons: list[str] = []
        drift_details: dict[str, Any] = {}

        if end_repo_snapshot.default_branch != plan.default_branch:
            drift_reasons.append("DEFAULT_BRANCH_CHANGED")
            drift_details["expected_default_branch"] = plan.default_branch
            drift_details["observed_default_branch"] = end_repo_snapshot.default_branch

        if end_repo_snapshot.default_branch_head_sha.lower() != plan.start_head_sha.lower():
            drift_reasons.append("DEFAULT_BRANCH_HEAD_CHANGED")
            drift_details["expected_head_sha"] = plan.start_head_sha
            drift_details["observed_head_sha"] = end_repo_snapshot.default_branch_head_sha

        first_by_pr = {
            cue.pr_number: cue.comparison_tuple()
            for cue in first_pass_merged_prs
            if plan.is_timestamp_in_window(cue.merged_at)
        }
        second_by_pr = {
            cue.pr_number: cue.comparison_tuple()
            for cue in second_pass_merged_prs
            if plan.is_timestamp_in_window(cue.merged_at)
        }

        if first_by_pr != second_by_pr:
            first_nums = set(first_by_pr.keys())
            second_nums = set(second_by_pr.keys())
            added = sorted(second_nums - first_nums)
            removed = sorted(first_nums - second_nums)
            modified = sorted(
                num for num in (first_nums & second_nums) if first_by_pr[num] != second_by_pr[num]
            )
            drift_reasons.append("MERGED_PR_ENUMERATION_DRIFT")
            drift_details["added_pr_numbers"] = added
            drift_details["removed_pr_numbers"] = removed
            drift_details["modified_pr_numbers"] = modified

        if drift_reasons:
            assert_no_forbidden_raw_keys(drift_details, field_name="drift_details")
            return CompletionGateResult(
                passed=False,
                status=SyncRunStatus.PARTIAL,
                import_ready=False,
                source_drift=True,
                retry_allowed=False,
                failure_reason_code="PLAN_DRIFT",
                started_observed_at=plan.started_observed_at,
                ended_observed_at=ended_at,
                drift_reasons=tuple(drift_reasons),
                drift_details=drift_details,
                processed_range=processed_range,
                remaining_range=remaining_range,
                has_safe_checkpoint=False,
            )

        # 4. Required streams & unresolved transient provider errors
        completed_set = (
            set(completed_required_streams)
            if completed_required_streams is not None
            else set(plan.required_streams)
        )
        missing_streams = [s for s in plan.required_streams if s not in completed_set]
        if transient_error_count > 0 or missing_streams:
            return CompletionGateResult(
                passed=False,
                status=SyncRunStatus.PARTIAL,
                import_ready=False,
                source_drift=False,
                retry_allowed=bool(has_safe_checkpoint),
                failure_reason_code="TRANSIENT_PROVIDER_ERROR",
                started_observed_at=plan.started_observed_at,
                ended_observed_at=ended_at,
                drift_details=(
                    {"missing_required_streams": [s.value for s in missing_streams]}
                    if missing_streams
                    else {}
                ),
                processed_range=processed_range,
                remaining_range=remaining_range,
                has_safe_checkpoint=bool(has_safe_checkpoint),
            )

        # 5. All completion gates satisfied -> SUCCESS + IMPORT_READY
        return CompletionGateResult(
            passed=True,
            status=SyncRunStatus.SUCCESS,
            import_ready=True,
            source_drift=False,
            retry_allowed=False,
            failure_reason_code=None,
            started_observed_at=plan.started_observed_at,
            ended_observed_at=ended_at,
            processed_range=processed_range,
            remaining_range=None,
            has_safe_checkpoint=has_safe_checkpoint,
        )
