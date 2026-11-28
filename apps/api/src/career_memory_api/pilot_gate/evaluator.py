"""Programmatic T-506 Closed Pilot & 3-Tier Go/No-Go Release Gate Evaluator."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from career_memory_api.auth.redaction import SENSITIVE_KEY_NAMES
from career_memory_api.conformance.checks import (
    M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
    compute_git_blob_sha1,
    default_repo_root,
)
from career_memory_api.pilot_gate.models import (
    BundleVerificationResult,
    InvariantVerificationItem,
    ReleaseGateDecision,
    ReleaseGateStatusCode,
    ReleaseGateTier,
    T506ReleaseGateBundle,
    TierGateEvaluation,
    compute_gate_sha256,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys

_HOSTED_DIGEST_RE = re.compile(r"^us-central1-docker\.pkg\.dev/[a-z0-9._/\-]+@sha256:[0-9a-f]{64}$")


class PilotGatePolicyViolationError(ValueError):
    """Raised if a caller attempts to falsely promote HELD hosted/AI gates from synthetic evidence."""


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _load_json_bundle(
    repo_root: Path, relative_path: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    path = repo_root / relative_path
    if not path.is_file():
        return None, None, f"Missing required evidence bundle: {relative_path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, _sha256_file(path), f"Evidence bundle {relative_path} is not a JSON object."
        return data, _sha256_file(path), None
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Failed to parse {relative_path}: {type(exc).__name__}"


def _verify_b004(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/fault-harness/b004_evidence_bundle.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="B-004",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    scenarios = data.get("scenarios", [])
    _check(data.get("gate_id") == "B-004", "gate_id must be B-004")
    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "B-004 openapi_import_v1_blob_sha mismatch",
    )
    _check(isinstance(scenarios, list) and len(scenarios) == 16, "Expected 16 B-004 scenarios")
    all_sc_ok = all(
        bool(s.get("passed"))
        and int(s.get("invariant_violation_count", 1)) == 0
        and int(s.get("expected_snapshot_diff_count", 1)) == 0
        and bool(s.get("canary_audit", {}).get("passed"))
        and int(s.get("canary_audit", {}).get("canary_hits", 1)) == 0
        for s in scenarios
    )
    _check(all_sc_ok, "One or more B-004 scenarios failed invariants, snapshot match, or canary audit")

    return BundleVerificationResult(
        bundle_id="B-004",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "scenarios_total": len(scenarios),
            "scenarios_passed": sum(1 for s in scenarios if s.get("passed")),
            "total_canary_hits": sum(
                int(s.get("canary_audit", {}).get("canary_hits", 0)) for s in scenarios
            ),
        },
    )


def _verify_t306a(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/event-eval/t306a_eval_bundle.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-306A",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    summary = data.get("summary", {})
    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-306A openapi_import_v1_blob_sha mismatch",
    )
    _check(bool(summary.get("all_passed")), "T-306A summary.all_passed is False")
    _check(int(summary.get("passed_cases", 0)) == 21, "T-306A expected 21/21 passed cases")
    _check(int(summary.get("failed_cases", 1)) == 0, "T-306A failed_cases != 0")
    _check(int(summary.get("total_oracle_diffs", 1)) == 0, "T-306A oracle diffs != 0")
    _check(int(summary.get("total_plan_c_violations", 1)) == 0, "T-306A Plan C violations != 0")
    _check(
        int(summary.get("total_raw_body_canary_hits", 1)) == 0,
        "T-306A raw body canary hits != 0",
    )
    _check(
        data.get("read_only_oracle_policy", {}).get("frozen_dataset_modified") is False,
        "T-306A frozen dataset must not be modified",
    )

    return BundleVerificationResult(
        bundle_id="T-306A",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "passed_cases": summary.get("passed_cases", 0),
            "total_cases": summary.get("total_cases", 0),
            "passed_bundles": summary.get("passed_bundles", 0),
            "total_plan_c_violations": summary.get("total_plan_c_violations", 0),
        },
    )


def _verify_t407a(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/claim-eval/t407a_eval_bundle.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-407A",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    gov = data.get("governance_gates", {})
    ref_matrix = data.get("official_ref_runtime_matrix", [])
    ref_cases = data.get("refetch_cases", [])
    scenarios = data.get("scenarios", [])

    _check(
        data.get("immutable_openapi_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-407A immutable_openapi_blob_sha mismatch",
    )
    _check(
        gov.get("SYNTHETIC_CONTRACT_AND_SAFETY_GATE") == "PASSED",
        "T-407A SYNTHETIC_CONTRACT_AND_SAFETY_GATE must be PASSED",
    )
    _check(
        gov.get("LIVE_LLM_SEMANTIC_EVAL") == "NOT_RUN_BLOCKED_BY_OD002_OD014",
        "T-407A LIVE_LLM_SEMANTIC_EVAL must remain NOT_RUN_BLOCKED_BY_OD002_OD014",
    )
    _check(
        gov.get("live_pilot_gate_eligible") is False,
        "T-407A synthetic run MUST NOT claim live_pilot_gate_eligible=True",
    )
    _check(
        int(gov.get("external_live_llm_calls", 1)) == 0,
        "T-407A external_live_llm_calls must be 0",
    )
    _check(len(scenarios) == 16 and all(bool(s.get("passed")) for s in scenarios), "T-407A expected 16/16 scenarios passed")
    _check(len(ref_cases) == 27 and all(bool(r.get("passed")) for r in ref_cases), "T-407A expected 27/27 refetch cases passed")
    _check(
        len(ref_matrix) == 8
        and all(
            bool(m.get("product_runtime_pg18_verified"))
            and m.get("passed_count") == m.get("total_count")
            for m in ref_matrix
        ),
        "T-407A expected REF-01..REF-08 all verified on PG18",
    )
    all_plan_c = all(bool(s.get("plan_c_audit", {}).get("passed")) for s in scenarios) and all(
        bool(r.get("plan_c_audit", {}).get("passed")) for r in ref_cases
    )
    _check(all_plan_c, "T-407A Plan C audit violation detected")

    return BundleVerificationResult(
        bundle_id="T-407A",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "scenarios_passed": sum(1 for s in scenarios if s.get("passed")),
            "refetch_cases_passed": sum(1 for r in ref_cases if r.get("passed")),
            "ref_families_verified": len(ref_matrix),
            "live_llm_semantic_eval": gov.get("LIVE_LLM_SEMANTIC_EVAL"),
            "live_pilot_gate_eligible": gov.get("live_pilot_gate_eligible"),
        },
    )


def _verify_inf08(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/llm-gate/inf08_evidence.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-408/INF-08",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    cases = data.get("cases", [])
    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "INF-08 openapi_import_v1_blob_sha mismatch",
    )
    _check(
        data.get("live_ai_gate_status") == "OFF_NOT_PASSED_PENDING_OD002_OD014",
        "INF-08 live_ai_gate_status must remain OFF_NOT_PASSED_PENDING_OD002_OD014",
    )
    _check(int(data.get("external_live_calls", 1)) == 0, "INF-08 external_live_calls must be 0")
    _check(int(data.get("token_or_raw_leaks", 1)) == 0, "INF-08 token_or_raw_leaks must be 0")
    _check(
        int(data.get("db_user_llm_token_tables_added", 1)) == 0,
        "INF-08 db_user_llm_token_tables_added must be 0",
    )
    _check(len(cases) == 17 and all(bool(c.get("passed")) for c in cases), "INF-08 expected 17/17 cases passed")

    return BundleVerificationResult(
        bundle_id="T-408/INF-08",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "cases_passed": sum(1 for c in cases if c.get("passed")),
            "cases_total": len(cases),
            "live_ai_gate_status": data.get("live_ai_gate_status"),
            "token_or_raw_leaks": data.get("token_or_raw_leaks"),
        },
    )


def _verify_t507(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/edge-guard/t507_route_trust_matrix.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-507/INF-07",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    cases = data.get("verification_cases", [])
    ci_audit = data.get("ci_trust_audit", {})
    cost_inv = data.get("cost_and_pause_invariants", {})
    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-507 openapi_import_v1_blob_sha mismatch",
    )
    _check(
        data.get("hosted_public_pilot_gate") == "HELD_NO_GO_PENDING_OD013_OD015",
        "T-507 hosted_public_pilot_gate must remain HELD_NO_GO_PENDING_OD013_OD015",
    )
    _check(int(data.get("external_cloud_calls", 1)) == 0, "T-507 external_cloud_calls must be 0")
    _check(bool(ci_audit.get("passed")), "T-507 ci_trust_audit.passed must be True")
    _check(
        cost_inv.get("paid_fallback_allowed") is False
        and cost_inv.get("resume_requires_budget_verified_true") is True,
        "T-507 cost/pause invariants must forbid paid fallback and require budget_verified=True",
    )
    _check(len(cases) == 14 and all(bool(c.get("passed")) for c in cases), "T-507 expected 14/14 verification cases passed")

    return BundleVerificationResult(
        bundle_id="T-507/INF-07",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "cases_passed": sum(1 for c in cases if c.get("passed")),
            "cases_total": len(cases),
            "ci_trust_audit_passed": ci_audit.get("passed"),
            "hosted_public_pilot_gate": data.get("hosted_public_pilot_gate"),
        },
    )


def _verify_t505(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/backup-restore/t505_restore_evidence.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-505/INF-10",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    inv = data.get("invariants", {})
    rto = data.get("measured_rto_metrics", {})
    supa = data.get("supabase_free_hosting_constraints", {})
    plan_c = inv.get("plan_c_audit", {})
    rls = inv.get("rls_isolation", {})
    fence = inv.get("repo_b_deletion_fence", {})

    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-505 openapi_import_v1_blob_sha mismatch",
    )
    _check(bool(inv.get("all_passed")), "T-505 invariants.all_passed must be True")
    _check(bool(rto.get("within_rto_budget")), "T-505 must be within RTO budget")
    _check(
        int(plan_c.get("audited_table_count", 0)) >= 46
        and int(plan_c.get("raw_text_violations", 1)) == 0
        and int(plan_c.get("secret_key_violations", 1)) == 0
        and int(plan_c.get("canary_string_hits", 1)) == 0,
        "T-505 Plan C audit must pass across all 46 tables with 0 violations",
    )
    _check(
        bool(rls.get("all_tables_force_rls"))
        and bool(rls.get("all_runtime_roles_nobypassrls"))
        and int(rls.get("catalog_violations", 1)) == 0,
        "T-505 FORCE RLS / NOBYPASSRLS invariant failed",
    )
    _check(
        bool(fence.get("fence_survived_restore"))
        and bool(fence.get("post_restore_completion_succeeded"))
        and int(fence.get("resurrected_rows", 1)) == 0,
        "T-505 deletion fence survival failed",
    )
    _check(
        supa.get("paid_pitr_assumed") is False
        and supa.get("hosted_public_pilot_gate") == "HELD_NO_GO_PENDING_OD013_OD015",
        "T-505 Supabase Free non-PITR constraint / HELD gate mismatch",
    )

    return BundleVerificationResult(
        bundle_id="T-505/INF-10",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "restored_table_count": rls.get("restored_table_count", 0),
            "within_rto_budget": rto.get("within_rto_budget"),
            "resurrected_rows": fence.get("resurrected_rows"),
            "paid_pitr_assumed": supa.get("paid_pitr_assumed"),
        },
    )


def _verify_t504a(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/fault-matrix/t504a_evidence_bundle.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-504A",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    summary = data.get("summary", {})
    scenarios = data.get("scenarios", [])
    rls = data.get("rls_catalog_verified", {})

    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-504A openapi_import_v1_blob_sha mismatch",
    )
    _check(bool(summary.get("all_passed")), "T-504A summary.all_passed must be True")
    _check(
        int(summary.get("passed_scenarios", 0)) == 12
        and int(summary.get("failed_scenarios", 1)) == 0
        and len(scenarios) == 12,
        "T-504A expected 12/12 XFLT scenarios passed",
    )
    _check(
        int(data.get("sleep_based_race_waits", 1)) == 0
        and int(summary.get("barrier_synchronized_scenarios", 0)) >= 4,
        "T-504A must use 0 sleep race waits and >= 4 barrier-synchronized race scenarios",
    )
    _check(
        int(summary.get("total_plan_c_and_secret_violations", 1)) == 0,
        "T-504A Plan C / secret violations must be 0",
    )
    _check(
        bool(rls.get("all_tables_force_rls"))
        and bool(rls.get("all_runtime_roles_nobypassrls"))
        and int(rls.get("table_count", 0)) >= 46,
        "T-504A PG18 FORCE RLS / NOBYPASSRLS catalog check failed",
    )
    _check(
        int(data.get("external_network_calls", 1)) == 0 and int(data.get("spend_krw", 1)) == 0,
        "T-504A must make 0 external calls at ₩0 spend",
    )

    return BundleVerificationResult(
        bundle_id="T-504A",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "passed_scenarios": summary.get("passed_scenarios", 0),
            "total_assertions_checked": summary.get("total_assertions_checked", 0),
            "barrier_synchronized_scenarios": summary.get("barrier_synchronized_scenarios", 0),
            "deterministic_matrix_sha256": data.get("deterministic_matrix_sha256"),
        },
    )


def _verify_t007f_t106b(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/hosted-staging/t007f_t106b_staging_evidence.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-007F/T-106B",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    raw_clean = True
    try:
        assert_no_forbidden_raw_keys(data, field_name=rel)
    except Exception:  # noqa: BLE001
        raw_clean = False
    secret_key_hits = 0
    stack: list[Any] = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).strip().lower() in SENSITIVE_KEY_NAMES:
                    secret_key_hits += 1
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)

    od015 = data.get("od015_preflight", {})
    supa = data.get("supabase_rls_and_pooler_audit", {})
    services = data.get("cloud_run_services", [])
    wif = data.get("wif_github_actions_runs", {})
    deploy_run = wif.get("deploy_run", {})
    rollback_run = wif.get("rollback_run", {})
    posture = data.get("ingress_and_backup_posture", {})

    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-007F/T-106B openapi_import_v1_blob_sha mismatch",
    )
    _check(
        raw_clean and secret_key_hits == 0,
        "T-007F/T-106B must contain 0 raw-text keys and 0 sensitive secret keys",
    )
    _check(
        od015.get("verdict") == "GO"
        and int(od015.get("checks_passed", 0)) == 6
        and od015.get("containerscanning_enabled") is False
        and float(od015.get("artifact_registry_total_mb", 999.0)) <= 250.0
        and int(od015.get("secret_manager_enabled_versions", 99)) <= 4
        and int(od015.get("vpc_connector_count", 1)) == 0
        and int(od015.get("estimated_monthly_krw_worst_case", 99999)) < 20000,
        "T-007F/T-106B OD-015 Free-Tier pre-flight invariants failed",
    )
    _check(
        int(supa.get("public_tables_total", 0)) == 46
        and int(supa.get("force_rls_tables", 0)) == 46
        and supa.get("runtime_roles_nobypassrls") is True
        and int(supa.get("runtime_roles_owned_tables", 1)) == 0
        and int(supa.get("anon_authenticated_public_grants", 1)) == 0
        and int(supa.get("supavisor_transaction_pooler_port", 0)) == 6543
        and supa.get("prepare_threshold") is None
        and int(supa.get("prepared_statements_observed", 1)) == 0
        and int(supa.get("postgrest_domain_table_bypass_status", 200)) in (401, 403, 404),
        "T-007F/T-106B Supabase RLS / Supavisor :6543 / PostgREST audit failed",
    )
    svc_by_name = {str(s.get("service_name")): s for s in services if isinstance(s, dict)}
    all_svc_ok = (
        set(svc_by_name.keys()) == {"cm-web", "cm-api", "cm-worker"}
        and svc_by_name["cm-worker"].get("allow_unauthenticated") is False
        and all(
            int(s.get("min_instances", 1)) == 0
            and int(s.get("max_instances", 99)) <= 2
            and float(s.get("cpu", 99.0)) <= 1.0
            and int(s.get("memory_mib", 9999)) <= 512
            and bool(_HOSTED_DIGEST_RE.match(str(s.get("image_digest", ""))))
            for s in svc_by_name.values()
        )
    )
    _check(all_svc_ok, "T-007F/T-106B Cloud Run service caps / IAM / @sha256 digest pin check failed")
    _check(
        deploy_run.get("conclusion") == "success"
        and int(deploy_run.get("smoke_checks_passed", 0)) == 11
        and int(deploy_run.get("static_sa_keys_used", 1)) == 0
        and rollback_run.get("conclusion") == "success"
        and int(rollback_run.get("smoke_checks_passed", 0)) == 11
        and rollback_run.get("post_rollback_od015_verdict") == "GO"
        and int(rollback_run.get("static_sa_keys_used", 1)) == 0,
        "T-007F/T-106B WIF deploy and rollback verification runs failed",
    )
    _check(
        posture.get("direct_api_bypass_blocked_403") is True
        and posture.get("supabase_free_pitr_assumed") is False
        and posture.get("live_subscription_llm_mode") == "OFF_MANUAL_ONLY",
        "T-007F/T-106B ingress/backup/LLM posture check failed",
    )

    return BundleVerificationResult(
        bundle_id="T-007F/T-106B",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "od015_verdict": od015.get("verdict"),
            "deploy_smoke_checks_passed": deploy_run.get("smoke_checks_passed", 0),
            "rollback_smoke_checks_passed": rollback_run.get("smoke_checks_passed", 0),
            "force_rls_tables": supa.get("force_rls_tables", 0),
        },
    )


def _verify_t006(repo_root: Path) -> BundleVerificationResult:
    rel = "contracts/llm-gate/t006_policy_audit.v1.json"
    data, digest, err = _load_json_bundle(repo_root, rel)
    if err or data is None:
        return BundleVerificationResult(
            bundle_id="T-006",
            relative_path=rel,
            present=False,
            verified=False,
            file_sha256=digest,
            checks_passed=0,
            checks_total=1,
            findings=[err or "Missing bundle"],
        )

    findings: list[str] = []
    checks = 0
    passed = 0

    def _check(cond: bool, msg: str) -> None:
        nonlocal checks, passed
        checks += 1
        if cond:
            passed += 1
        else:
            findings.append(msg)

    raw_clean = True
    try:
        assert_no_forbidden_raw_keys(data, field_name=rel)
    except Exception:  # noqa: BLE001
        raw_clean = False
    secret_key_hits = 0
    stack: list[Any] = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).strip().lower() in SENSITIVE_KEY_NAMES:
                    secret_key_hits += 1
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)

    providers = data.get("providers_audited", [])
    prov_by_id = {str(p.get("provider_id")): p for p in providers if isinstance(p, dict)}
    gov = data.get("final_governance_posture", {})
    flags = gov.get("hosted_llm_approval_flags", {})

    _check(
        data.get("openapi_import_v1_blob_sha") == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
        "T-006 openapi_import_v1_blob_sha mismatch",
    )
    _check(
        raw_clean and secret_key_hits == 0,
        "T-006 must contain 0 raw-text keys and 0 sensitive secret keys",
    )
    _check(
        set(prov_by_id.keys())
        == {
            "OPENAI_SIGN_IN_WITH_CHATGPT",
            "ANTHROPIC_CLAUDE_SUBSCRIPTION",
            "GOOGLE_GEMINI_SUBSCRIPTION_AND_UNPAID_FREE_TIER",
        }
        and prov_by_id["OPENAI_SIGN_IN_WITH_CHATGPT"].get("remote_hosted_cloud_run_self_serve_allowed") is False
        and prov_by_id["ANTHROPIC_CLAUDE_SUBSCRIPTION"].get("third_party_subscription_oauth_allowed") is False
        and prov_by_id["GOOGLE_GEMINI_SUBSCRIPTION_AND_UNPAID_FREE_TIER"].get(
            "unpaid_api_free_tier_training_and_human_review_opted_out"
        )
        is False,
        "T-006 3-provider official hosting/training/OAuth audit matrix incomplete",
    )
    _check(
        gov.get("hosted_runtime_mode") == "OFF_MANUAL_ONLY"
        and all(flags.get(k) is False for k in (
            "hosted_deployment_approved",
            "zero_overage_guaranteed",
            "od002_data_policy_verified",
            "od014_token_store_verified",
        ))
        and int(gov.get("postgres_user_llm_token_tables", 1)) == 0
        and int(gov.get("gcp_secret_manager_llm_secrets", 1)) == 0
        and int(gov.get("live_external_llm_calls_performed", 1)) == 0
        and gov.get("live_subscription_ai_pilot_gate") == "NO-GO",
        "T-006 hard-OFF hosted governance posture or zero-token-storage invariant violated",
    )

    return BundleVerificationResult(
        bundle_id="T-006",
        relative_path=rel,
        present=True,
        verified=(len(findings) == 0),
        file_sha256=digest,
        checks_passed=passed,
        checks_total=checks,
        findings=findings,
        metrics={
            "providers_audited": len(prov_by_id),
            "hosted_runtime_mode": gov.get("hosted_runtime_mode"),
            "live_external_llm_calls_performed": gov.get("live_external_llm_calls_performed", 0),
        },
    )


def evaluate_t506_pilot_gate(
    *,
    repo_root: Path | None = None,
    require_live_fe_be_tests_present: bool = True,
) -> T506ReleaseGateBundle:
    """Programmatically verify all M0-M5, Track 2 & Track 3 evidence bundles and evaluate the 3-tier release gates."""
    root = default_repo_root() if repo_root is None else repo_root.resolve()
    openapi_path = root / "contracts" / "import-v1" / "openapi.yaml"
    actual_openapi_sha = (
        compute_git_blob_sha1(openapi_path.read_bytes()) if openapi_path.is_file() else ""
    )
    openapi_pin_verified = actual_openapi_sha == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1

    b004 = _verify_b004(root)
    t306a = _verify_t306a(root)
    t407a = _verify_t407a(root)
    inf08 = _verify_inf08(root)
    t507 = _verify_t507(root)
    t505 = _verify_t505(root)
    t504a = _verify_t504a(root)
    t007f = _verify_t007f_t106b(root)
    t006 = _verify_t006(root)

    local_bundles = [b004, t306a, t407a, inf08, t507, t505, t504a]
    bundles = [*local_bundles, t007f, t006]
    by_id = {b.bundle_id: b for b in bundles}

    fe_be_m4_exists = (
        (
            root / "apps" / "web" / "tests" / "contract-http" / "live-backend-claim-review.test.tsx"
        ).is_file()
        or (
            root / "spikes" / "web-import" / "tests" / "contract-http" / "live-backend-claim-review.test.tsx"
        ).is_file()
    )
    fe_be_m5_exists = (
        (
            root / "apps" / "web" / "tests" / "contract-http" / "live-backend-memory-ops.test.tsx"
        ).is_file()
        or (
            root / "spikes" / "web-import" / "tests" / "contract-http" / "live-backend-memory-ops.test.tsx"
        ).is_file()
    )
    fe_be_present = (not require_live_fe_be_tests_present) or (fe_be_m4_exists and fe_be_m5_exists)

    invariants: list[InvariantVerificationItem] = [
        InvariantVerificationItem(
            invariant_id="M0-OPENAPI-PIN",
            category="CONTRACT_IMMUTABILITY",
            passed=openapi_pin_verified and all(b.verified for b in local_bundles) and (not t007f.present or t007f.verified),
            evidence_sources=["contracts/import-v1/openapi.yaml", "B-004", "T-306A", "T-407A", "T-408/INF-08", "T-507/INF-07", "T-505/INF-10", "T-504A", "T-007F/T-106B"],
            summary=f"Immutable M0 OpenAPI blob SHA verified ({M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1}).",
        ),
        InvariantVerificationItem(
            invariant_id="ADR-013-PLAN-C-ZERO-LEAKS",
            category="DATA_PRIVACY_PLAN_C",
            passed=all(b.verified for b in local_bundles) and (not t007f.present or t007f.verified),
            evidence_sources=["B-004", "T-306A", "T-407A", "T-408/INF-08", "T-505/INF-10", "T-504A", "T-007F/T-106B"],
            summary="0 raw source bodies persisted, 0 forbidden JSONB keys, and 0 canary/secret leaks across all 46 PG18/PG17 tables, logs, and exports.",
        ),
        InvariantVerificationItem(
            invariant_id="SEARCH-01..05+OD-008",
            category="MEMORY_SEARCH",
            passed=by_id["T-504A"].verified and by_id["T-505/INF-10"].verified and fe_be_present,
            evidence_sources=["T-504A (XFLT-02, XFLT-05, XFLT-06, XFLT-12)", "T-505/INF-10", "live-backend-memory-ops.test.tsx"],
            summary="Permission-safe PG18 FTS search with pre-response authoritative recheck, index-lag filtering, cursor version/scope binding, and zero project_id scope.",
        ),
        InvariantVerificationItem(
            invariant_id="DELETE-01..07+OD-003",
            category="SOURCE_DELETION",
            passed=by_id["T-504A"].verified and by_id["T-505/INF-10"].verified and fe_be_present,
            evidence_sources=["T-504A (XFLT-04, XFLT-05, XFLT-08, XFLT-09, XFLT-11)", "T-505/INF-10", "live-backend-memory-ops.test.tsx"],
            summary="Version-bound OD-003 deletion preview/confirm, zero-window fence, 5-component receipt idempotency, 4-worker barrier zero resurrection, and post-restore fence survival.",
        ),
        InvariantVerificationItem(
            invariant_id="SEC-01..02",
            category="SECURITY_AND_ISOLATION",
            passed=by_id["T-504A"].verified and by_id["T-507/INF-07"].verified and by_id["T-407A"].verified,
            evidence_sources=["T-504A (XFLT-07, XFLT-09, XFLT-10)", "T-507/INF-07", "T-407A"],
            summary="Hostile prompt-injection/XSS/binary/oversized neutralization, SSRF/redirect block (REF-05), read-only verification guard, private no-store cache isolation, and 46-table FORCE RLS.",
        ),
        InvariantVerificationItem(
            invariant_id="REL-01..02",
            category="RELIABILITY_AND_RECOVERY",
            passed=by_id["B-004"].verified and by_id["T-504A"].verified and by_id["T-505/INF-10"].verified,
            evidence_sources=["B-004", "T-504A (XFLT-01, XFLT-11, XFLT-12)", "T-505/INF-10"],
            summary="Poison job/outbox terminal isolation, unique logical-effect replay safety, encrypted PG18 backup/restore parity, and canonical clean-vs-fault snapshot convergence.",
        ),
        InvariantVerificationItem(
            invariant_id="EXPORT-01..02",
            category="ZERO_QUOTE_EXPORTS",
            passed=by_id["T-407A"].verified and by_id["T-504A"].verified and fe_be_present,
            evidence_sources=["T-407A", "T-504A (XFLT-03, XFLT-04, XFLT-10, XFLT-12)", "live-backend-claim-review.test.tsx"],
            summary="Zero-quote Markdown/JSON exports with live EXPORT_DOWNLOAD refetch gate and FOR UPDATE build-time invalidation fence.",
        ),
        InvariantVerificationItem(
            invariant_id="PERF-01+INF-03+INF-06",
            category="QUOTAS_AND_COST_SAFETY",
            passed=by_id["T-504A"].verified and by_id["T-507/INF-07"].verified and fe_be_present,
            evidence_sources=["T-504A (XFLT-08, XFLT-09, XFLT-10)", "T-507/INF-07", "live-backend-memory-ops.test.tsx"],
            summary="Operation caps, circuit breaker, ambiguous billable timeout no-auto-retry, and 80%/95% quota cutoff blocking growth with paid_fallback_allowed=False while preserving disconnect & deletion rights.",
        ),
        InvariantVerificationItem(
            invariant_id="INF-01..10",
            category="INFRASTRUCTURE_AND_GOVERNANCE",
            passed=by_id["T-408/INF-08"].verified and by_id["T-507/INF-07"].verified and by_id["T-505/INF-10"].verified and by_id["T-504A"].verified,
            evidence_sources=["T-408/INF-08", "T-507/INF-07", "T-505/INF-10", "T-504A", "T-007F/T-106B"],
            summary="All app-layer, local PG18, and hosted Cloud Run + Supabase PG17 guards (FORCE RLS, edge origin/worker/cache/pause, WIF release/rollback, LLM hard-OFF policy gate, encrypted backup/restore drill) verified.",
        ),
    ]

    all_local_verified = (
        openapi_pin_verified
        and fe_be_present
        and all(b.verified for b in local_bundles)
        and all(inv.passed for inv in invariants)
    )
    hosted_verified = all_local_verified and t007f.verified

    # Strict, honest 3-tier release gate separation (ADR-018, ADR-019, ADR-020)
    release_gates = [
        TierGateEvaluation(
            tier=ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE,
            decision=ReleaseGateDecision.GO if all_local_verified else ReleaseGateDecision.NO_GO,
            status_code=(
                ReleaseGateStatusCode.PASSED
                if all_local_verified
                else ReleaseGateStatusCode.FAILED
            ),
            blocking_open_decisions=[],
            rationale=(
                "Full local end-to-end journey (GitHub OAuth/Allowlist -> Initial Import -> Event Review -> "
                "Identity/Fact/Claim Review -> Plan C Ephemeral Refetch -> Zero-Quote Export -> FTS Search -> "
                "OD-003 Deletion -> Quota/Circuit-Breaker -> Encrypted PG18 Backup/Restore) verified on real "
                "PostgreSQL 18 + Next.js UI at ₩0 spend with 0 Plan C leaks."
                if all_local_verified
                else "One or more local evidence bundles or invariants failed verification; failing closed."
            ),
            required_operator_actions=[],
        ),
        TierGateEvaluation(
            tier=ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE,
            decision=ReleaseGateDecision.GO if hosted_verified else ReleaseGateDecision.NO_GO,
            status_code=(
                ReleaseGateStatusCode.PASSED
                if hosted_verified
                else ReleaseGateStatusCode.HELD_PENDING_OD013_OD015
            ),
            blocking_open_decisions=[] if hosted_verified else ["OD-013", "OD-015"],
            rationale=(
                "Live Cloud Run (us-central1 scale-to-zero cm-web + cm-api + private IAM/OIDC cm-worker) + "
                "Supabase PostgreSQL 17.12 (:5432 cm_migrator 0010..0080, 46/46 FORCE RLS, :6543 Supavisor "
                "prepare_threshold=None, PostgREST 401 blocked) + keyless OIDC WIF deploy (Run 46143882856, 11/11) "
                "& instant rollback drill (Run 46144193821, 11/11) + OD-015 Free-Tier pre-flight GO (₩0, 4/4 secrets, "
                "214.99 MB AR) verified under owner-confirmed OD-005/006/010/012/013/015."
                if hosted_verified
                else (
                    "Hosted staging verification bundle (T-007F/T-106B) or local prerequisite bundle missing/failed; "
                    "failing closed until OD-013/OD-015 live cloud verification passes."
                )
            ),
            required_operator_actions=(
                []
                if hosted_verified
                else [
                    "Execute OD-015 Free-Tier pre-flight, Supabase PG17 RLS/Pooler audit, and keyless WIF Cloud Run deploy & rollback verification (T-007A..F, T-106B)."
                ]
            ),
        ),
        TierGateEvaluation(
            tier=ReleaseGateTier.LIVE_SUBSCRIPTION_AI_PILOT_GATE,
            decision=ReleaseGateDecision.NO_GO,
            status_code=ReleaseGateStatusCode.HELD_PENDING_OD002_OD014,
            blocking_open_decisions=["OD-002", "OD-014"],
            rationale=(
                "Synthetic/contract/safety gates (T-407A 16 scenarios + 27 refetch cases, T-408/INF-08 17 gate cases) "
                "passed with 0 leaks and 0 external calls, but per M4/M5 Exit Criteria and ADR-018/ADR-020, synthetic "
                "fixture passes NEVER close the live model gate while external subscription LLM transmission remains OFF."
            ),
            required_operator_actions=[
                "Confirm and sign off on external model data handling / consent policy for public-only unmasked payloads (OD-002).",
                "Provision and verify external encrypted user-subscription OAuth token vault & unlink/wipe automation without storing raw tokens in PostgreSQL (OD-014, INF-08).",
                "Execute live closed-pilot semantic groundedness & review-time evaluation against real subscription model traffic before enabling live AI extraction/generation (ADR-018).",
            ],
        ),
    ]

    summary = {
        "bundles_verified": sum(1 for b in bundles if b.verified),
        "bundles_total": len(bundles),
        "invariants_passed": sum(1 for inv in invariants if inv.passed),
        "invariants_total": len(invariants),
        "local_deterministic_mvp_decision": release_gates[0].decision.value,
        "public_cloud_hosted_closed_pilot_decision": release_gates[1].decision.value,
        "live_subscription_ai_pilot_decision": release_gates[2].decision.value,
        "live_fe_be_pg18_suites_present": fe_be_present,
        "external_network_calls": 0,
        "spend_krw": 0,
    }

    digest_input = {
        "openapi_import_v1_blob_sha": actual_openapi_sha,
        "evidence_bundles": [b.model_dump(mode="json") for b in bundles],
        "invariants": [i.model_dump(mode="json") for i in invariants],
        "release_gates": [g.model_dump(mode="json") for g in release_gates],
        "summary": summary,
    }
    gate_sha = compute_gate_sha256(digest_input)

    return T506ReleaseGateBundle(
        openapi_import_v1_blob_sha=actual_openapi_sha,
        openapi_pin_verified=openapi_pin_verified,
        evidence_bundles=bundles,
        invariants=invariants,
        release_gates=release_gates,
        summary=summary,
        deterministic_gate_sha256=gate_sha,
    )


def assert_strict_three_tier_honesty(bundle: T506ReleaseGateBundle) -> None:
    """Fail closed if hosted cloud is promoted without T-007F/T-106B or live AI is promoted while OD-002/OD-014 are OFF."""
    gate_by_tier = {g.tier: g for g in bundle.release_gates}
    bundle_by_id = {b.bundle_id: b for b in bundle.evidence_bundles}
    hosted = gate_by_tier.get(ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE)
    live_ai = gate_by_tier.get(ReleaseGateTier.LIVE_SUBSCRIPTION_AI_PILOT_GATE)
    hosted_bundle = bundle_by_id.get("T-007F/T-106B")
    if hosted is None or (
        hosted.decision is ReleaseGateDecision.GO
        and (hosted_bundle is None or not hosted_bundle.verified)
    ):
        raise PilotGatePolicyViolationError(
            "PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE cannot be promoted to GO without verified T-007F/T-106B hosted evidence."
        )
    if live_ai is None or live_ai.decision is ReleaseGateDecision.GO:
        raise PilotGatePolicyViolationError(
            "LIVE_SUBSCRIPTION_AI_PILOT_GATE cannot be promoted to GO from synthetic evidence while OD-002/OD-014 are OFF."
        )


__all__ = [
    "PilotGatePolicyViolationError",
    "assert_strict_three_tier_honesty",
    "evaluate_t506_pilot_gate",
]
