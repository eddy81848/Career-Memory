"""Models and constants for M3 T-306 / T-306A Frozen 21-Case Event Evaluation Harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EVAL_BUNDLE_SCHEMA_VERSION = "cm-t306a-event-eval-bundle.v1"
EXPECTED_APPROVED_DATASET_VERSION = "1.1.0-mvp-approved"
EXPECTED_APPROVED_21_BUNDLE_SHA256 = (
    "91cac742b48ad3bebee2e84aac3f6d3c5d9202c65d2406f3828b8494bf114a4d"
)
IMMUTABLE_OPENAPI_BLOB_SHA = "07594d75425744d6149250a9f7ff95ac24659f80"
EXPECTED_SCHEMA_MIGRATIONS: tuple[str, ...] = (
    "0010_identity_workspace_source_auth.sql",
    "0020_source_provenance_metadata.sql",
    "0030_events_facts_claims_contributions.sql",
    "0040_sync_runs_jobs_outbox_audit.sql",
    "0050_m2_backfill_observations.sql",
    "0060_m3_event_versions_and_constraints.sql",
)

# Exact exclusions documented in evaluation/event-draft/releases/1.1.0-mvp-approved/T-306A-HANDOFF.md §3
EXCLUDED_FROM_MVP_TRUTH_COMPARISON: tuple[str, ...] = (
    "UI badge strings & unconfirmed API field names for direct-commit association confirmation (DELTA-EV-002, CAND-EV-019)",
    "External API ReviewAction response tokens (CAND-EV-022, CAND-EV-023; domain ReviewAction CONFIRM/EDIT is verified)",
    "Unconfirmed HTTP status codes (409 vs 412, If-Match, 403 vs 404, CANNOT_LINK_CONFLICT HTTP code, confirm/edit HTTP codes)",
    "Internal diagnostic record persistence format & live GitHub API lookup assumptions for out-of-window PR deduplication (CAND-EV-019)",
    "OD-007 physical database table / storage layout details",
    "Editing behavior on an already-CONFIRMED Event (CAND-EV-023 covers active CANDIDATE -> new CANDIDATE only)",
    "Multi-violation error precedence (EV-014 cmd_syn_014_nested: MEMBER_ALREADY_COMPOSED vs MEMBER_NOT_ACTIVE)",
    "Non-Event ID wire error codes (EV-013), unauthorized foreign-workspace external wire codes (EV-016), mixed-purpose review flag literal (EV-007), and historical SUPERSEDED detail GET shape (EV-010)",
)

UNIMPLEMENTED_M5_CONTRACT_BOUNDARIES: tuple[str, ...] = (
    "M5 Full-Text Search (FTS) ranking engine is not yet implemented; search_checks verify single-logical-event collapse and non-raw text matching via production query_searchable_top_level_work_events() and GET /v1/work-events.",
    "M5 Export download/generation table and adapter are not yet implemented in migrations 0010..0060; EV-012 Export STALE invalidation is verified at contract level via atomic WORK_EVENT_COMPOSITE_DISSOLVED outbox cleanup intent alongside direct PG18 STALE verification on facts and claims.",
)

# Volatile top-level keys excluded from --verify-bundle deterministic drift checks
VOLATILE_BUNDLE_KEYS: frozenset[str] = frozenset(
    {
        "volatile_runtime_metrics",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovedCaseEnvelope:
    """Single approved case loaded strictly from 1.1.0-mvp-approved/manifest.json."""

    case_id: str
    source_record_id: str
    origin: str
    rel_path: str
    file_sha256: str
    eval_bundle_id: str
    confirmed_api_route_handoff: str | None
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OracleDiffEntry:
    """Single read-only path-level mismatch between frozen oracle and live PG18 result."""

    path: str
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CaseOracleComparison:
    """Structured read-only comparison result for a single approved case."""

    case_id: str
    matched: bool
    diffs: tuple[OracleDiffEntry, ...]
    assertions_checked: int
    excluded_unconfirmed_checks: tuple[str, ...] = ()

    def format_report(self) -> str:
        if self.matched:
            return (
                f"[{self.case_id}] ORACLE MATCHED "
                f"(assertions={self.assertions_checked}, diffs=0)"
            )
        lines = [
            f"[{self.case_id}] ORACLE MISMATCH "
            f"(assertions={self.assertions_checked}, diffs={len(self.diffs)}):"
        ]
        for idx, d in enumerate(self.diffs, start=1):
            lines.append(
                f"  {idx}. {d.path}: {d.message} | expected={d.expected!r} actual={d.actual!r}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "assertions_checked": self.assertions_checked,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
        }


@dataclass(frozen=True, slots=True)
class PlanCAuditResult:
    """Plan C (ADR-013) non-raw storage verification result across PG18 tables."""

    canaries_checked: int
    tables_scanned: int
    canary_hits: int
    content_units_plan_c_compliant: bool
    fact_support_plan_c_compliant: bool
    jsonb_forbidden_key_hits: int
    leak_locations: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.canary_hits == 0
            and self.content_units_plan_c_compliant
            and self.fact_support_plan_c_compliant
            and self.jsonb_forbidden_key_hits == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canaries_checked": self.canaries_checked,
            "tables_scanned": self.tables_scanned,
            "canary_hits": self.canary_hits,
            "content_units_plan_c_compliant": self.content_units_plan_c_compliant,
            "fact_support_plan_c_compliant": self.fact_support_plan_c_compliant,
            "jsonb_forbidden_key_hits": self.jsonb_forbidden_key_hits,
            "passed": self.passed,
            "leak_locations": list(self.leak_locations),
        }


@dataclass(frozen=True, slots=True)
class CommandExecutionRecord:
    """Observed outcome of executing a fixture user_command against production FastAPI/PG18."""

    command_id: str
    command_action: str
    http_method: str
    http_path: str
    http_status: int
    outcome: str  # ACCEPTED | REJECTED
    error_code: str | None
    internal_policy_code: str | None
    state_mutated: bool
    pre_state_sha256: str
    post_state_sha256: str
    resulting_event_ids: tuple[str, ...]
    latency_ms: float

    def to_deterministic_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_action": self.command_action,
            "http_method": self.http_method,
            "http_path": self.http_path,
            "http_status": self.http_status,
            "outcome": self.outcome,
            "error_code": self.error_code,
            "internal_policy_code": self.internal_policy_code,
            "state_mutated": self.state_mutated,
            "resulting_event_ids": list(self.resulting_event_ids),
        }


@dataclass(frozen=True, slots=True)
class ObservedWorkEvent:
    """Normalized logical WorkEvent extracted from real PG18 tables and Read API."""

    event_uuid: str
    logical_event_id: str | None
    workspace_id: str
    repo_id: str | None
    anchor_mode: str
    lifecycle_status: str
    is_active_top_level: bool
    version: int
    title: str
    summary: str | None
    primary_pr_id: str | None
    source_commit_ids: tuple[str, ...]
    context_ticket_ids: tuple[str, ...]
    cannot_link_source_ids: tuple[str, ...]
    member_event_ids: tuple[str, ...]
    parent_event_id: str | None
    composite_membership_status: str | None
    composite_direct_must_link_count: int
    member_snapshots_preserved: bool
    prior_version_preserved: bool
    version_numbers: tuple[int, ...]
    searchable_as_top_level: bool
    counted_in_metrics: bool
    review_flags: tuple[str, ...]
    requires_user_confirmation: bool
    no_match_observed_means_definitely_no_pr: bool | None
    anchor_scope: dict[str, Any] | None
    dependent_fact_ids: tuple[str, ...]
    dependent_claim_ids: tuple[str, ...]
    merge_origin: str
    api_detail_verified: bool


@dataclass(frozen=True, slots=True)
class ObservedReviewAction:
    """Observed domain ReviewAction row from public.review_actions."""

    review_action_id: str
    event_id: str
    action: str
    from_version: int | None
    to_version: int | None
    changed_fields: tuple[str, ...]
    creates_new_event_version: bool
    is_event_merge: bool


@dataclass(frozen=True, slots=True)
class ObservedDownstreamArtifact:
    """Observed state of a seeded dependent Fact, Claim, or Export invalidation signal."""

    artifact_type: str  # FACT | CLAIM | EXPORT
    artifact_id: str
    source_event_id: str
    status: str
    still_exists: bool
    transferred_to_members: bool
    verification_boundary: str  # PG18_TABLE | M5_OUTBOX_CONTRACT_BOUNDARY


@dataclass(frozen=True, slots=True)
class ObservedHeldItem:
    """Observed held / non-eligible commit record in PG18."""

    source_id: str
    record_kind: str  # OBSERVATION_RECORD | NON_ELIGIBLE_SOURCE_RECORD
    pr_association_state: str | None
    interpreted_as_no_pr: bool


@dataclass(frozen=True, slots=True)
class ObservedOutOfWindowDedup:
    """Observed out-of-window PR commit deduplication state in PG18."""

    commit_id: str
    matched_pr_provider_id: str | None
    creates_new_pr_event: bool
    creates_direct_commit_event: bool


@dataclass(frozen=True, slots=True)
class ObservedSearchCheck:
    """Observed top-level collapsed search result for a fixture search_check."""

    query: str
    workspace_id: str
    matched_event_uuids: tuple[str, ...]
    result_count: int
    duplicate_count: int
    verification_boundary: str = "CONTRACT_LEVEL_SEARCH_COLLAPSE"


@dataclass(frozen=True, slots=True)
class ObservedCaseSnapshot:
    """Complete logical state extracted from PG18 & Read API after case execution."""

    case_id: str
    events: tuple[ObservedWorkEvent, ...]
    review_actions: tuple[ObservedReviewAction, ...]
    downstream_artifacts: tuple[ObservedDownstreamArtifact, ...]
    held_items: tuple[ObservedHeldItem, ...]
    out_of_window_dedups: tuple[ObservedOutOfWindowDedup, ...]
    search_checks: tuple[ObservedSearchCheck, ...]
    per_workspace_api_counts: dict[str, int]
    total_countable_events: int
    total_user_composite_events: int
    total_direct_commit_events: int
    total_held_items: int
    total_rejected_events: int
    promoted_events_from_partial_runs: int
    source_provenance_counts_before: dict[str, int]
    source_provenance_counts_after: dict[str, int]
    dissolve_outbox_event_count: int
    merge_outbox_event_count: int
    top_level_duplicate_count: int
    logical_state_sha256: str


@dataclass(frozen=True, slots=True)
class CaseEvaluationResult:
    """End-to-end execution, comparison, and Plan C audit result for 1 approved case."""

    case_id: str
    source_record_id: str
    origin: str
    eval_bundle_id: str
    file_sha256: str
    execution_mode: str  # CANDIDATE_BUILDER | CURATION_COMMANDS | READ_COLLAPSE
    builder_status: str | None
    builder_error_code: str | None
    replay_idempotency_checked: bool
    replay_idempotency_passed: bool | None
    command_results: tuple[CommandExecutionRecord, ...]
    observed_snapshot: ObservedCaseSnapshot
    comparison: CaseOracleComparison
    plan_c_audit: PlanCAuditResult
    execution_ms: float
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        replay_ok = (
            self.replay_idempotency_passed is not False
            if self.replay_idempotency_checked
            else True
        )
        return self.comparison.matched and self.plan_c_audit.passed and replay_ok

    def to_deterministic_bundle_entry(self) -> dict[str, Any]:
        ev_by_uuid = {e.event_uuid: e for e in self.observed_snapshot.events}

        def _det_event_ref(uid: str) -> str:
            ev = ev_by_uuid.get(uid)
            if ev is None:
                return uid
            if ev.logical_event_id is not None:
                return ev.logical_event_id
            return f"created:{ev.anchor_mode}:{ev.title}"

        det_commands = []
        for c in self.command_results:
            cd = c.to_deterministic_dict()
            cd["resulting_event_ids"] = [_det_event_ref(uid) for uid in c.resulting_event_ids]
            det_commands.append(cd)

        return {
            "case_id": self.case_id,
            "source_record_id": self.source_record_id,
            "origin": self.origin,
            "eval_bundle_id": self.eval_bundle_id,
            "file_sha256": self.file_sha256,
            "execution_mode": self.execution_mode,
            "builder_status": self.builder_status,
            "builder_error_code": self.builder_error_code,
            "replay_idempotency_checked": self.replay_idempotency_checked,
            "replay_idempotency_passed": self.replay_idempotency_passed,
            "passed": self.passed,
            "oracle_comparison": self.comparison.to_dict(),
            "plan_c_audit": self.plan_c_audit.to_dict(),
            "command_results": det_commands,
            "summary_counts": {
                "total_events_in_db": len(self.observed_snapshot.events),
                "total_countable_events": self.observed_snapshot.total_countable_events,
                "total_user_composite_events": self.observed_snapshot.total_user_composite_events,
                "total_direct_commit_events": self.observed_snapshot.total_direct_commit_events,
                "total_held_items": self.observed_snapshot.total_held_items,
                "total_rejected_events": self.observed_snapshot.total_rejected_events,
                "top_level_duplicate_count": self.observed_snapshot.top_level_duplicate_count,
                "review_action_count": len(self.observed_snapshot.review_actions),
                "search_check_count": len(self.observed_snapshot.search_checks),
            },
            "logical_state_sha256": self.observed_snapshot.logical_state_sha256,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class BundleGroupSummary:
    """Same-source evaluation bundle rollup across the 16 approved bundles."""

    eval_bundle_id: str
    case_ids: tuple[str, ...]
    passed_count: int
    total_count: int

    @property
    def all_passed(self) -> bool:
        return self.passed_count == self.total_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "eval_bundle_id": self.eval_bundle_id,
            "case_ids": list(self.case_ids),
            "passed_count": self.passed_count,
            "total_count": self.total_count,
            "all_passed": self.all_passed,
        }


@dataclass(frozen=True, slots=True)
class EvalSuiteResult:
    """Complete 21-case T-306A evaluation suite result + R-002 telemetry summary."""

    dataset_version: str
    approved_21_bundle_sha256: str
    case_results: tuple[CaseEvaluationResult, ...]
    bundle_summaries: tuple[BundleGroupSummary, ...]
    telemetry_summary: dict[str, Any]
    volatile_runtime_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.case_results if r.passed)

    @property
    def total_count(self) -> int:
        return len(self.case_results)

    @property
    def all_passed(self) -> bool:
        return self.passed_count == self.total_count and self.total_count == 21


__all__ = [
    "EVAL_BUNDLE_SCHEMA_VERSION",
    "EXCLUDED_FROM_MVP_TRUTH_COMPARISON",
    "EXPECTED_APPROVED_21_BUNDLE_SHA256",
    "EXPECTED_APPROVED_DATASET_VERSION",
    "EXPECTED_SCHEMA_MIGRATIONS",
    "IMMUTABLE_OPENAPI_BLOB_SHA",
    "UNIMPLEMENTED_M5_CONTRACT_BOUNDARIES",
    "VOLATILE_BUNDLE_KEYS",
    "ApprovedCaseEnvelope",
    "BundleGroupSummary",
    "CaseEvaluationResult",
    "CaseOracleComparison",
    "CommandExecutionRecord",
    "EvalSuiteResult",
    "ObservedCaseSnapshot",
    "ObservedDownstreamArtifact",
    "ObservedHeldItem",
    "ObservedOutOfWindowDedup",
    "ObservedReviewAction",
    "ObservedSearchCheck",
    "ObservedWorkEvent",
    "OracleDiffEntry",
    "PlanCAuditResult",
]
