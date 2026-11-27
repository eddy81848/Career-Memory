"""Declarative registry of all 16 B-004 fault-harness scenarios (S01..S16)."""

from __future__ import annotations

from career_memory_api.fault_harness.scenario_schema import (
    DEFAULT_OBSERVED_AT_ISO,
    FaultInjectionPoint,
    ScenarioSpec,
    validate_scenario_spec,
)

SCENARIO_SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        scenario_id="S01",
        title="10-page clean initial import completion (baseline)",
        requirements=("SYNC-01", "SYNC-02", "SYNC-05", "SYNC-20", "REF-04"),
        default_branch="trunk",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.NONE,),
        description=(
            "10 pages of merged PRs + 3 pages of default-branch ('trunk') commits + "
            "PR reviews/comments + optional referenced issues complete cleanly to SUCCESS, "
            "emitting exactly 1 IMPORT_READY outbox row, PR & DIRECT_COMMIT work_events, "
            "and Plan C metadata-only content_units."
        ),
    ),
    ScenarioSpec(
        scenario_id="S02",
        title="Transient 503 on PRS page 8/10 then safe checkpoint resume equivalence",
        requirements=("SYNC-04", "SYNC-10", "SYNC-14"),
        default_branch="trunk",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_PAGE_TX,),
        baseline_equivalence_target="S01",
        description=(
            "Same 10-page dataset as S01 with transient HTTP 503 on PRS page 8/10. "
            "Stops at PARTIAL (last_committed_page=7, retry_allowed=True, 0 promotions), "
            "resumes via POST /v1/sync-runs/{id}/retry without re-fetching pages 1..7, "
            "and converges to a 100% identical logical DB snapshot as S01."
        ),
    ),
    ScenarioSpec(
        scenario_id="S03",
        title="Worker crash inside PostgreSQL page TX before COMMIT then resume equivalence",
        requirements=("SYNC-07", "REPLAY-01"),
        default_branch="trunk",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.INSIDE_PAGE_TX_BEFORE_COMMIT,),
        baseline_equivalence_target="S01",
        description=(
            "Same 10-page dataset as S01 with crash injected inside PostgreSQL page TX "
            "on PRS page 5 right before COMMIT. Proves full atomic rollback of source_objects, "
            "source_object_revisions, sync_checkpoints, and sync_run_page_observations, "
            "followed by clean resume converging identically to S01."
        ),
    ),
    ScenarioSpec(
        scenario_id="S04",
        title="Worker crash after PostgreSQL page COMMIT before worker ack then resume equivalence",
        requirements=("SYNC-08", "REPLAY-02"),
        default_branch="trunk",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.AFTER_PAGE_COMMIT_BEFORE_ACK,),
        baseline_equivalence_target="S01",
        description=(
            "Same 10-page dataset as S01 with crash injected on PRS page 6 immediately after "
            "DB COMMIT and before worker heartbeat/ack. Proves lease reclaim and checkpoint "
            "fencing produce 0 duplicate objects/revisions/outbox effects and converge identically to S01."
        ),
    ),
    ScenarioSpec(
        scenario_id="S05",
        title="Concurrent duplicate import requests + 2-worker same checkpoint race",
        requirements=("SYNC-09", "SYNC-10", "REPLAY-04"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.CONCURRENT_REQUEST_AND_WORKER_RACE,),
        description=(
            "Concurrent POST /v1/sync-runs requests on the same repository enforce single active "
            "run (202 + 409 SYNC_ALREADY_RUNNING / 202 idempotent replay), and two workers racing "
            "on the same (sync_run_id, PRS, checkpoint_version) commit exactly once while the stale "
            "write is discarded with STALE_CHECKPOINT_VERSION."
        ),
    ),
    ScenarioSpec(
        scenario_id="S06",
        title="HTTP 200 truncated partial response and corrupted required fields",
        requirements=("SYNC-11",),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_PAGE_TX,),
        description=(
            "HTTP 200 Content-Length truncation and HTTP 200 missing required item fields both "
            "terminate in non-retryable PARTIAL (MALFORMED_OR_PARTIAL_PAGE, retry_allowed=False) "
            "with 0 IMPORT_READY and 0 domain promotions."
        ),
    ),
    ScenarioSpec(
        scenario_id="S07",
        title="Repeated / cyclic pagination cursor detection",
        requirements=("SYNC-12",),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_PAGE_TX,),
        description=(
            "Cyclic Link rel='next' cursor (page 2 -> page 2 self-loop) is detected immediately "
            "within safeguard bounds, halting as PARTIAL (CURSOR_CYCLE_DETECTED, retry_allowed=False) "
            "with 0 promotions and no infinite loop."
        ),
    ),
    ScenarioSpec(
        scenario_id="S08",
        title="Rate limit 429 Retry-After and timeout retry budget exhaustion",
        requirements=("SYNC-13",),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_PAGE_TX,),
        description=(
            "HTTP 429 with Retry-After and repeated request timeouts exhaust attempt limits cleanly, "
            "preserving safe committed checkpoints as PARTIAL (TRANSIENT_PROVIDER_ERROR, retry_allowed=True) "
            "with 0 IMPORT_READY and 0 promotions."
        ),
    ),
    ScenarioSpec(
        scenario_id="S09",
        title="Mid-run PR cue mutation and default branch / HEAD drift",
        requirements=("SYNC-16", "SYNC-17"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_FINALIZE_SUCCESS,),
        review_comment_drift_note=(
            "Completion gate verifies default branch, HEAD SHA, and 2nd-pass merged PR cues "
            "(pr_number/head_sha/updated_at/merge_commit_sha); global review/comment mutation "
            "detection across all PR threads is explicitly NOT assumed."
        ),
        description=(
            "Both PR cue mutation between 1st and 2nd enumeration passes and default-branch/HEAD "
            "mutation between start and completion gate terminate in PARTIAL (PLAN_DRIFT, "
            "source_drift=True, retry_allowed=False), reject /retry with 409, and promote 0 events."
        ),
    ),
    ScenarioSpec(
        scenario_id="S10",
        title="UNKNOWN PR association block + NO_MATCH_OBSERVED badge + out-of-window/squash exclusions",
        requirements=("SYNC-18", "SYNC-19"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_FINALIZE_SUCCESS,),
        description=(
            "Unresolved commit-to-PR lookup yields UNKNOWN, blocking SUCCESS (PARTIAL / "
            "INCOMPLETE_PR_ASSOCIATION) with 0 DIRECT_COMMIT candidates. On clean completion, "
            "NO_MATCH_OBSERVED yields 1 DIRECT_COMMIT with requires_user_confirmation=True while "
            "out-of-window PR and squash/merge commits produce 0 DIRECT_COMMIT candidates."
        ),
    ),
    ScenarioSpec(
        scenario_id="S11",
        title="PR-linked commits deduplicated strictly under PR Event MUST_LINK",
        requirements=("SYNC-20",),
        default_branch="master",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.NONE,),
        description=(
            "All commits linked to eligible merged PRs via head_sha, merge_commit_sha, or "
            "commit-PR API are attached solely as PR_COMMIT MUST_LINK memberships on PR work_events "
            "with 0 duplicate DIRECT_COMMIT work_events (exercising default_branch='master')."
        ),
    ),
    ScenarioSpec(
        scenario_id="S12",
        title="Max objects / requests / bytes budget enforcement (PARTIAL_WITH_LIMIT)",
        requirements=("SYNC-21",),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_PAGE_TX,),
        description=(
            "Exceeding configured max_objects / max_requests / max_bytes stops the run in "
            "PARTIAL_WITH_LIMIT (LIMIT_EXCEEDED, retry_allowed=False) with explicit processed_range "
            "and remaining_range and 0 Event/Fact/Claim promotions."
        ),
    ),
    ScenarioSpec(
        scenario_id="S13",
        title="Mid-run visibility flip to PRIVATE, 401 revocation, and policy API 503 failure",
        requirements=("SYNC-22", "POLICY-01"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.BEFORE_FINALIZE_SUCCESS,),
        description=(
            "Mid-run PUBLIC->PRIVATE flip, 401 token revocation, and policy API 503 outage all "
            "fail closed with 0 tombstones, 0 promotions, and complete blocking of raw content "
            "viewing, external AI transmission, and Export gates."
        ),
    ),
    ScenarioSpec(
        scenario_id="S14",
        title="Transient retry vs PLAN_DRIFT abandoned-run isolation vs post-SUCCESS 409 rejection",
        requirements=("SYNC-04", "SYNC-14", "SYNC-16"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(
            FaultInjectionPoint.BEFORE_PAGE_TX,
            FaultInjectionPoint.BEFORE_FINALIZE_SUCCESS,
        ),
        description=(
            "Transient PARTIAL resumes on the same frozen plan to SUCCESS and rejects subsequent "
            "create/retry with 409; PLAN_DRIFT rejects /retry with 409, exposes no HTTP abandon "
            "endpoint, and isolates abandoned runs when a new plan executes cleanly."
        ),
    ),
    ScenarioSpec(
        scenario_id="S15",
        title="Pre-IMPORT_READY block, duplicate completion idempotency, and refetch match/mismatch",
        requirements=("REPLAY-01", "REPLAY-02", "REPLAY-04", "REF-04", "REF-06"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(FaultInjectionPoint.AFTER_FINALIZE_BEFORE_DERIVED_UNITS,),
        description=(
            "Downstream derived-unit processing is blocked before IMPORT_READY; duplicate completion "
            "signals and outbox deliveries execute strictly once; post-SUCCESS refetch succeeds on "
            "hash match (MATCHED) and projects BLOCKED_SOURCE (preserving status='SUCCESS') with "
            "approval/export gates blocked on hash mismatch (REVISION_MISMATCH)."
        ),
    ),
    ScenarioSpec(
        scenario_id="S16",
        title="Post-SUCCESS OD-003 deletion fence & pre-resume deletion/private cancellation",
        requirements=("SYNC-22", "OD-003"),
        default_branch="main",
        observed_at_iso=DEFAULT_OBSERVED_AT_ISO,
        injection_points=(
            FaultInjectionPoint.AFTER_FINALIZE_BEFORE_DERIVED_UNITS,
            FaultInjectionPoint.BEFORE_PAGE_TX,
        ),
        description=(
            "Post-SUCCESS OD-003 deletion-preview/confirm activates deletion_jobs fence and blocks "
            "raw viewing, AI, and Export with 0 exposures; pre-resume OD-003 deletion fence or "
            "PRIVATE flip on a PARTIAL run immediately cancels/blocks resume with 0 resurrection."
        ),
    ),
)

SCENARIO_SPECS_BY_ID: dict[str, ScenarioSpec] = {spec.scenario_id: spec for spec in SCENARIO_SPECS}

for _spec in SCENARIO_SPECS:
    validate_scenario_spec(_spec)


def get_scenario_spec(scenario_id: str) -> ScenarioSpec:
    key = scenario_id.strip().upper()
    if key not in SCENARIO_SPECS_BY_ID:
        raise KeyError(f"Unknown B-004 scenario_id: {scenario_id!r}")
    return SCENARIO_SPECS_BY_ID[key]


__all__ = [
    "SCENARIO_SPECS",
    "SCENARIO_SPECS_BY_ID",
    "get_scenario_spec",
]
