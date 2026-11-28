"""T-106A conformance drift, failure-injection negative tests, and PG18 catalog integration tests."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from career_memory_api.conformance.checks import (
    M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
    check_artifact_drift,
    check_enum_parity,
    check_postgres_catalog_conformance,
    compute_git_blob_sha1,
    default_repo_root,
    run_all_conformance_checks,
    validate_openapi_invariants,
)
from career_memory_api.conformance.db_catalog import (
    CRITICAL_PARTIAL_UNIQUE_INDEXES,
    M1_REQUIRED_COMPOSITE_FKS,
    REQUIRED_PLAN_C_CONSTRAINTS,
    diff_db_catalog_snapshots,
    run_fresh_and_staged_pg18_catalog_smoke,
)
from career_memory_api.conformance.openapi_spec import generate_integrated_openapi_spec

REPO_ROOT = default_repo_root()
CONFORMANCE_CLI = REPO_ROOT / "scripts" / "run_conformance_ci.py"


def test_static_conformance_gate_passes_with_zero_violations() -> None:
    m0_bytes = (REPO_ROOT / "contracts" / "import-v1" / "openapi.yaml").read_bytes()
    assert compute_git_blob_sha1(m0_bytes) == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1

    assert check_artifact_drift(REPO_ROOT) == []
    assert check_enum_parity(REPO_ROOT) == []
    assert run_all_conformance_checks(REPO_ROOT, with_postgres=False) == []


def test_failure_injection_unregistered_enum_drift_detected_with_authority() -> None:
    violations = check_enum_parity(
        REPO_ROOT,
        override_enum_values={"ClaimStatus": {"DRAFT", "APPROVED", "ROGUE_UNREGISTERED_STATE"}},
    )
    assert violations
    codes = {v.code for v in violations}
    assert "SQL_ENUM_DRIFT" in codes
    assert "OPENAPI_ENUM_DRIFT" in codes
    assert all("notion.com" in v.authority for v in violations)
    assert any("ROGUE_UNREGISTERED_STATE" in v.message for v in violations)


def test_failure_injection_openapi_and_m0_drift_detected_without_mutating_files(
    tmp_path: Path,
) -> None:
    # Build an isolated miniature repo tree in tmp_path
    shutil.copytree(REPO_ROOT / "contracts", tmp_path / "contracts")
    shutil.copytree(REPO_ROOT / "db", tmp_path / "db")

    # 1. Corrupt integrated OpenAPI snapshot -> drift detected with diff, file untouched
    openapi_copy = tmp_path / "contracts" / "openapi" / "career-memory-v1.openapi.json"
    drifted_content = openapi_copy.read_text(encoding="utf-8") + "\n"
    openapi_copy.write_text(drifted_content, encoding="utf-8")

    violations = check_artifact_drift(tmp_path)
    drift_hits = [v for v in violations if v.code == "OPENAPI_SNAPSHOT_DRIFT"]
    assert len(drift_hits) == 1
    assert drift_hits[0].diff != ""
    assert "notion.com" in drift_hits[0].authority
    # Must NOT auto-overwrite the drifted file
    assert openapi_copy.read_text(encoding="utf-8") == drifted_content

    # 2. Run CLI against drifted temp repo -> exits non-zero and still never overwrites file
    proc = subprocess.run(
        [sys.executable, str(CONFORMANCE_CLI), "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "OPENAPI_SNAPSHOT_DRIFT" in proc.stderr
    assert openapi_copy.read_text(encoding="utf-8") == drifted_content

    # 3. Mutate immutable M0 openapi.yaml -> M0_OPENAPI_MUTATED detected
    m0_copy = tmp_path / "contracts" / "import-v1" / "openapi.yaml"
    m0_copy.write_text(m0_copy.read_text(encoding="utf-8") + "# mutated\n", encoding="utf-8")
    m0_violations = check_artifact_drift(tmp_path)
    assert any(v.code == "M0_OPENAPI_MUTATED" for v in m0_violations)


def test_failure_injection_openapi_invariants_catch_project_id_verification_and_byok() -> None:
    base_spec = generate_integrated_openapi_spec()

    # Inject project_id parameter into /v1/search (OD-008 violation)
    spec_project = copy.deepcopy(base_spec)
    spec_project["paths"]["/v1/search"]["get"]["parameters"].append(
        {"name": "project_id", "in": "query", "schema": {"type": "string"}}
    )
    v_proj = validate_openapi_invariants(spec_project)
    assert any(v.code == "OD008_PROJECT_ID_DETECTED" for v in v_proj)

    # Inject writable verification field into ClaimDraftCreateRequest
    spec_verif = copy.deepcopy(base_spec)
    spec_verif["components"]["schemas"]["ClaimDraftCreateRequest"]["properties"]["verification"] = {
        "type": "string"
    }
    v_verif = validate_openapi_invariants(spec_verif)
    assert any(v.code == "PROHIBITED_WRITABLE_VERIFICATION_SCHEMA" for v in v_verif)

    # Inject BYOK route and subscription LLM callback route
    spec_byok = copy.deepcopy(base_spec)
    spec_byok["paths"]["/v1/byok/keys"] = {"post": {"responses": {"200": {"description": "bad"}}}}
    spec_byok["paths"]["/v1/openai/callback"] = {"get": {"responses": {"200": {"description": "bad"}}}}
    v_byok = validate_openapi_invariants(spec_byok)
    byok_codes = [v for v in v_byok if v.code == "PROHIBITED_OPENAPI_ROUTE"]
    assert len(byok_codes) == 2


def test_failure_injection_db_catalog_snapshot_catches_rls_fk_plan_c_and_post_mvp() -> None:
    snapshot_path = (
        REPO_ROOT / "contracts" / "conformance" / "expected_db_catalog_snapshot.json"
    )
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))

    # 1. Disable FORCE RLS on facts
    no_force_rls = copy.deepcopy(expected)
    no_force_rls["tables"]["facts"]["forcerowsecurity"] = False
    drifts_rls = diff_db_catalog_snapshots(expected, no_force_rls)
    assert any(d.code == "MISSING_FORCE_RLS" and "facts" in d.object_name for d in drifts_rls)

    # 2. Drop Composite FK on claims -> work_events
    missing_fk = copy.deepcopy(expected)
    missing_fk["tables"]["claims"]["foreign_keys"] = [
        fk
        for fk in missing_fk["tables"]["claims"]["foreign_keys"]
        if fk.get("parent_table") != "work_events"
    ]
    drifts_fk = diff_db_catalog_snapshots(expected, missing_fk)
    assert any(d.code == "MISSING_COMPOSITE_FK" and "claims" in d.object_name for d in drifts_fk)

    # 3. Drop Plan C constraint on content_units
    missing_plan_c = copy.deepcopy(expected)
    del missing_plan_c["tables"]["content_units"]["check_constraints"][
        "ck_content_units_text_content_null"
    ]
    drifts_pc = diff_db_catalog_snapshots(expected, missing_plan_c)
    assert any(d.code == "MISSING_PLAN_C_CONSTRAINT" for d in drifts_pc)

    # 4. Inject writable verification column on claims
    bad_claims = copy.deepcopy(expected)
    bad_claims["tables"]["claims"]["columns"].append("verification")
    drifts_verif = diff_db_catalog_snapshots(expected, bad_claims)
    assert any(d.code == "PROHIBITED_CLAIMS_VERIFICATION_COLUMN" for d in drifts_verif)

    # 5. Inject Post-MVP embeddings table
    post_mvp = copy.deepcopy(expected)
    post_mvp["tables"]["embeddings"] = {
        "owner": "cm_migrator",
        "rowsecurity": True,
        "forcerowsecurity": True,
        "columns": ["id", "workspace_id"],
        "check_constraints": {},
        "foreign_keys": [],
        "policies": [{"name": "emb_pol", "cmd": "ALL", "roles": ["cm_api_app"]}],
        "indexes": [],
    }
    drifts_post = diff_db_catalog_snapshots(expected, post_mvp)
    assert any(d.code == "PROHIBITED_POST_MVP_TABLE" and d.object_name == "embeddings" for d in drifts_post)


@pytest.mark.postgres
def test_real_pg18_fresh_and_staged_upgrade_0010_through_0040_catalog_conformance() -> None:
    fresh_snapshot, staged_snapshot = run_fresh_and_staged_pg18_catalog_smoke(
        REPO_ROOT / "db" / "migrations"
    )
    assert fresh_snapshot == staged_snapshot

    tables = fresh_snapshot["tables"]
    assert len(tables) == 46
    assert all(t["rowsecurity"] and t["forcerowsecurity"] for t in tables.values())
    assert "verification" not in tables["claims"]["columns"]
    assert len(M1_REQUIRED_COMPOSITE_FKS) == 62
    assert len(REQUIRED_PLAN_C_CONSTRAINTS) == 51
    assert len(CRITICAL_PARTIAL_UNIQUE_INDEXES) == 10

    violations = check_postgres_catalog_conformance(
        REPO_ROOT,
        fresh_snapshot=fresh_snapshot,
        staged_snapshot=staged_snapshot,
    )
    assert violations == []
