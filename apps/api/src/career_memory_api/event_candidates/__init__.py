"""M3 Event Candidate Builder (`T-301` + `T-301A`).

Source-anchored ``PR`` and 1:1 ``DIRECT_COMMIT`` candidate builder, source
relation linker (``MUST_LINK`` / ``CONTEXT_ONLY`` / ``CANNOT_LINK`` precedence),
and deterministic review-flag evaluator operating strictly on Plan C metadata.
"""

from __future__ import annotations

from career_memory_api.event_candidates.flags import (
    ReviewFlagInputs,
    evaluate_review_flags,
    extract_component_bucket,
    has_mixed_purpose_signals,
    is_generic_title,
)
from career_memory_api.event_candidates.models import (
    DIRECT_COMMIT_ASSOCIATION_NOTE,
    DIRECT_COMMIT_BADGE_CODE,
    BuiltCandidateSummary,
    BuiltMembershipSummary,
    CandidateBuildErrorCode,
    CandidateBuildRequest,
    CandidateBuildResult,
    CandidateBuildStatus,
    EventRelationType,
    ReviewFlagCode,
)
from career_memory_api.event_candidates.postgres_builder import (
    BeforeCommitHook,
    ConnectionFactory,
    PostgresEventCandidateBuilder,
)

__all__ = [
    "BeforeCommitHook",
    "BuiltCandidateSummary",
    "BuiltMembershipSummary",
    "CandidateBuildErrorCode",
    "CandidateBuildRequest",
    "CandidateBuildResult",
    "CandidateBuildStatus",
    "ConnectionFactory",
    "DIRECT_COMMIT_ASSOCIATION_NOTE",
    "DIRECT_COMMIT_BADGE_CODE",
    "EventRelationType",
    "PostgresEventCandidateBuilder",
    "ReviewFlagCode",
    "ReviewFlagInputs",
    "evaluate_review_flags",
    "extract_component_bucket",
    "has_mixed_purpose_signals",
    "is_generic_title",
]
