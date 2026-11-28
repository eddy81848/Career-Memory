"""Real PostgreSQL 18 21-Case Frozen Evaluation Suite Integration Tests (T-306 / T-306A).

Executes all 21 frozen cases from `evaluation/event-draft/releases/1.1.0-mvp-approved/manifest.json`
against real PostgreSQL 18 (migrations 0010..0060, NOBYPASSRLS + FORCE RLS) and the real M3
production stack (`PostgresEventCandidateBuilder`, `PostgresWorkEventsReadService`,
`PostgresWorkEventMutationService`, `create_work_events_router`, `query_searchable_top_level_work_events`),
plus verifies 0-drift parity with `contracts/event-eval/t306a_eval_bundle.v1.json`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator

import pytest

from career_memory_api.event_eval import (
    EXPECTED_APPROVED_21_BUNDLE_SHA256,
    EXPECTED_APPROVED_DATASET_VERSION,
    IMMUTABLE_OPENAPI_BLOB_SHA,
    EphemeralEventEvalPg18Cluster,
    FrozenEventEvalRunner,
    build_redacted_eval_bundle,
    default_repo_root,
    diff_deterministic_bundles,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg18_eval_cluster() -> Iterator[EphemeralEventEvalPg18Cluster]:
    cluster = EphemeralEventEvalPg18Cluster(prefix="cm-t306a-pytest-pg18-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


def test_t306a_frozen_21_cases_pg18_and_zero_drift_bundle(
    pg18_eval_cluster: EphemeralEventEvalPg18Cluster,
) -> None:
    repo_root = default_repo_root()
    runner = FrozenEventEvalRunner(pg18_eval_cluster, repo_root=repo_root)
    suite = runner.run_all()

    assert suite.dataset_version == EXPECTED_APPROVED_DATASET_VERSION
    assert suite.approved_21_bundle_sha256 == EXPECTED_APPROVED_21_BUNDLE_SHA256
    assert suite.total_count == 21
    assert suite.passed_count == 21, "\n".join(
        r.comparison.format_report() for r in suite.case_results if not r.passed
    )
    assert suite.all_passed is True
    assert len(suite.bundle_summaries) == 16
    assert all(b.all_passed for b in suite.bundle_summaries)

    by_id = {r.case_id: r for r in suite.case_results}

    # Verify all 9 candidate-builder cases passed replay idempotency on real PG18
    for cid in ("EV-001", "EV-002", "EV-003", "EV-004", "EV-005", "EV-006", "EV-007", "EV-019", "EV-020"):
        res = by_id[cid]
        assert res.execution_mode == "CANDIDATE_BUILDER"
        assert res.replay_idempotency_checked is True
        assert res.replay_idempotency_passed is True

    # Verify EV-016 dual boundary: HTTP 404 concealment + domain CROSS_WORKSPACE_MEMBER + FORCE RLS
    ev016 = by_id["EV-016"]
    cross_merge_cmd = next(
        c for c in ev016.command_results if c.command_id == "cmd_syn_016_cross_merge"
    )
    assert cross_merge_cmd.http_status == 404
    assert cross_merge_cmd.error_code == "RESOURCE_NOT_FOUND"
    assert cross_merge_cmd.internal_policy_code == "CROSS_WORKSPACE_MEMBER"
    assert cross_merge_cmd.state_mutated is False
    assert ev016.observed_snapshot.per_workspace_api_counts == {
        "ws_syn_016_a": 2,
        "ws_syn_016_b": 1,
    }

    # Verify EV-019 multi-token search regression fix ("currency rounding" -> PR #1902)
    ev019 = by_id["EV-019"]
    currency_check = next(
        s for s in ev019.observed_snapshot.search_checks if s.query == "currency rounding"
    )
    assert currency_check.result_count == 1
    assert currency_check.duplicate_count == 0

    # Verify EV-012 dissolve: direct PG18 STALE on facts/claims + WORK_EVENT_COMPOSITE_DISSOLVED outbox
    ev012 = by_id["EV-012"]
    assert ev012.observed_snapshot.dissolve_outbox_event_count >= 1
    assert all(
        d.status == "STALE" and d.still_exists and not d.transferred_to_members
        for d in ev012.observed_snapshot.downstream_artifacts
    )

    # Verify Plan C (ADR-013) compliance across all 21 cases
    for res in suite.case_results:
        assert res.plan_c_audit.passed is True
        assert res.plan_c_audit.canary_hits == 0
        assert res.plan_c_audit.jsonb_forbidden_key_hits == 0
        assert res.plan_c_audit.content_units_plan_c_compliant is True
        assert res.plan_c_audit.fact_support_plan_c_compliant is True

    # Verify R-002 telemetry partition honesty
    partitions = suite.telemetry_summary["partitions"]
    assert partitions["CLOSED_PILOT_USER_TELEMETRY"]["status"] == "NOT_YET_COLLECTED_PRE_PILOT"
    assert partitions["CLOSED_PILOT_USER_TELEMETRY"]["pilot_session_count"] == 0
    assert suite.telemetry_summary["governance_policy"]["auto_clustering_enabled"] is False
    assert (
        partitions["SYNTHETIC_CONTRACT_REGRESSION"][
            "post_merge_top_level_search_export_duplicate_count"
        ]
        == 0
    )

    # Verify 0-drift parity with committed redacted bundle contracts/event-eval/t306a_eval_bundle.v1.json
    openapi_sha = subprocess.run(
        ["git", "rev-parse", "HEAD:contracts/import-v1/openapi.yaml"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert openapi_sha == IMMUTABLE_OPENAPI_BLOB_SHA

    bundle_path = repo_root / "contracts" / "event-eval" / "t306a_eval_bundle.v1.json"
    assert bundle_path.is_file(), "Committed contracts/event-eval/t306a_eval_bundle.v1.json missing"
    expected_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    actual_bundle = build_redacted_eval_bundle(suite, openapi_blob_sha=openapi_sha)
    assert diff_deterministic_bundles(expected_bundle, actual_bundle) == []
