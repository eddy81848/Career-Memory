"""Fast unit tests for sync_runs request validation, fingerprinting, and job backoff policy."""

from __future__ import annotations

from datetime import timedelta

import pytest

from career_memory_api.jobs.models import (
    BackoffPolicy,
    JobErrorClass,
    JobStatus,
    OutboxStatus,
)
from career_memory_api.sync_runs.models import (
    ProcessingStatus,
    SyncRunMode,
    SyncRunStatus,
    compute_create_request_fingerprint,
    compute_retry_request_fingerprint,
    parse_idempotency_key,
    parse_initial_import_request,
    parse_retry_import_request,
)


def test_parse_idempotency_key_requires_valid_uuid() -> None:
    key = parse_idempotency_key("00000000-0000-4000-8000-000000000001")
    assert key == "00000000-0000-4000-8000-000000000001"

    for bad in (None, "", "   ", "not-a-uuid", "12345"):
        with pytest.raises(ValueError):
            parse_idempotency_key(bad)


def test_parse_initial_import_request_enforces_mode_date_and_limits() -> None:
    parsed = parse_initial_import_request(
        {
            "source_resource_id": "11111111-1111-4111-8111-111111111111",
            "mode": "INITIAL_BACKFILL",
            "backfill_from": "2025-09-23",
            "limits": {"max_objects": 5000},
        }
    )
    assert parsed.mode == SyncRunMode.INITIAL_BACKFILL
    assert parsed.backfill_from == "2025-09-23"
    assert parsed.limits == {"max_objects": 5000}

    invalid_payloads = [
        None,
        [],
        {"source_resource_id": "repo-1"},
        {"source_resource_id": "repo-1", "mode": "INCREMENTAL"},
        {"source_resource_id": "", "mode": "INITIAL_BACKFILL"},
        {
            "source_resource_id": "repo-1",
            "mode": "INITIAL_BACKFILL",
            "backfill_from": "2025-02-30",
        },
        {
            "source_resource_id": "repo-1",
            "mode": "INITIAL_BACKFILL",
            "limits": {"max_objects": True},
        },
        {
            "source_resource_id": "repo-1",
            "mode": "INITIAL_BACKFILL",
            "limits": "bad",
        },
    ]
    for bad in invalid_payloads:
        with pytest.raises(ValueError):
            parse_initial_import_request(bad)


def test_parse_retry_import_request_rejects_missing_extra_and_invalid_version() -> None:
    parsed = parse_retry_import_request({"expected_run_version": 2})
    assert parsed.expected_run_version == 2

    for bad in (
        None,
        {},
        {"expected_run_version": 0},
        {"expected_run_version": -1},
        {"expected_run_version": True},
        {"expected_run_version": "2"},
        {"expected_run_version": 2, "extra": 1},
    ):
        with pytest.raises(ValueError):
            parse_retry_import_request(bad)


def test_create_fingerprint_is_order_independent_and_value_sensitive() -> None:
    req_a = parse_initial_import_request(
        {
            "source_resource_id": "11111111-1111-4111-8111-111111111111",
            "mode": "INITIAL_BACKFILL",
            "backfill_from": "2025-09-23",
            "limits": {"max_objects": 5000},
        }
    )
    req_b = parse_initial_import_request(
        {
            "limits": {"max_objects": 5000},
            "backfill_from": "2025-09-23",
            "mode": "INITIAL_BACKFILL",
            "source_resource_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    req_c = parse_initial_import_request(
        {
            "source_resource_id": "11111111-1111-4111-8111-111111111111",
            "mode": "INITIAL_BACKFILL",
            "backfill_from": "2025-09-23",
            "limits": {"max_objects": 200},
        }
    )

    fp_a = compute_create_request_fingerprint(req_a)
    fp_b = compute_create_request_fingerprint(req_b)
    fp_c = compute_create_request_fingerprint(req_c)

    assert len(fp_a) == 64
    assert fp_a == fp_b
    assert fp_a != fp_c


def test_retry_fingerprint_distinguishes_run_id_and_version() -> None:
    fp_1 = compute_retry_request_fingerprint("run-1", 1)
    fp_2 = compute_retry_request_fingerprint("run-1", 2)
    fp_3 = compute_retry_request_fingerprint("run-2", 1)
    assert len({fp_1, fp_2, fp_3}) == 3


def test_backoff_policy_exponential_growth_and_cap() -> None:
    policy = BackoffPolicy(base_seconds=5, multiplier=2, max_seconds=30)
    assert policy.delay_for_attempt(1) == timedelta(seconds=5)
    assert policy.delay_for_attempt(2) == timedelta(seconds=10)
    assert policy.delay_for_attempt(3) == timedelta(seconds=20)
    assert policy.delay_for_attempt(4) == timedelta(seconds=30)
    assert policy.delay_for_attempt(10) == timedelta(seconds=30)


def test_canonical_enum_vocabularies_match_contract() -> None:
    assert {s.value for s in SyncRunStatus} == {
        "PENDING",
        "RUNNING",
        "SUCCESS",
        "PARTIAL",
        "PARTIAL_WITH_LIMIT",
        "FAILED",
        "CANCELED",
    }
    assert {s.value for s in ProcessingStatus} == {
        "NOT_STARTED",
        "RUNNING",
        "BLOCKED_POLICY",
        "BLOCKED_SOURCE",
        "FAILED",
        "COMPLETE",
    }
    assert {s.value for s in JobStatus} == {
        "PENDING",
        "RUNNING",
        "RETRY_WAIT",
        "SUCCEEDED",
        "FAILED_TERMINAL",
        "CANCELED",
    }
    assert {s.value for s in JobErrorClass} == {
        "RETRYABLE",
        "NON_RETRYABLE",
        "POISON_BUDGET_EXHAUSTED",
        "CANCELED_BY_FENCE",
    }
    assert {s.value for s in OutboxStatus} == {
        "PENDING",
        "PUBLISHED",
        "FAILED_TERMINAL",
    }
