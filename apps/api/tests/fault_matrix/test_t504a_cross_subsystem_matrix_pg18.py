"""Real PostgreSQL 18 pytest suite for T-504 / T-504A Cross-Subsystem Fault & Race Matrix."""

from __future__ import annotations

import json
from pathlib import Path

from career_memory_api.conformance.ai_schemas import render_canonical_json
from career_memory_api.conformance.checks import (
    M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
    default_repo_root,
)
from career_memory_api.fault_matrix import (
    FaultMatrixScenarioId,
    normalize_t504a_bundle_for_drift_check,
    run_t504a_fault_matrix,
)


def test_t504a_all_12_cross_subsystem_fault_and_race_scenarios_pg18() -> None:
    """Execute all 12 XFLT-01..XFLT-12 scenarios on real PG18 and assert 0 drift against committed evidence."""
    repo_root: Path = default_repo_root()
    bundle = run_t504a_fault_matrix(repo_root=repo_root)

    assert bundle.postgres_major_version >= 18
    assert bundle.openapi_import_v1_blob_sha == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1
    assert bundle.external_network_calls == 0
    assert bundle.spend_krw == 0
    assert bundle.sleep_based_race_waits == 0
    assert bundle.rls_catalog_verified["all_tables_force_rls"] is True
    assert bundle.rls_catalog_verified["all_runtime_roles_nobypassrls"] is True
    assert bundle.rls_catalog_verified["catalog_violations"] == 0
    assert bundle.rls_catalog_verified["table_count"] >= 46

    expected_ids = [sid for sid in FaultMatrixScenarioId]
    assert [sc.scenario_id for sc in bundle.scenarios] == expected_ids
    assert bundle.summary["all_passed"] is True
    assert bundle.summary["passed_scenarios"] == 12
    assert bundle.summary["failed_scenarios"] == 0
    assert bundle.summary["total_plan_c_and_secret_violations"] == 0
    assert bundle.summary["barrier_synchronized_scenarios"] >= 4

    for sc in bundle.scenarios:
        assert sc.passed is True, f"Scenario {sc.scenario_id} failed: {sc.observed_metrics}"
        assert sc.assertions_failed == 0
        assert sc.plan_c_audit.passed is True
        assert sc.plan_c_audit.audited_table_count >= 46
        assert sc.plan_c_audit.raw_text_violations == 0
        assert sc.plan_c_audit.secret_key_violations == 0
        assert sc.plan_c_audit.canary_string_hits == 0
        assert sc.plan_c_audit.log_canary_hits == 0
        assert sc.plan_c_audit.export_canary_hits == 0

    evidence_path = repo_root / "contracts" / "fault-matrix" / "t504a_evidence_bundle.v1.json"
    assert evidence_path.is_file(), "Committed t504a_evidence_bundle.v1.json must exist."
    committed = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert render_canonical_json(
        normalize_t504a_bundle_for_drift_check(committed)
    ) == render_canonical_json(
        normalize_t504a_bundle_for_drift_check(bundle.model_dump(mode="json"))
    )
