"""T-306 & R-002 Curation Usability Telemetry Aggregator with Strict Partition Isolation.

Enforces two critical product/governance principles:
1. Strict structural separation between:
   - ``SYNTHETIC_CONTRACT_REGRESSION``: metrics observed while executing the frozen
     21-case synthetic regression suite (classified strictly as contract regression,
     NOT architecture go/no-go and NEVER disguised as real user review time).
   - ``CLOSED_PILOT_USER_TELEMETRY``: real user closed-pilot curation sessions
     (classified as R-002 usability evidence; explicitly reported as
     ``NOT_YET_COLLECTED_PRE_PILOT`` with ``pilot_session_count = 0`` until real
     closed-pilot sessions are recorded).
2. Auto-clustering non-activation rule (T-306): curation cost observations NEVER
   enable automatic multi-PR or multi-direct-commit clustering in MVP.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

TelemetryPartition = Literal[
    "SYNTHETIC_CONTRACT_REGRESSION",
    "CLOSED_PILOT_USER_TELEMETRY",
]

CANONICAL_REVIEW_ACTIONS: tuple[str, ...] = (
    "CONFIRM",
    "EDIT",
    "SPLIT",
    "MERGE",
    "DISSOLVE",
    "REJECT",
)


@dataclass(frozen=True, slots=True)
class CurationTelemetrySample:
    """Single curation interaction or candidate evaluation observation."""

    partition: TelemetryPartition
    case_or_session_id: str
    event_id: str | None
    anchor_mode: str | None  # PR | PR_SEGMENT | DIRECT_COMMIT | USER_COMPOSITE
    action: str | None  # CONFIRM | EDIT | SPLIT | MERGE | DISSOLVE | REJECT | None
    outcome: str  # ACCEPTED | REJECTED | CANDIDATE_GENERATED | READ_ONLY
    review_duration_ms: float | None
    review_flags_present: tuple[str, ...] = ()
    review_flag_acted_upon: bool = False
    post_merge_top_level_duplicates: int = 0


def compute_percentile_ms(values: Sequence[float], percentile: float) -> float | None:
    """Compute nearest-rank percentile (e.g. 50.0, 95.0) rounded to 3 decimals."""
    cleaned = sorted(float(v) for v in values if v is not None and v >= 0.0)
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return round(cleaned[0], 3)
    rank = max(1, math.ceil((percentile / 100.0) * len(cleaned)))
    return round(cleaned[min(rank - 1, len(cleaned) - 1)], 3)


@dataclass(slots=True)
class CurationTelemetryCollector:
    """Accumulates telemetry samples while keeping synthetic and pilot partitions isolated."""

    _samples: list[CurationTelemetrySample] = field(default_factory=list)

    def record_sample(self, sample: CurationTelemetrySample) -> None:
        if sample.partition not in (
            "SYNTHETIC_CONTRACT_REGRESSION",
            "CLOSED_PILOT_USER_TELEMETRY",
        ):
            raise ValueError(f"Unsupported telemetry partition: {sample.partition!r}")
        if sample.review_duration_ms is not None and sample.review_duration_ms < 0:
            raise ValueError("review_duration_ms must be non-negative.")
        if sample.post_merge_top_level_duplicates < 0:
            raise ValueError("post_merge_top_level_duplicates must be non-negative.")
        self._samples.append(sample)

    @property
    def samples(self) -> tuple[CurationTelemetrySample, ...]:
        return tuple(self._samples)

    def summarize(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(deterministic_summary, volatile_timing_metrics)``.

        Separates deterministic structural metrics (included in committed evidence
        bundles for zero-drift verification) from wall-clock execution timings
        (stored in volatile runtime metrics).
        """
        syn_samples = [
            s for s in self._samples if s.partition == "SYNTHETIC_CONTRACT_REGRESSION"
        ]
        pilot_samples = [
            s for s in self._samples if s.partition == "CLOSED_PILOT_USER_TELEMETRY"
        ]

        syn_det, syn_vol = _summarize_partition(
            partition="SYNTHETIC_CONTRACT_REGRESSION",
            samples=syn_samples,
        )
        pilot_det, pilot_vol = _summarize_partition(
            partition="CLOSED_PILOT_USER_TELEMETRY",
            samples=pilot_samples,
        )

        deterministic_summary: dict[str, Any] = {
            "governance_policy": {
                "classification_boundary": (
                    "Results are classified as contract regression (SYNTHETIC_CONTRACT_REGRESSION) "
                    "and R-002 usability evidence (CLOSED_PILOT_USER_TELEMETRY), NOT architecture go/no-go."
                ),
                "auto_clustering_enabled": False,
                "auto_clustering_policy_note": (
                    "Per T-306, curation telemetry and data cost observations MUST NOT activate "
                    "automatic multi-PR or multi-direct-commit clustering in MVP."
                ),
                "strict_partition_separation": True,
            },
            "partitions": {
                "SYNTHETIC_CONTRACT_REGRESSION": syn_det,
                "CLOSED_PILOT_USER_TELEMETRY": pilot_det,
            },
        }
        volatile_timings: dict[str, Any] = {
            "SYNTHETIC_CONTRACT_REGRESSION": syn_vol,
            "CLOSED_PILOT_USER_TELEMETRY": pilot_vol,
        }
        return deterministic_summary, volatile_timings


