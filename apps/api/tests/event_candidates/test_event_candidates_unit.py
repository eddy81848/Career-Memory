"""Pure unit tests for M3 Event Candidate Builder models & deterministic review flags."""

from __future__ import annotations

import pytest

from career_memory_api.event_candidates import (
    ReviewFlagCode,
    ReviewFlagInputs,
    evaluate_review_flags,
    extract_component_bucket,
    has_mixed_purpose_signals,
    is_generic_title,
)
from career_memory_api.event_candidates.postgres_builder import (
    _abbreviate_sha,
    _extract_first_line_title,
    _is_placeholder_title,
)
from career_memory_api.schema_contracts.enums import PrAssociationState, WorkEventAnchorMode


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "fix",
        "Fix",
        "update",
        "wip",
        "WIP",
        "misc",
        "chore",
        "fix: minor update",
        "chore: cleanup",
        "WIP: temp changes",
        "bugfix #123",
        "SYN-101 fix",
        "abc",
    ],
)
def test_generic_titles_are_flagged(title: str) -> None:
    assert is_generic_title(title) is True
    flags = evaluate_review_flags(
        ReviewFlagInputs(anchor_mode=WorkEventAnchorMode.PR, title=title)
    )
    assert ReviewFlagCode.GENERIC_TITLE_OR_SUMMARY in flags


@pytest.mark.parametrize(
    "title",
    [
        "Add idempotent payment retry guard",
        "Hotfix: raise circuit breaker timeout from 2s to 5s on edge router",
        "Throttle webhook burst retries for SYN-101",
        "Refactor auth header parser and add monthly CSV export",
        "Fix race condition in settlement ledger commit",
    ],
)
def test_specific_titles_are_not_flagged_as_generic(title: str) -> None:
    assert is_generic_title(title) is False
    flags = evaluate_review_flags(
        ReviewFlagInputs(anchor_mode=WorkEventAnchorMode.PR, title=title)
    )
    assert ReviewFlagCode.GENERIC_TITLE_OR_SUMMARY not in flags


def test_placeholder_titles_are_not_falsely_flagged() -> None:
    assert _is_placeholder_title("PR #101") is True
    assert _is_placeholder_title("Direct commit 201000000000 on main") is True
    assert is_generic_title("PR #101", is_placeholder=True) is False


def test_extract_component_bucket_separates_prod_and_test_companions() -> None:
    assert extract_component_bucket("src/settlement/retry_guard.py") == ("src/settlement", False)
    assert extract_component_bucket("tests/test_retry_guard.py") == ("tests", True)
    assert extract_component_bucket("src/middleware/auth_header.py") == ("src/middleware", False)
    assert extract_component_bucket("src/export/csv_stream.py") == ("src/export", False)
    assert extract_component_bucket("services/billing/proration.py") == ("services/billing", False)
    assert extract_component_bucket("infra/terraform/main.tf") == ("infra", False)


def test_ev001_single_purpose_pr_with_tests_not_flagged_mixed_purpose() -> None:
    flags = evaluate_review_flags(
        ReviewFlagInputs(
            anchor_mode=WorkEventAnchorMode.PR,
            title="Add idempotent payment retry guard",
            commit_changed_path_groups=(
                ("src/settlement/retry_guard.py",),
                ("tests/test_retry_guard.py",),
            ),
        )
    )
    assert flags == ()


def test_ev007_mixed_purpose_pr_flags_without_auto_split() -> None:
    inputs = ReviewFlagInputs(
        anchor_mode=WorkEventAnchorMode.PR,
        title="Refactor auth header parser and add monthly CSV export",
        commit_changed_path_groups=(
            ("src/middleware/auth_header.py",),
            ("src/export/csv_stream.py",),
        ),
    )
    assert has_mixed_purpose_signals(inputs) is True
    flags = evaluate_review_flags(inputs)
    assert flags == (ReviewFlagCode.MIXED_PURPOSE_SIGNALS,)


def test_mixed_purpose_triggered_by_multi_top_level_dirs_and_conflicting_labels() -> None:
    multi_dir = ReviewFlagInputs(
        anchor_mode=WorkEventAnchorMode.PR,
        title="Update billing pipeline configuration",
        changed_paths=("auth/jwt.py", "billing/invoice.py", "infra/deploy.yaml"),
    )
    assert ReviewFlagCode.MIXED_PURPOSE_SIGNALS in evaluate_review_flags(multi_dir)

    conflicting_labels = ReviewFlagInputs(
        anchor_mode=WorkEventAnchorMode.PR,
        title="Adjust cache eviction policy",
        labels=("bug", "feature"),
    )
    assert ReviewFlagCode.MIXED_PURPOSE_SIGNALS in evaluate_review_flags(conflicting_labels)


def test_multiple_linked_issues_partial_skip_and_ambiguous_author_flags() -> None:
    flags = evaluate_review_flags(
        ReviewFlagInputs(
            anchor_mode=WorkEventAnchorMode.PR,
            title="Implement batch export throttling",
            linked_issue_keys=("101", "202"),
            has_partial_source_skipped=True,
            has_ambiguous_author_identity=True,
        )
    )
    assert flags == (
        ReviewFlagCode.AMBIGUOUS_AUTHOR_IDENTITY,
        ReviewFlagCode.MULTIPLE_LINKED_ISSUES,
        ReviewFlagCode.PARTIAL_SOURCE_SKIPPED,
    )

    # Single issue (even duplicated with different casing) does NOT trigger MULTIPLE_LINKED_ISSUES
    single_issue_flags = evaluate_review_flags(
        ReviewFlagInputs(
            anchor_mode=WorkEventAnchorMode.PR,
            title="Implement batch export throttling",
            linked_issue_keys=("SYN-101", "syn-101"),
        )
    )
    assert ReviewFlagCode.MULTIPLE_LINKED_ISSUES not in single_issue_flags


def test_direct_commit_confirmation_and_multi_parent_flags() -> None:
    single_parent_flags = evaluate_review_flags(
        ReviewFlagInputs(
            anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
            title="Hotfix: raise circuit breaker timeout from 2s to 5s on edge router",
            parent_count=1,
            pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        )
    )
    assert single_parent_flags == (
        ReviewFlagCode.DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED,
    )

    multi_parent_flags = evaluate_review_flags(
        ReviewFlagInputs(
            anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
            title="Merge release branch into trunk",
            parent_count=2,
            pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        )
    )
    assert multi_parent_flags == (
        ReviewFlagCode.DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED,
        ReviewFlagCode.REVIEW_REQUIRED_MULTI_PARENT,
    )


def test_first_line_title_strips_multiline_body_and_abbreviates_sha() -> None:
    raw = "\n  Hotfix: raise timeout  \n\nCANARY_BODY_MUST_NOT_LEAK\nline 2"
    assert _extract_first_line_title(raw) == "Hotfix: raise timeout"
    assert "CANARY_BODY_MUST_NOT_LEAK" not in (_extract_first_line_title(raw) or "")
    assert _abbreviate_sha("2010000000000000000000000000000000000001") == "201000000000"
    assert 7 <= len(_abbreviate_sha("2010000000000000000000000000000000000001")) <= 12
