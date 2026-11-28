"""Tests for GitHub Actions CI trust boundary auditor (INF-09) and T-507 evidence contract."""

from __future__ import annotations

import json
from pathlib import Path

from career_memory_api.edge_guard import (
    HOSTED_PUBLIC_PILOT_GATE_DEFAULT,
    M0_IMMUTABLE_OPENAPI_BLOB_SHA,
    RouteTrustClass,
    audit_pr_workflow_text,
    audit_workflow_file,
    audit_workflows_directory,
    classify_route,
    validate_release_workflow_manifest,
)
from career_memory_api.auth.redaction import SENSITIVE_KEY_NAMES
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
EVIDENCE_PATH = REPO_ROOT / "contracts" / "edge-guard" / "t507_route_trust_matrix.v1.json"


def test_repo_pr_conformance_workflow_passes_inf09_audit() -> None:
    results = audit_workflows_directory(WORKFLOWS_DIR)
    assert len(results) >= 1

    pr_wf = audit_workflow_file(WORKFLOWS_DIR / "ci.yml")
    assert pr_wf.passed is True, f"Unexpected INF-09 findings: {pr_wf.findings}"
    assert pr_wf.top_level_permissions == {"contents": "read"}
    assert "pull_request" in pr_wf.triggers
    assert "pull_request_target" not in pr_wf.triggers
    assert pr_wf.openapi_pin_present is True
    assert pr_wf.secret_references == ()


def test_ci_audit_rejects_write_all_id_token_write_and_missing_permissions() -> None:
    wf_write_all = f"""
name: Bad Permissions
on:
  pull_request:
permissions: write-all
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "{M0_IMMUTABLE_OPENAPI_BLOB_SHA} contracts/import-v1/openapi.yaml"
"""
    res_write_all = audit_pr_workflow_text(wf_write_all, workflow_name="bad1.yml")
    assert res_write_all.passed is False
    assert any(f.rule_id == "INF09_PERMISSIONS_BROAD_SHORTHAND" for f in res_write_all.findings)

    wf_id_token = f"""
name: Bad OIDC on PR
on:
  pull_request:
permissions:
  contents: read
  id-token: write
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "{M0_IMMUTABLE_OPENAPI_BLOB_SHA} contracts/import-v1/openapi.yaml"
"""
    res_id_token = audit_pr_workflow_text(wf_id_token, workflow_name="bad2.yml")
    assert res_id_token.passed is False
    assert any(
        f.rule_id == "INF09_WRITE_PERMISSION_FORBIDDEN_ON_PR" for f in res_id_token.findings
    )

    wf_missing_perms = f"""
name: Missing Permissions
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "{M0_IMMUTABLE_OPENAPI_BLOB_SHA} contracts/import-v1/openapi.yaml"
"""
    res_missing = audit_pr_workflow_text(wf_missing_perms, workflow_name="bad3.yml")
    assert res_missing.passed is False
    assert any(f.rule_id == "INF09_PERMISSIONS_MISSING" for f in res_missing.findings)


def test_ci_audit_rejects_pull_request_target_cloud_secrets_missing_pin_and_pr_injection() -> None:
    wf_prt = f"""
name: Unsafe PRT
on:
  pull_request_target:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
      - run: echo "{M0_IMMUTABLE_OPENAPI_BLOB_SHA} contracts/import-v1/openapi.yaml"
"""
    res_prt = audit_pr_workflow_text(wf_prt, workflow_name="prt.yml")
    assert res_prt.passed is False
    assert any(
        f.rule_id == "INF09_PULL_REQUEST_TARGET_FORBIDDEN" for f in res_prt.findings
    )

    for secret_name in (
        "GCP_SA_KEY",
        "CLOUDFLARE_API_TOKEN",
        "SUPABASE_SERVICE_ROLE_KEY",
        "OPENAI_API_KEY",
    ):
        wf_secret = f"""
name: Secret Leak
on:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      LEAK: ${{{{ secrets.{secret_name} }}}}
    steps:
      - run: echo "{M0_IMMUTABLE_OPENAPI_BLOB_SHA} contracts/import-v1/openapi.yaml"
"""
        res_sec = audit_pr_workflow_text(wf_secret, workflow_name=f"sec-{secret_name}.yml")
        assert res_sec.passed is False
        assert secret_name in res_sec.secret_references

    # Missing immutable M0 OpenAPI blob pin
    wf_no_pin = """
name: No Pin
on:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "no openapi check"
"""
    res_no_pin = audit_pr_workflow_text(wf_no_pin, workflow_name="nopin.yml")
    assert res_no_pin.passed is False
    assert any(
        f.rule_id == "INF09_IMMUTABLE_OPENAPI_PIN_MISSING" for f in res_no_pin.findings
    )

    # Untrusted PR title shell injection
    wf_inject = f"""
name: Shell Injection
on:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{{{ github.event.pull_request.title }}}}"
      - run: echo "{M0_IMMUTABLE_OPENAPI_BLOB_SHA} contracts/import-v1/openapi.yaml"
"""
    res_inj = audit_pr_workflow_text(wf_inject, workflow_name="inject.yml")
    assert res_inj.passed is False
    assert any(
        f.rule_id == "INF09_UNTRUSTED_PR_EXPRESSION_INJECTION" for f in res_inj.findings
    )


