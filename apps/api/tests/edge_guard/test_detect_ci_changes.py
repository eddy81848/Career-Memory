"""Unit tests for smart path-aware CI/CD change detection (scripts/detect_ci_changes.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "detect_ci_changes.py"

_spec = importlib.util.spec_from_file_location("detect_ci_changes", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["detect_ci_changes"] = _mod
_spec.loader.exec_module(_mod)
classify_changed_paths = _mod.classify_changed_paths


def test_docs_and_evidence_only_skips_heavy_pg18_web_and_deploy() -> None:
    res = classify_changed_paths(
        [
            "docs/m5/M5_PILOT_GO_NO_GO_REPORT.md",
            "contracts/llm-gate/t006_policy_audit.v1.json",
            "contracts/pilot-gate/t506_release_gate.v1.json",
            ".gitignore",
        ]
    )
    assert res.run_static_gate is True
    assert res.run_api_unit is True
    assert res.run_api_pg18 is False
    assert res.run_m0_spike is False
    assert res.run_web_test is False
    assert res.run_web_build is False
    assert res.run_web_e2e is False
    assert res.deploy_api is False
    assert res.deploy_web is False
    assert res.run_migrations is False
    assert res.any_cd_work is False


def test_web_ui_change_runs_only_web_including_ui_e2e_and_deploys_only_cm_web() -> None:
    res = classify_changed_paths(["apps/web/src/components/LiveFullstackShell.tsx"])
    assert res.run_web_test is True
    assert res.run_web_build is True
    assert res.run_web_e2e is True
    assert res.deploy_web is True
    assert res.run_api_unit is False
    assert res.run_api_pg18 is False
    assert res.deploy_api is False
    assert res.run_migrations is False


def test_web_unit_or_hosted_spec_change_skips_next_build_and_skips_cloud_run_deploy() -> None:
    res = classify_changed_paths(
        [
            "apps/web/src/components/HostedBffShell.test.tsx",
            "apps/web/e2e-hosted/hosted-staging.spec.ts",
        ]
    )
    assert res.run_web_test is True
    assert res.run_web_build is False
    assert res.run_web_e2e is False
    assert res.deploy_web is False
    assert res.deploy_api is False
    assert res.any_cd_work is False


def test_web_local_e2e_spec_change_runs_local_e2e_without_deploying_cloud_run() -> None:
    res = classify_changed_paths(["apps/web/e2e/event-review.spec.ts"])
    assert res.run_web_test is True
    assert res.run_web_build is True
    assert res.run_web_e2e is True
    assert res.deploy_web is False
    assert res.any_cd_work is False


def test_api_src_change_runs_pg18_and_indirect_web_contract_http_and_deploys_only_api() -> None:
    res = classify_changed_paths(["apps/api/src/career_memory_api/hosted_runtime/api_service.py"])
    assert res.run_api_unit is True
    assert res.run_api_pg18 is True
    assert res.run_web_contract_http is True
    assert res.deploy_api is True
    assert res.deploy_web is False
    assert res.run_migrations is False


def test_api_test_only_change_skips_docker_build_and_deploy_on_main() -> None:
    res = classify_changed_paths(["apps/api/tests/hosted_runtime/test_hosted_services.py"])
    assert res.run_api_unit is True
    assert res.run_api_pg18 is True
    assert res.deploy_api is False
    assert res.deploy_web is False
    assert res.any_cd_work is False


def test_db_migration_change_triggers_pg18_migrations_and_api_deploy() -> None:
    res = classify_changed_paths(["db/migrations/0080_budgets_search_deletion.sql"])
    assert res.run_api_pg18 is True
    assert res.run_migrations is True
    assert res.deploy_api is True
    assert res.deploy_web is False


def test_shared_openapi_contract_triggers_all_dependent_suites() -> None:
    res = classify_changed_paths(["contracts/import-v1/openapi.yaml"])
    assert res.run_api_pg18 is True
    assert res.run_m0_spike is True
    assert res.run_web_test is True
    assert res.run_web_contract_http is True
    assert res.run_web_e2e is True
    assert res.deploy_api is True
    assert res.deploy_web is True


def test_workflow_yml_only_skips_both_api_and_web_test_jobs_and_deploy() -> None:
    res = classify_changed_paths([".github/workflows/ci.yml"])
    assert res.run_static_gate is True
    assert res.run_api_unit is False
    assert res.run_api_pg18 is False
    assert res.run_m0_spike is False
    assert res.run_web_test is False
    assert res.run_web_contract_http is False
    assert res.run_web_build is False
    assert res.run_web_e2e is False
    assert res.deploy_api is False
    assert res.deploy_web is False
    assert res.run_migrations is False
    assert res.any_cd_work is False


def test_root_dockerignore_and_unknown_future_paths_fail_safe() -> None:
    res_docker = classify_changed_paths([".dockerignore"])
    assert res_docker.run_api_pg18 is True
    assert res_docker.deploy_api is True

    res_unknown = classify_changed_paths(["packages/new-shared-lib/index.ts"])
    assert res_unknown.run_api_pg18 is True
    assert res_unknown.run_web_test is True
    assert res_unknown.run_web_contract_http is True
    assert res_unknown.run_web_build is True
    assert res_unknown.run_web_e2e is True
