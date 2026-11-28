"""Deterministic Redacted Evaluation Evidence Bundle Builder & Zero-Drift Comparator (T-407A)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from career_memory_api.claim_eval.models import (
    EVAL_BUNDLE_SCHEMA_VERSION,
    EXPECTED_APPROVED_COUNTS,
    EXPECTED_SCHEMA_MIGRATIONS,
    LIVE_LLM_SEMANTIC_EVAL_BLOCKED,
    SYNTHETIC_GATE_STATUS_FAILED,
    SYNTHETIC_GATE_STATUS_PASSED,
    VOLATILE_BUNDLE_KEYS,
    ClaimEvalSuiteResult,
)
from career_memory_api.claim_eval.plan_c_auditor import scan_object_for_canaries
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_redacted_claim_eval_bundle(
    suite: ClaimEvalSuiteResult,
    *,
    openapi_blob_sha: str,
    canary_tokens: Sequence[str] = (),
    raw_source_texts: Sequence[str] = (),
) -> dict[str, Any]:
    """Build deterministic, redacted T-407A evaluation evidence bundle (zero raw source text)."""
    scenario_entries = [s.to_deterministic_dict() for s in suite.scenario_results]
    refetch_entries = [r.to_deterministic_dict() for r in suite.refetch_results]

    # Per-partition rollups (SYNTHETIC_REGRESSION_DEV vs SYNTHETIC_REGRESSION_TEST)
    partitions_rollup: dict[str, dict[str, Any]] = {}
    for part_name in ("SYNTHETIC_REGRESSION_DEV", "SYNTHETIC_REGRESSION_TEST"):
        p_scns = [s for s in suite.scenario_results if s.evaluation_partition == part_name]
        p_claims = [c for s in p_scns for c in s.claim_results]
        p_atoms = [a for c in p_claims for a in c.atom_results]
        p_refs = [r for r in suite.refetch_results if r.evaluation_partition == part_name]
        partitions_rollup[part_name] = {
            "scenarios_passed": sum(1 for s in p_scns if s.passed),
            "scenarios_total": len(p_scns),
            "claims_passed": sum(1 for c in p_claims if c.matched),
            "claims_total": len(p_claims),
            "atoms_passed": sum(1 for a in p_atoms if a.matched),
            "atoms_total": len(p_atoms),
            "refetch_cases_passed": sum(1 for r in p_refs if r.passed),
            "refetch_cases_total": len(p_refs),
            "source_verified_expected_count": sum(
                1 for c in p_claims if c.expected_source_verified_computed
            ),
            "source_verified_observed_count": sum(
                1 for c in p_claims if c.observed_source_verified_computed
            ),
        }

    # Per-REF-01..REF-08 rollup
    ref_matrix: dict[str, dict[str, Any]] = {}
    for r in suite.refetch_results:
        bucket = ref_matrix.setdefault(
            r.official_ref_id,
            {
                "official_ref_id": r.official_ref_id,
                "case_ids": [],
                "passed_count": 0,
                "total_count": 0,
                "product_runtime_pg18_verified": True,
                "external_live_provider_verified": False,
            },
        )
        bucket["case_ids"].append(r.fixture_case_id)
        bucket["total_count"] += 1
        if r.passed:
            bucket["passed_count"] += 1
        else:
            bucket["product_runtime_pg18_verified"] = False

    synthetic_gate_status = (
        SYNTHETIC_GATE_STATUS_PASSED if suite.all_passed else SYNTHETIC_GATE_STATUS_FAILED
    )

    deterministic_core: dict[str, Any] = {
        "schema_version": EVAL_BUNDLE_SCHEMA_VERSION,
        "task_id": "T-407A",
        "dataset_version": suite.dataset_version,
        "approved_candidate_bundle_sha256": suite.approved_candidate_bundle_sha256,
        "candidate_manifest_sha256": suite.candidate_manifest_sha256,
        "approval_manifest_sha256": suite.approval_manifest_sha256,
        "immutable_openapi_blob_sha": openapi_blob_sha,
        "schema_migrations_applied": list(EXPECTED_SCHEMA_MIGRATIONS),
        "approved_counts_verified": dict(EXPECTED_APPROVED_COUNTS),
        "governance_gates": {
            "SYNTHETIC_CONTRACT_AND_SAFETY_GATE": synthetic_gate_status,
            "LIVE_LLM_SEMANTIC_EVAL": LIVE_LLM_SEMANTIC_EVAL_BLOCKED,
            "live_llm_hard_off_verified": suite.live_llm_hard_off_verified,
            "external_live_llm_calls": suite.external_live_llm_calls,
            "live_pilot_gate_eligible": False,
            "population_accuracy_claim_permitted": False,
            "population_accuracy_disclaimer": (
                "Small synthetic 0.4.0-synthetic-golden regression set (16 scenarios, 29 claims, "
                "27 refetch cases) executed with deterministic offline synthetic stage adapters; "
                "zero live LLM calls (OD-002/OD-014 OFF) and zero population accuracy claims (<1%)."
            ),
        },
        "safety_invariants": {
            "scenarios_passed": suite.passed_scenarios,
            "scenarios_total": suite.total_scenarios,
            "claims_passed": suite.passed_claims,
            "claims_total": suite.total_claims,
            "atoms_passed": suite.passed_atoms,
            "atoms_total": suite.total_atoms,
            "refetch_cases_passed": suite.passed_refetch_cases,
            "refetch_cases_total": suite.total_refetch_cases,
            "unsupported_source_verified_count": suite.unsupported_source_verified_count,
            "unsupported_approvals_count": suite.unsupported_approvals_count,
            "unsupported_or_stale_exports_count": suite.unsupported_or_stale_exports_count,
            "rejected_candidates_promoted_count": suite.rejected_candidates_promoted_count,
            "total_plan_c_leaks": suite.total_plan_c_leaks,
        },
        "partition_rollups": partitions_rollup,
        "official_ref_runtime_matrix": [ref_matrix[k] for k in sorted(ref_matrix.keys())],
        "scenarios": scenario_entries,
        "refetch_cases": refetch_entries,
    }
    deterministic_core["deterministic_bundle_sha256"] = _canonical_sha256(deterministic_core)

    bundle = {
        **deterministic_core,
        "volatile_runtime_metrics": dict(suite.volatile_runtime_metrics),
    }

    # Plan C self-audit on the serialized bundle itself (ADR-013 / REF-04)
    assert_no_forbidden_raw_keys(bundle, field_name="t407a_eval_bundle")
    canary_hits = scan_object_for_canaries(bundle, canary_tokens)
    if canary_hits:
        raise ValueError(
            f"Plan C violation: canary tokens detected in t407a_eval_bundle: {canary_hits}"
        )
    serialized = json.dumps(bundle, ensure_ascii=False)
    for raw_txt in raw_source_texts:
        cleaned = raw_txt.strip()
        if len(cleaned) >= 40 and cleaned in serialized:
            raise ValueError("Plan C violation: raw source document text leaked into t407a_eval_bundle.")

    return bundle


def _strip_volatile(bundle: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in bundle.items() if k not in VOLATILE_BUNDLE_KEYS}


def diff_deterministic_bundles(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    path: str = "$",
) -> list[str]:
    """Compare two evaluation bundles ignoring VOLATILE_BUNDLE_KEYS; return human-readable diffs."""
    if path == "$":
        expected = _strip_volatile(expected)
        actual = _strip_volatile(actual)

    diffs: list[str] = []
    if type(expected) is not type(actual):
        diffs.append(f"{path}: type {type(expected).__name__} != {type(actual).__name__}")
        return diffs

    if isinstance(expected, dict):
        all_keys = sorted(set(expected.keys()) | set(actual.keys()))
        for key in all_keys:
            child_path = f"{path}.{key}"
            if key not in expected:
                diffs.append(f"{child_path}: unexpected key in actual")
            elif key not in actual:
                diffs.append(f"{child_path}: missing key in actual")
            else:
                diffs.extend(diff_deterministic_bundles(expected[key], actual[key], path=child_path))
        return diffs

    if isinstance(expected, list):
        if len(expected) != len(actual):
            diffs.append(f"{path}: length {len(expected)} != {len(actual)}")
            return diffs
        for idx, (e_item, a_item) in enumerate(zip(expected, actual, strict=True)):
            diffs.extend(diff_deterministic_bundles(e_item, a_item, path=f"{path}[{idx}]"))
        return diffs

    if expected != actual:
        diffs.append(f"{path}: expected={expected!r} actual={actual!r}")
    return diffs


__all__ = [
    "build_redacted_claim_eval_bundle",
    "diff_deterministic_bundles",
]