def test_release_workflow_manifest_validator_enforces_digest_pinning_concurrency_and_wif() -> None:
    valid_digest = "a" * 64
    valid_sha40 = "b" * 40
    valid_release = f"""
name: Staging Release (Manual Only)
on:
  workflow_dispatch:
permissions:
  contents: read
  id-token: write
concurrency:
  group: staging-deploy-api
  cancel-in-progress: false
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{valid_sha40}
      - run: >-
          gcloud run deploy cm-api
          --image asia-northeast3-docker.pkg.dev/cm-proj/api/cm-api@sha256:{valid_digest}
"""
    ok_res = validate_release_workflow_manifest(valid_release)
    assert ok_res.passed is True, f"Unexpected findings: {ok_res.findings}"
    assert ok_res.has_concurrency_group is True
    assert ok_res.cancel_in_progress is False
    assert len(ok_res.image_references) == 1

    # Mutable tag + cancel-in-progress: true + static GCP_SA_KEY + pull_request trigger -> MUST FAIL
    bad_release = """
name: Unsafe Release
on:
  pull_request:
permissions: write-all
concurrency:
  group: staging-deploy-api
  cancel-in-progress: true
jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      KEY: ${{ secrets.GCP_SA_KEY }}
    steps:
      - uses: actions/checkout@main
      - run: gcloud run deploy cm-api --image asia-northeast3-docker.pkg.dev/cm-proj/api/cm-api:latest
"""
    bad_res = validate_release_workflow_manifest(bad_release)
    assert bad_res.passed is False
    rule_ids = {f.rule_id for f in bad_res.findings}
    assert "INF09_RELEASE_PR_TRIGGER_FORBIDDEN" in rule_ids
    assert "INF09_RELEASE_PERMISSIONS_BROAD" in rule_ids
    assert "INF09_RELEASE_CANCEL_IN_PROGRESS_MUST_BE_FALSE" in rule_ids
    assert "INF09_STATIC_GCP_SA_KEY_FORBIDDEN" in rule_ids
    assert "INF09_IMAGE_DIGEST_PIN_REQUIRED" in rule_ids
    assert "INF09_ACTION_COMMIT_SHA_PIN_REQUIRED" in rule_ids


def test_repo_staging_deploy_workflow_passes_release_manifest_validation() -> None:
    staging_path = WORKFLOWS_DIR / "cd.yml"
    assert staging_path.is_file(), f"Missing CD deploy workflow: {staging_path}"
    content = staging_path.read_text(encoding="utf-8")
    res = validate_release_workflow_manifest(
        content,
        workflow_name=staging_path.name,
    )
    assert res.passed is True, f"Unexpected INF-09 release findings: {res.findings}"
    assert res.has_concurrency_group is True
    assert res.cancel_in_progress is False
    assert len(res.image_references) >= 1
    assert "environment: staging" in content
    assert "secrets.STAGING_MIGRATION_DATABASE_URL" in content
    assert "--set-secrets" in content


def test_t507_route_trust_matrix_evidence_contract_is_redacted_and_consistent() -> None:
    assert EVIDENCE_PATH.is_file(), f"Missing evidence file: {EVIDENCE_PATH}"
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))

    # Must contain zero raw-text keys and zero secret keys
    assert_no_forbidden_raw_keys(evidence, field_name="t507_route_trust_matrix.v1.json")
    stack: list[object] = [evidence]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for k, v in current.items():
                assert str(k).strip().lower() not in SENSITIVE_KEY_NAMES
                stack.append(v)
        elif isinstance(current, list):
            stack.extend(current)

    assert evidence["task_id"] == "T-507"
    assert evidence["issue_number"] == 26
    assert evidence["external_cloud_calls"] == 0
    assert evidence["hosted_public_pilot_gate"] == HOSTED_PUBLIC_PILOT_GATE_DEFAULT
    assert evidence["openapi_import_v1_blob_sha"] == M0_IMMUTABLE_OPENAPI_BLOB_SHA
    assert evidence["ci_trust_audit"]["passed"] is True

    # Verify every documented route example matches live classify_route()
    for entry in evidence["route_trust_matrix"]:
        expected_class = RouteTrustClass(entry["trust_class"])
        for sample in entry["sample_routes"]:
            method, path = sample.split(" ", 1)
            actual = classify_route(method, path)
            assert actual.trust_class is expected_class, (
                f"Evidence drift on {sample}: expected {expected_class}, got {actual.trust_class}"
            )

    # Verify all INF-07 and INF-09 verification cases are marked passed with 0 external calls
    cases = evidence["verification_cases"]
    assert len(cases) >= 12
    for case in cases:
        assert case["passed"] is True
        assert case["external_cloud_calls"] == 0