def record_curation_telemetry_sample(
    collector: CurationTelemetryCollector,
    *,
    partition: TelemetryPartition,
    case_or_session_id: str,
    event_id: str | None = None,
    anchor_mode: str | None = None,
    action: str | None = None,
    outcome: str = "ACCEPTED",
    review_duration_ms: float | None = None,
    review_flags_present: Sequence[str] = (),
    review_flag_acted_upon: bool = False,
    post_merge_top_level_duplicates: int = 0,
) -> CurationTelemetrySample:
    """Create and record a single curation usability telemetry sample."""
    sample = CurationTelemetrySample(
        partition=partition,
        case_or_session_id=case_or_session_id,
        event_id=event_id,
        anchor_mode=anchor_mode,
        action=action,
        outcome=outcome,
        review_duration_ms=review_duration_ms,
        review_flags_present=tuple(sorted(set(review_flags_present))),
        review_flag_acted_upon=review_flag_acted_upon,
        post_merge_top_level_duplicates=post_merge_top_level_duplicates,
    )
    collector.record_sample(sample)
    return sample


def _summarize_partition(
    *,
    partition: TelemetryPartition,
    samples: Sequence[CurationTelemetrySample],
) -> tuple[dict[str, Any], dict[str, Any]]:
    is_pilot = partition == "CLOSED_PILOT_USER_TELEMETRY"
    session_ids = sorted({s.case_or_session_id for s in samples})

    action_counts: dict[str, int] = {act: 0 for act in CANONICAL_REVIEW_ACTIONS}
    accepted_action_counts: dict[str, int] = {act: 0 for act in CANONICAL_REVIEW_ACTIONS}
    rejected_action_counts: dict[str, int] = {act: 0 for act in CANONICAL_REVIEW_ACTIONS}
    total_actions = 0
    total_accepted_actions = 0
    total_rejected_actions = 0

    pr_candidates = 0
    direct_commit_candidates = 0
    user_composite_merges_accepted = 0
    user_composite_merge_attempts = 0
    flagged_events_total = 0
    flagged_events_acted_upon = 0
    candidate_samples_total = 0
    post_merge_duplicates_total = 0

    review_durations: list[float] = []

    for s in samples:
        post_merge_duplicates_total += s.post_merge_top_level_duplicates
        if s.review_duration_ms is not None:
            review_durations.append(s.review_duration_ms)

        if s.outcome == "CANDIDATE_GENERATED":
            candidate_samples_total += 1
            if s.anchor_mode == "PR":
                pr_candidates += 1
            elif s.anchor_mode == "DIRECT_COMMIT":
                direct_commit_candidates += 1
            if s.review_flags_present:
                flagged_events_total += 1
                if s.review_flag_acted_upon:
                    flagged_events_acted_upon += 1

        if s.action in action_counts:
            action_counts[s.action] += 1
            total_actions += 1
            if s.outcome == "ACCEPTED":
                accepted_action_counts[s.action] += 1
                total_accepted_actions += 1
            elif s.outcome == "REJECTED":
                rejected_action_counts[s.action] += 1
                total_rejected_actions += 1

            if s.action == "MERGE":
                user_composite_merge_attempts += 1
                if s.outcome == "ACCEPTED":
                    user_composite_merges_accepted += 1
            if s.review_flags_present:
                flagged_events_total += 1
                if s.review_flag_acted_upon:
                    flagged_events_acted_upon += 1

    action_ratios: dict[str, float] = {
        act: round(action_counts[act] / total_actions, 4) if total_actions > 0 else 0.0
        for act in CANONICAL_REVIEW_ACTIONS
    }
    direct_commit_to_pr_ratio = (
        round(direct_commit_candidates / pr_candidates, 4)
        if pr_candidates > 0
        else (float(direct_commit_candidates) if direct_commit_candidates > 0 else None)
    )
    direct_commit_share = (
        round(direct_commit_candidates / (pr_candidates + direct_commit_candidates), 4)
        if (pr_candidates + direct_commit_candidates) > 0
        else None
    )
    review_flag_hit_rate = (
        round(flagged_events_total / candidate_samples_total, 4)
        if candidate_samples_total > 0
        else None
    )
    review_flag_utilization_rate = (
        round(flagged_events_acted_upon / flagged_events_total, 4)
        if flagged_events_total > 0
        else None
    )
    user_composite_merge_frequency = (
        round(user_composite_merges_accepted / total_accepted_actions, 4)
        if total_accepted_actions > 0
        else None
    )

    if is_pilot and not samples:
        deterministic: dict[str, Any] = {
            "partition": partition,
            "classification": "R-002_USABILITY_EVIDENCE",
            "status": "NOT_YET_COLLECTED_PRE_PILOT",
            "pilot_session_count": 0,
            "events_reviewed_count": 0,
            "total_command_samples": 0,
            "action_counts": action_counts,
            "action_distribution_ratio": action_ratios,
            "candidate_anchor_counts": {
                "PR": 0,
                "DIRECT_COMMIT": 0,
                "direct_commit_to_pr_ratio": None,
                "direct_commit_share": None,
            },
            "user_composite_merge_stats": {
                "accepted_merges": 0,
                "attempted_merges": 0,
                "merge_frequency_of_accepted_actions": None,
            },
            "review_flag_stats": {
                "flagged_events_total": 0,
                "flagged_events_acted_upon": 0,
                "flag_hit_rate": None,
                "flag_utilization_rate": None,
            },
            "post_merge_top_level_search_export_duplicate_count": 0,
            "honesty_note": (
                "Closed pilot has not yet been executed. Real human review duration (p50/p95) "
                "and R-002 usability rates are intentionally null/0 and NEVER substituted "
                "with synthetic regression timings."
            ),
        }
        volatile: dict[str, Any] = {
            "sample_count": 0,
            "review_time_p50_ms": None,
            "review_time_p95_ms": None,
        }
        return deterministic, volatile

    deterministic = {
        "partition": partition,
        "classification": (
            "R-002_USABILITY_EVIDENCE"
            if is_pilot
            else "CONTRACT_REGRESSION_ONLY_NOT_ARCHITECTURE_GO_NO_GO"
        ),
        "status": "COLLECTED" if is_pilot else "SYNTHETIC_21_CASE_REGRESSION_EXECUTED",
        "pilot_session_count": len(session_ids) if is_pilot else 0,
        "synthetic_case_count": 0 if is_pilot else len(session_ids),
        "total_samples": len(samples),
        "total_command_samples": total_actions,
        "accepted_command_samples": total_accepted_actions,
        "rejected_command_samples": total_rejected_actions,
        "action_counts": action_counts,
        "accepted_action_counts": accepted_action_counts,
        "rejected_action_counts": rejected_action_counts,
        "action_distribution_ratio": action_ratios,
        "candidate_anchor_counts": {
            "PR": pr_candidates,
            "DIRECT_COMMIT": direct_commit_candidates,
            "direct_commit_to_pr_ratio": direct_commit_to_pr_ratio,
            "direct_commit_share": direct_commit_share,
        },
        "user_composite_merge_stats": {
            "accepted_merges": user_composite_merges_accepted,
            "attempted_merges": user_composite_merge_attempts,
            "merge_frequency_of_accepted_actions": user_composite_merge_frequency,
        },
        "review_flag_stats": {
            "flagged_events_total": flagged_events_total,
            "flagged_events_acted_upon": flagged_events_acted_upon,
            "flag_hit_rate": review_flag_hit_rate,
            "flag_utilization_rate": review_flag_utilization_rate,
        },
        "post_merge_top_level_search_export_duplicate_count": post_merge_duplicates_total,
    }
    volatile = {
        "sample_count": len(review_durations),
        "execution_or_review_p50_ms": compute_percentile_ms(review_durations, 50.0),
        "execution_or_review_p95_ms": compute_percentile_ms(review_durations, 95.0),
    }
    return deterministic, volatile


__all__ = [
    "CANONICAL_REVIEW_ACTIONS",
    "CurationTelemetryCollector",
    "CurationTelemetrySample",
    "TelemetryPartition",
    "compute_percentile_ms",
    "record_curation_telemetry_sample",
]
