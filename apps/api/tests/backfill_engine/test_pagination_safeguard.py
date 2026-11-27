"""Unit tests for T-203A PaginationSafeguard (cycles, HTTP 200 corruption, limits, dedupe)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from career_memory_api.backfill_engine import (
    BackfillFailureReasonCode,
    BackfillLimits,
    BackfillStreamName,
    ItemOutcomeKind,
    ItemOutcomeRecord,
    PaginationSafeguard,
    ProviderPageEnvelope,
    SkipReasonCode,
)
from career_memory_api.schema_contracts.enums import SourceObjectType
from career_memory_api.sync_runs.models import SyncRunStatus


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _pr_item(
    pr_num: int,
    *,
    content_label: str | None = None,
    extra_meta: dict | None = None,
) -> dict:
    meta = {
        "provider_number": pr_num,
        "state": "closed",
        "merged": True,
        "base_ref": "main",
        "head_sha": _sha(f"head-{pr_num}")[:40],
        "merge_commit_sha": _sha(f"merge-{pr_num}")[:40],
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "object_type": SourceObjectType.PR.value,
        "provider_object_id": f"pr:{pr_num}",
        "content_hash": _sha(content_label or f"pr-content-{pr_num}"),
        "provider_updated_at": datetime(2026, 6, 1, 12, 0, pr_num % 60, tzinfo=UTC),
        "normalized_metadata": meta,
    }


def _commit_item(idx: int) -> dict:
    sha = _sha(f"commit-{idx}")[:40]
    return {
        "object_type": SourceObjectType.COMMIT.value,
        "provider_object_id": f"commit:{sha}",
        "commit_sha": sha,
        "content_hash": _sha(f"commit-content-{idx}"),
        "provider_updated_at": datetime(2026, 6, 2, 12, 0, idx % 60, tzinfo=UTC),
        "normalized_metadata": {"commit_sha": sha, "base_ref": "main"},
    }


def test_immediate_cursor_repeat_a_to_a_detected_as_cycle() -> None:
    guard = PaginationSafeguard(BackfillLimits())

    decision = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=2,
            http_status=200,
            request_cursor="cursor-A",
            next_cursor="cursor-A",
            has_next_page=True,
            items=(_pr_item(1),),
            declared_item_count=1,
            payload_bytes=512,
        )
    )

    assert decision.accepted is False
    assert decision.terminal_status == SyncRunStatus.PARTIAL
    assert decision.failure_reason_code == BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED
    assert decision.retry_allowed is False


def test_multi_step_cursor_cycle_a_b_a_detected() -> None:
    guard = PaginationSafeguard(BackfillLimits())

    page1 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor="cursor-A",
            has_next_page=True,
            items=(_pr_item(1),),
            declared_item_count=1,
            payload_bytes=256,
        )
    )
    assert page1.accepted is True

    page2 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=2,
            http_status=200,
            request_cursor="cursor-A",
            next_cursor="cursor-B",
            has_next_page=True,
            items=(_pr_item(2),),
            declared_item_count=1,
            payload_bytes=256,
        )
    )
    assert page2.accepted is True

    page3 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=3,
            http_status=200,
            request_cursor="cursor-B",
            next_cursor="cursor-A",
            has_next_page=True,
            items=(_pr_item(3),),
            declared_item_count=1,
            payload_bytes=256,
        )
    )
    assert page3.accepted is False
    assert page3.terminal_status == SyncRunStatus.PARTIAL
    assert page3.failure_reason_code == BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED
    assert page3.retry_allowed is False


def test_identical_page_fingerprint_loop_detected_even_with_mutated_cursor() -> None:
    guard = PaginationSafeguard(BackfillLimits())

    item = _pr_item(10)
    page1 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor="cur-ts-1",
            has_next_page=True,
            items=(item,),
            declared_item_count=1,
            payload_bytes=200,
        )
    )
    assert page1.accepted is True

    page2 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=2,
            http_status=200,
            request_cursor="cur-ts-1",
            next_cursor="cur-ts-2",
            has_next_page=True,
            items=(item,),
            declared_item_count=1,
            payload_bytes=200,
        )
    )
    assert page2.accepted is False
    assert page2.terminal_status == SyncRunStatus.PARTIAL
    assert page2.failure_reason_code == BackfillFailureReasonCode.CURSOR_CYCLE_DETECTED
    assert page2.retry_allowed is False


def test_http_200_missing_required_identifiers_or_truncation_blocked_as_partial() -> None:
    guard = PaginationSafeguard(BackfillLimits())

    # Missing provider_object_id on HTTP 200
    missing_id = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            items=(
                {
                    "object_type": "PR",
                    "provider_object_id": "",
                    "content_hash": _sha("x"),
                    "provider_updated_at": datetime(2026, 6, 1, tzinfo=UTC),
                    "normalized_metadata": {},
                },
            ),
            declared_item_count=1,
            payload_bytes=100,
        )
    )
    assert missing_id.accepted is False
    assert missing_id.terminal_status == SyncRunStatus.PARTIAL
    assert missing_id.failure_reason_code == BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE
    assert missing_id.retry_allowed is False

    # Missing commit_sha on COMMIT item
    missing_sha = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            items=(
                {
                    "object_type": "COMMIT",
                    "provider_object_id": "commit:abc",
                    "content_hash": _sha("y"),
                    "provider_updated_at": datetime(2026, 6, 1, tzinfo=UTC),
                    "normalized_metadata": {},
                },
            ),
            declared_item_count=1,
            payload_bytes=100,
        )
    )
    assert missing_sha.accepted is False
    assert missing_sha.failure_reason_code == BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE

    # Declared item count mismatch (HTTP 200 truncated payload)
    count_mismatch = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            items=(_pr_item(1),),
            declared_item_count=5,
            payload_bytes=100,
        )
    )
    assert count_mismatch.accepted is False
    assert count_mismatch.failure_reason_code == BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE

    # Explicit is_truncated flag on HTTP 200
    truncated = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            is_truncated=True,
            items=(_pr_item(2),),
            declared_item_count=1,
            payload_bytes=100,
        )
    )
    assert truncated.accepted is False
    assert truncated.failure_reason_code == BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE

    # Plan C forbidden raw key smuggled inside normalized_metadata on HTTP 200
    raw_smuggle = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            items=(_pr_item(3, extra_meta={"pr_body": "secret raw markdown"}),),
            declared_item_count=1,
            payload_bytes=100,
        )
    )
    assert raw_smuggle.accepted is False
    assert raw_smuggle.failure_reason_code == BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE


def test_optional_context_issues_access_error_excluded_without_failing_run() -> None:
    guard = PaginationSafeguard(BackfillLimits())

    decision = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.CONTEXT_ISSUES,
            page_number=1,
            http_status=403,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            items=(),
            optional_access_error="ISSUES_DISABLED_OR_FORBIDDEN",
        )
    )

    assert decision.accepted is True
    assert decision.terminal_status is None
    assert len(decision.deduplicated_items) == 0
    assert len(decision.item_outcomes) == 1
    assert decision.item_outcomes[0].outcome_kind == ItemOutcomeKind.EXCLUDED
    assert decision.item_outcomes[0].reason_code == SkipReasonCode.OPTIONAL_CONTEXT_UNAVAILABLE.value


def test_max_objects_and_request_and_byte_limits_trigger_partial_with_limit() -> None:
    # Object limit exceeded on page 2
    guard = PaginationSafeguard(BackfillLimits(max_objects=2, max_requests=10, max_bytes=10_000))

    p1 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor="cur-2",
            has_next_page=True,
            items=(_pr_item(1), _pr_item(2)),
            declared_item_count=2,
            payload_bytes=500,
        )
    )
    assert p1.accepted is True
    assert p1.unique_objects_total == 2

    p2 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=2,
            http_status=200,
            request_cursor="cur-2",
            next_cursor="cur-3",
            has_next_page=True,
            items=(_pr_item(3),),
            declared_item_count=1,
            payload_bytes=300,
        )
    )
    assert p2.accepted is False
    assert p2.terminal_status == SyncRunStatus.PARTIAL_WITH_LIMIT
    assert p2.failure_reason_code == BackfillFailureReasonCode.LIMIT_EXCEEDED
    assert p2.retry_allowed is False
    assert p2.processed_range is not None and len(p2.processed_range) > 0
    assert p2.remaining_range is not None and len(p2.remaining_range) > 0

    # Byte limit exceeded
    byte_guard = PaginationSafeguard(BackfillLimits(max_objects=100, max_requests=10, max_bytes=400))
    bp1 = byte_guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor=None,
            has_next_page=False,
            items=(_commit_item(1),),
            declared_item_count=1,
            payload_bytes=500,
        )
    )
    assert bp1.accepted is False
    assert bp1.terminal_status == SyncRunStatus.PARTIAL_WITH_LIMIT
    assert bp1.failure_reason_code == BackfillFailureReasonCode.LIMIT_EXCEEDED
    assert bp1.retry_allowed is False


def test_boundary_duplicate_items_deduplicated_and_approved_skips_preserved() -> None:
    guard = PaginationSafeguard(BackfillLimits(max_objects=3))

    skip_record = ItemOutcomeRecord(
        stream_name=BackfillStreamName.PRS,
        object_type="FILE",
        provider_object_id="pr:1:assets/logo.png",
        outcome_kind=ItemOutcomeKind.SKIPPED,
        reason_code=SkipReasonCode.BINARY_FILE.value,
        affected_range="assets/logo.png",
    )

    p1 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            http_status=200,
            request_cursor=None,
            next_cursor="cur-2",
            has_next_page=True,
            items=(_pr_item(1), _pr_item(1), _pr_item(2)),
            declared_item_count=3,
            payload_bytes=300,
            skipped_items=(skip_record,),
        )
    )
    assert p1.accepted is True
    assert len(p1.deduplicated_items) == 2
    assert p1.unique_objects_total == 2
    assert len(p1.item_outcomes) == 1
    assert p1.item_outcomes[0].reason_code == SkipReasonCode.BINARY_FILE.value

    # Page 2 repeats pr:2 across page boundary + adds pr:3 -> total unique is 3 (within limit!)
    p2 = guard.evaluate_page(
        ProviderPageEnvelope(
            stream_name=BackfillStreamName.PRS,
            page_number=2,
            http_status=200,
            request_cursor="cur-2",
            next_cursor=None,
            has_next_page=False,
            items=(_pr_item(2), _pr_item(3)),
            declared_item_count=2,
            payload_bytes=200,
        )
    )
    assert p2.accepted is True
    assert p2.unique_objects_total == 3
