"""Real PostgreSQL 18 (0010..0050) B-004 16-Scenario Fault Matrix pytest suite."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from career_memory_api.fault_harness import (
    FIXED_NOW,
    B004PostgresScenarioRunner,
    EphemeralB004Pg18Cluster,
    SCENARIO_SPECS,
    build_10page_canonical_provider,
    build_redacted_evidence_bundle,
    compare_logical_snapshots,
)
from career_memory_api.fault_harness.scenario_schema import FaultInjectionPoint
from career_memory_api.sync_runs import SyncRunStatus

REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_SNAPSHOTS_DIR = REPO_ROOT / "contracts" / "fault-harness" / "expected_snapshots"
EVIDENCE_BUNDLE_PATH = REPO_ROOT / "contracts" / "fault-harness" / "b004_evidence_bundle.v1.json"


@pytest.fixture(scope="module")
def b004_pg18_cluster() -> Iterator[EphemeralB004Pg18Cluster]:
    cluster = EphemeralB004Pg18Cluster(repo_root=REPO_ROOT, prefix="cm-b004-pytest-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.mark.postgres
def test_b004_full_16_scenario_matrix_invariants_canaries_and_bundle_parity(
    b004_pg18_cluster: EphemeralB004Pg18Cluster,
) -> None:
    runner = B004PostgresScenarioRunner(
        b004_pg18_cluster,
        expected_snapshots_dir=EXPECTED_SNAPSHOTS_DIR,
    )
    results = runner.run_all()
    assert len(results) == len(SCENARIO_SPECS) == 16

    s01_core_sha = None
    for res in results:
        assert res.all_violations == (), f"{res.spec.scenario_id} invariant violations: {res.all_violations}"
        assert res.canary_audit.passed, f"{res.spec.scenario_id} canary audit failed: {res.canary_audit}"
        assert res.canary_audit.canary_hits == 0
        assert res.canary_audit.content_units_plan_c_compliant is True
        assert res.comparison is not None
        assert res.comparison.matched, res.comparison.format_report()
        if res.spec.scenario_id == "S01":
            s01_core_sha = res.logical_snapshot.core_convergence_sha256
        elif res.spec.scenario_id in ("S02", "S03", "S04"):
            assert res.baseline_equivalence_matched is True
            assert res.logical_snapshot.core_convergence_sha256 == s01_core_sha
        assert res.passed is True

    # Verify committed redacted evidence bundle matches live PG18 run with zero drift!
    openapi_sha = subprocess.run(
        ["git", "rev-parse", "HEAD:contracts/import-v1/openapi.yaml"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert openapi_sha == "07594d75425744d6149250a9f7ff95ac24659f80"

    expected_bundle = json.loads(EVIDENCE_BUNDLE_PATH.read_text(encoding="utf-8"))
    actual_bundle = build_redacted_evidence_bundle(results, openapi_blob_sha=openapi_sha)
    bundle_cmp = compare_logical_snapshots(
        scenario_id="EVIDENCE_BUNDLE",
        injection_points=(FaultInjectionPoint.NONE,),
        expected=expected_bundle,
        actual=actual_bundle,
    )
    assert bundle_cmp.matched, bundle_cmp.format_report()


@pytest.mark.postgres
@pytest.mark.parametrize("default_branch", ["main", "master"])
def test_b004_10page_resume_supports_main_and_master_default_branches_without_hardcoding(
    b004_pg18_cluster: EphemeralB004Pg18Cluster,
    default_branch: str,
) -> None:
    """Verify S02-style 8/10 transient failure & resume works on 'main' and 'master' too."""
    b004_pg18_cluster.truncate_all()
    runner = B004PostgresScenarioRunner(b004_pg18_cluster)
    provider = build_10page_canonical_provider(
        scenario_id="S02",
        canary_id="S02",
        default_branch=default_branch,
        repo_id="900400099" if default_branch == "main" else "900400098",
        repo_name=f"career-memory-{default_branch}",
    )
    provider.status_fault_queues[f"/repositories/{provider.repo_id}/pulls?page=8"] = [503]
    stack = runner._build_stack(provider)  # noqa: SLF001
    receipt = runner._create_run_http(stack, backfill_from="2026-08-01", limits={"max_objects": 500})  # noqa: SLF001
    run_id = receipt["target_id"]

    step1 = stack.worker.claim_and_execute_next(
        stack.access.workspace_id,
        worker_id=f"w-{default_branch}-1",
        access=stack.access,
        run_derived_units=True,
        now=FIXED_NOW,
    )
    assert step1 is not None and step1.execution.status is SyncRunStatus.PARTIAL
    assert step1.execution.retry_allowed is True

    st1 = runner._get_run_status_http(stack, run_id)  # noqa: SLF001
    runner._retry_run_http(stack, run_id=run_id, expected_run_version=st1["run_version"])  # noqa: SLF001
    step2 = stack.worker.claim_and_execute_next(
        stack.access.workspace_id,
        worker_id=f"w-{default_branch}-2",
        access=stack.access,
        run_derived_units=True,
        now=FIXED_NOW,
    )
    assert step2 is not None and step2.execution.status is SyncRunStatus.SUCCESS
    assert len(step2.execution.promoted_pr_event_ids) == 10
    assert len(step2.execution.promoted_direct_commit_event_ids) == 1
