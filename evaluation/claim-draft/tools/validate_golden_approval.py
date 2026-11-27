"""Standalone validator for the separate 0.4.0-synthetic-golden approval manifest
(`evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`).

Design guarantees:
- Does NOT modify or weaken the candidate validator (`validate_dataset.py`) or its ADR-018
  self-approval guard; candidate JSONs MUST stay `PENDING_OWNER_REVIEW` / `human_approved=false`.
- Verifies exact hash binding between `GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`,
  `CANDIDATE_MANIFEST.json`, and all 9 candidate dataset/schema artifacts on disk.
- Verifies exact counts (16 scenarios, 29 claims, 27 refetch cases), zero duplicate IDs,
  strict DEV/TEST partition preservation (no cross-partition leakage, all 15 v0.3.0 families
  preserved in their original partition), no fabricated approver identity/timestamp, and
  `product_runtime_verified=false` across all REF-01..REF-08 and live LLM/product scopes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from validate_dataset import (
    OFFICIAL_REF_REQUIREMENTS,
    PENDING_ORACLE_STATUS,
    PRIOR_BASELINE_BUNDLE_SHA256,
    PRIOR_V030_FAMILY_PARTITIONS,
    VALID_EVALUATION_PARTITIONS,
    ValidationErrorItem,
    check_no_stale_approval_manifest,
    load_dataset_bundle,
    validate_bundle,
)

APPROVAL_MANIFEST_FILENAME = "GOLDEN_APPROVAL_MANIFEST_v0.4.0.json"
EXPECTED_APPROVED_VERSION = "0.4.0-synthetic-golden"
EXPECTED_CANDIDATE_VERSION = "0.4.0-owner-review-candidate"
EXPECTED_SCENARIO_COUNT = 16
EXPECTED_CLAIM_COUNT = 29
EXPECTED_REFETCH_COUNT = 27
EXPECTED_PRIOR_V030_CLAIM_COUNT = 23
EXPECTED_ADDED_V040_CLAIM_COUNT = 6


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_golden_approval_manifest(base_dir: Path) -> dict[str, Any]:
    path = base_dir / APPROVAL_MANIFEST_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def validate_golden_approval(
    approval_doc: dict[str, Any],
    base_dir: Path,
    *,
    schema: dict[str, Any] | None = None,
    docs: dict[str, dict[str, Any]] | None = None,
    candidate_manifest_disk_sha256: str | None = None,
) -> tuple[list[ValidationErrorItem], dict[str, Any]]:
    """Validate GOLDEN_APPROVAL_MANIFEST_v0.4.0.json against the preserved candidate bundle."""
    errors: list[ValidationErrorItem] = []

    if schema is None or docs is None:
        schema, docs = load_dataset_bundle(base_dir)

    # 1. Verify the underlying candidate bundle stays intact & passes every candidate/ADR-018/semantic rule
    stale_errs = check_no_stale_approval_manifest(base_dir)
    errors.extend(stale_errs)
    cand_errors, cand_summary = validate_bundle(schema, docs)
    errors.extend(cand_errors)

    oracle_doc = docs["expected/oracle_draft.json"]
    ref_oracle_doc = docs["expected/refetch_oracle_draft.json"]
    s3_doc = docs["stages/stage3_claim_assembly.json"]
    s4_doc = docs["stages/stage4_refetch_checks.json"]
    snap_doc = docs["sources/refetch_snapshots.json"]
    cand_manifest = docs["CANDIDATE_MANIFEST.json"]

    # Confirm candidate artifacts were NOT rewritten to APPROVED
    if (
        cand_manifest.get("candidate_status") != PENDING_ORACLE_STATUS
        or cand_manifest.get("human_approved") is not False
        or cand_manifest.get("frozen_for_golden") is not False
        or oracle_doc.get("oracle_status") != PENDING_ORACLE_STATUS
        or ref_oracle_doc.get("oracle_status") != PENDING_ORACLE_STATUS
    ):
        errors.append(
            ValidationErrorItem(
                "CANDIDATE_PRESERVATION_VIOLATION",
                "CANDIDATE_MANIFEST.json",
                "Reviewed candidate JSONs must remain PENDING_OWNER_REVIEW / human_approved=false / frozen_for_golden=false",
            )
        )

    # 2. Top-level metadata & review/consumer linkage
    if approval_doc.get("dataset_version") != EXPECTED_APPROVED_VERSION:
        errors.append(
            ValidationErrorItem(
                "APPROVAL_VERSION_MISMATCH",
                "dataset_version",
                f"Expected {EXPECTED_APPROVED_VERSION!r}, got {approval_doc.get('dataset_version')!r}",
            )
        )
    if approval_doc.get("approval_status") != "APPROVED_SYNTHETIC_REGRESSION_GOLDEN":
        errors.append(ValidationErrorItem("APPROVAL_STATUS_INVALID", "approval_status", "Must be APPROVED_SYNTHETIC_REGRESSION_GOLDEN"))
    if approval_doc.get("review_task_ref") != "T-004B" or approval_doc.get("downstream_consumer_task_ref") != "T-407A":
        errors.append(ValidationErrorItem("APPROVAL_LINKAGE_ERROR", "task_refs", "Must link review_task_ref='T-004B' and downstream_consumer_task_ref='T-407A'"))

    # 3. No fabricated approver name, account, or timestamp
    auth = approval_doc.get("approval_authority", {})
    if (
        auth.get("approver_name") is not None
        or auth.get("approver_account") is not None
        or auth.get("approved_at") is not None
    ):
        errors.append(
            ValidationErrorItem(
                "FABRICATED_APPROVAL_METADATA",
                "approval_authority",
                "Unconfirmed approver_name, approver_account, and approved_at MUST remain null (never fabricated)",
            )
        )
    if auth.get("approved_by_role") != "PROJECT_OWNER" or auth.get("human_approved") is not True:
        errors.append(
            ValidationErrorItem(
                "APPROVAL_AUTHORITY_INVALID",
                "approval_authority",
                "Must record approved_by_role='PROJECT_OWNER' and human_approved=true",
            )
        )

    # 4. Exact candidate & per-file SHA-256 hash verification
    afc = approval_doc.get("approved_from_candidate", {})
    actual_bundle_sha = cand_summary.get("bundle_sha256")
    actual_cand_manifest_sha = candidate_manifest_disk_sha256 or _sha256_file(base_dir / "CANDIDATE_MANIFEST.json")

    if afc.get("candidate_dataset_version") != EXPECTED_CANDIDATE_VERSION:
        errors.append(ValidationErrorItem("APPROVAL_VERSION_MISMATCH", "approved_from_candidate.candidate_dataset_version", "Candidate version mismatch"))
    if afc.get("candidate_files_mutated") is not False or afc.get("candidate_oracle_status_preserved") != PENDING_ORACLE_STATUS:
        errors.append(ValidationErrorItem("CANDIDATE_PRESERVATION_VIOLATION", "approved_from_candidate", "Must preserve candidate files unmutated in PENDING_OWNER_REVIEW"))
    if afc.get("candidate_bundle_sha256") != actual_bundle_sha or approval_doc.get("approved_candidate_bundle_sha256") != actual_bundle_sha:
        errors.append(
            ValidationErrorItem(
                "APPROVAL_HASH_MISMATCH",
                "approved_candidate_bundle_sha256",
                f"Approval bundle hash ({afc.get('candidate_bundle_sha256')!r} / {approval_doc.get('approved_candidate_bundle_sha256')!r}) != live candidate bundle hash {actual_bundle_sha!r}",
            )
        )
    if actual_bundle_sha == PRIOR_BASELINE_BUNDLE_SHA256:
        errors.append(ValidationErrorItem("APPROVAL_HASH_MISMATCH", "approved_candidate_bundle_sha256", "Bundle hash must not equal prior v0.3.0 baseline"))
    if afc.get("candidate_manifest_sha256") != actual_cand_manifest_sha:
        errors.append(
            ValidationErrorItem(
                "APPROVAL_HASH_MISMATCH",
                "approved_from_candidate.candidate_manifest_sha256",
                f"Recorded {afc.get('candidate_manifest_sha256')!r} != actual CANDIDATE_MANIFEST.json {actual_cand_manifest_sha!r}",
            )
        )

    cand_artifact_hashes = cand_manifest.get("artifact_sha256", {})
    rec_artifact_hashes = approval_doc.get("artifact_sha256", {})
    if rec_artifact_hashes != cand_artifact_hashes:
        errors.append(ValidationErrorItem("APPROVAL_HASH_MISMATCH", "artifact_sha256", "artifact_sha256 does not match CANDIDATE_MANIFEST.json exactly"))

    repo_root = base_dir.parent.parent
    repo_rel_hashes = approval_doc.get("repo_relative_artifact_sha256", {})
    expected_repo_rel_hashes = {
        f"evaluation/claim-draft/{k}": v for k, v in cand_artifact_hashes.items()
    }
    expected_repo_rel_hashes["evaluation/claim-draft/CANDIDATE_MANIFEST.json"] = actual_cand_manifest_sha
    if repo_rel_hashes != expected_repo_rel_hashes:
        errors.append(ValidationErrorItem("APPROVAL_HASH_MISMATCH", "repo_relative_artifact_sha256", "repo_relative_artifact_sha256 mismatch with candidate files + CANDIDATE_MANIFEST.json"))
    for rel_path, expected_digest in sorted(repo_rel_hashes.items()):
        file_path = repo_root / rel_path
        if not file_path.exists():
            errors.append(ValidationErrorItem("APPROVAL_MISSING_FILE", rel_path, "Referenced artifact path does not exist"))
        else:
            disk_digest = _sha256_file(file_path)
            if disk_digest != expected_digest:
                errors.append(
                    ValidationErrorItem(
                        "APPROVAL_HASH_MISMATCH",
                        f"repo_relative_artifact_sha256[{rel_path}]",
                        f"Recorded {expected_digest} != on-disk {disk_digest}",
                    )
                )

    # 5. Exact counts, zero duplicates, and T-407A single-entrypoint contract (29 claims, 27 refetch, 16 scenarios)
    entry = approval_doc.get("t407a_single_entrypoint", {})
    counts = approval_doc.get("approved_counts", {})

    actual_scenario_ids = [s["scenario_id"] for s in oracle_doc.get("scenarios", [])]
    actual_claim_ids = [c["claim_id"] for c in s3_doc.get("claims", [])]
    actual_refetch_ids = [r["fixture_case_id"] for r in snap_doc.get("refetch_snapshots", [])]

    def _check_unique_list(label: str, values: list[str], expected_set: set[str], expected_len: int) -> None:
        if len(values) != len(set(values)):
            errors.append(ValidationErrorItem("APPROVAL_DUPLICATE_OR_COUNT_MISMATCH", label, f"Duplicate IDs found in {label}"))
        if len(values) != expected_len or set(values) != expected_set:
            errors.append(
                ValidationErrorItem(
                    "APPROVAL_DUPLICATE_OR_COUNT_MISMATCH",
                    label,
                    f"Expected {expected_len} exact unique IDs matching bundle, got {len(values)}",
                )
            )

    _check_unique_list("t407a_single_entrypoint.approved_scenario_ids", entry.get("approved_scenario_ids", []), set(actual_scenario_ids), EXPECTED_SCENARIO_COUNT)
    _check_unique_list("t407a_single_entrypoint.approved_claim_ids", entry.get("approved_claim_ids", []), set(actual_claim_ids), EXPECTED_CLAIM_COUNT)
    _check_unique_list("t407a_single_entrypoint.approved_refetch_case_ids", entry.get("approved_refetch_case_ids", []), set(actual_refetch_ids), EXPECTED_REFETCH_COUNT)

    # Verify non-overlapping v0.3.0 preserved (23) + v0.4.0 added (6) == 29 claims (no double counting!)
    v030_claims = entry.get("preserved_from_v030_claim_ids", [])
    v040_claims = entry.get("added_in_v040_claim_ids", [])
    if (
        len(v030_claims) != EXPECTED_PRIOR_V030_CLAIM_COUNT
        or len(v040_claims) != EXPECTED_ADDED_V040_CLAIM_COUNT
        or len(set(v030_claims)) != EXPECTED_PRIOR_V030_CLAIM_COUNT
        or len(set(v040_claims)) != EXPECTED_ADDED_V040_CLAIM_COUNT
        or bool(set(v030_claims) & set(v040_claims))
        or (set(v030_claims) | set(v040_claims)) != set(actual_claim_ids)
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_DUPLICATE_OR_COUNT_MISMATCH",
                "t407a_single_entrypoint.claim_lineage",
                "Preserved v0.3.0 (23) + added v0.4.0 (6) claim IDs must be disjoint and equal the exact 29 approved claims",
            )
        )

    v030_refs = entry.get("preserved_from_v030_refetch_case_ids", [])
    v040_refs = entry.get("added_in_v040_refetch_case_ids", [])
    if (
        len(v030_refs) != 12
        or len(v040_refs) != 15
        or bool(set(v030_refs) & set(v040_refs))
        or (set(v030_refs) | set(v040_refs)) != set(actual_refetch_ids)
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_DUPLICATE_OR_COUNT_MISMATCH",
                "t407a_single_entrypoint.refetch_lineage",
                "Preserved v0.3.0 (12) + added v0.4.0 (15) refetch IDs must be disjoint and equal the exact 27 approved refetch cases",
            )
        )

    expected_counts = {
        "source_revisions": cand_summary["source_revisions"],
        "content_units": cand_summary["content_units"],
        "fact_candidates": cand_summary["fact_candidates"],
        "fact_support_validations": cand_summary["fact_support_validations"],
        "canonical_facts": cand_summary["canonical_facts"],
        "fact_supports": cand_summary["fact_supports"],
        "confirmed_events": cand_summary["confirmed_events"],
        "scenarios": EXPECTED_SCENARIO_COUNT,
        "claims": EXPECTED_CLAIM_COUNT,
        "claim_atoms": cand_summary["claim_atoms"],
        "claim_atom_supports": cand_summary["claim_atom_supports"],
        "refetch_cases": EXPECTED_REFETCH_COUNT,
        "evaluation_families": 17,
    }
    if counts != expected_counts:
        errors.append(
            ValidationErrorItem(
                "APPROVAL_DUPLICATE_OR_COUNT_MISMATCH",
                "approved_counts",
                f"approved_counts {counts} != expected {expected_counts}",
            )
        )

    # Check every T-407A dataset_files entry exists and is covered by repo_relative_artifact_sha256
    dataset_files_map = entry.get("dataset_files", {})
    if len(dataset_files_map) != 10 or set(dataset_files_map.values()) != set(expected_repo_rel_hashes.keys()):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_ENTRYPOINT_MISMATCH",
                "t407a_single_entrypoint.dataset_files",
                "dataset_files must enumerate all 9 dataset/schema files + CANDIDATE_MANIFEST.json",
            )
        )

    # 6. Evaluation partition preservation (exact match to candidate + disjoint + all 15 v0.3.0 families preserved)
    parts = approval_doc.get("evaluation_partitions", {})
    if parts != cand_manifest.get("evaluation_partitions"):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_PARTITION_VIOLATION",
                "evaluation_partitions",
                "Approval evaluation_partitions drifted from CANDIDATE_MANIFEST.json",
            )
        )
    dev_part = parts.get("SYNTHETIC_REGRESSION_DEV", {})
    test_part = parts.get("SYNTHETIC_REGRESSION_TEST", {})
    for field_name, expected_set in (
        ("family_ids", set(PRIOR_V030_FAMILY_PARTITIONS.keys()) | {"family-src15", "family-src16"}),
        ("scenario_ids", set(actual_scenario_ids)),
        ("claim_ids", set(actual_claim_ids)),
        ("refetch_fixture_case_ids", set(actual_refetch_ids)),
    ):
        d_list = dev_part.get(field_name, [])
        t_list = test_part.get(field_name, [])
        if len(d_list) != len(set(d_list)) or len(t_list) != len(set(t_list)):
            errors.append(ValidationErrorItem("APPROVAL_PARTITION_VIOLATION", f"evaluation_partitions.{field_name}", "Duplicate entries inside a partition"))
        if set(d_list) & set(t_list):
            errors.append(ValidationErrorItem("APPROVAL_PARTITION_VIOLATION", f"evaluation_partitions.{field_name}", f"Overlap across DEV/TEST: {sorted(set(d_list) & set(t_list))}"))
        if (set(d_list) | set(t_list)) != expected_set:
            errors.append(ValidationErrorItem("APPROVAL_PARTITION_VIOLATION", f"evaluation_partitions.{field_name}", "DEV+TEST union does not equal full approved set"))

    for fam, orig_part in PRIOR_V030_FAMILY_PARTITIONS.items():
        if fam not in parts.get(orig_part, {}).get("family_ids", []):
            errors.append(
                ValidationErrorItem(
                    "APPROVAL_PARTITION_VIOLATION",
                    f"evaluation_partitions.{orig_part}",
                    f"Prior v0.3.0 family '{fam}' was not preserved in {orig_part}",
                )
            )
    for part_name in VALID_EVALUATION_PARTITIONS:
        if part_name not in parts:
            errors.append(ValidationErrorItem("APPROVAL_PARTITION_VIOLATION", "evaluation_partitions", f"Missing partition {part_name}"))

    # 7. Scope & product runtime unverified honesty (NO overclaim, NO premature spec freeze)
    scope = approval_doc.get("approval_scope", {})
    for flag in (
        "live_llm_evaluation_approved",
        "product_pipeline_approved",
        "physical_db_or_connectors_approved",
        "production_limits_approved",
        "ref_runtime_tests_passed",
        "product_runtime_verified",
    ):
        if scope.get(flag) is not False:
            errors.append(ValidationErrorItem("APPROVAL_SCOPE_OVERCLAIM", f"approval_scope.{flag}", f"{flag} MUST remain false"))

    non_frozen = scope.get("non_frozen_specs_review_required", {})
    for nf_flag in (
        "production_normalization_and_offset_rules_frozen",
        "physical_db_schema_frozen",
        "export_field_and_header_layout_frozen",
        "production_numeric_limits_frozen",
    ):
        if non_frozen.get(nf_flag) is not False:
            errors.append(ValidationErrorItem("APPROVAL_SCOPE_OVERCLAIM", f"approval_scope.non_frozen_specs_review_required.{nf_flag}", f"{nf_flag} MUST remain false"))

    ref_status = approval_doc.get("ref_verification_status", {})
    if (
        ref_status.get("product_runtime_verified_refs") != []
        or ref_status.get("product_runtime_unverified_all") != sorted(OFFICIAL_REF_REQUIREMENTS.keys())
    ):
        errors.append(
            ValidationErrorItem(
                "APPROVAL_SCOPE_OVERCLAIM",
                "ref_verification_status",
                "All REF-01..REF-08 MUST remain product_runtime_verified_refs=[] and unverified in product runtime",
            )
        )
    for row in ref_oracle_doc.get("official_ref_matrix", []):
        if row.get("product_runtime_verified") is not False:
            errors.append(ValidationErrorItem("APPROVAL_SCOPE_OVERCLAIM", f"official_ref_matrix[{row.get('official_ref_id')}]", "product_runtime_verified must remain false"))

    approval_manifest_path = base_dir / APPROVAL_MANIFEST_FILENAME
    approval_manifest_sha256 = _sha256_file(approval_manifest_path) if approval_manifest_path.exists() else None

    summary = {
        "approved_dataset_version": approval_doc.get("dataset_version"),
        "approval_manifest_path": f"evaluation/claim-draft/{APPROVAL_MANIFEST_FILENAME}",
        "approval_manifest_sha256": approval_manifest_sha256,
        "approved_from_candidate_version": afc.get("candidate_dataset_version"),
        "candidate_manifest_sha256": actual_cand_manifest_sha,
        "candidate_bundle_sha256": actual_bundle_sha,
        "candidate_oracle_status_preserved": cand_manifest.get("candidate_status"),
        "review_task_ref": approval_doc.get("review_task_ref"),
        "downstream_consumer_task_ref": approval_doc.get("downstream_consumer_task_ref"),
        "scenarios": len(actual_scenario_ids),
        "claims": len(actual_claim_ids),
        "refetch_cases": len(actual_refetch_ids),
        "dev_claims": len(dev_part.get("claim_ids", [])),
        "test_claims": len(test_part.get("claim_ids", [])),
        "dev_refetch_cases": len(dev_part.get("refetch_fixture_case_ids", [])),
        "test_refetch_cases": len(test_part.get("refetch_fixture_case_ids", [])),
        "product_runtime_verified_refs": ref_status.get("product_runtime_verified_refs"),
        "live_llm_evaluation_approved": scope.get("live_llm_evaluation_approved"),
        "error_count": len(errors),
    }
    return errors, summary


def main() -> int:
    base_dir = Path(__file__).resolve().parent.parent
    approval_doc = load_golden_approval_manifest(base_dir)
    errors, summary = validate_golden_approval(approval_doc, base_dir)
    print(f"T-004B -> T-407A Synthetic Golden Approval Validation Summary ({summary['approved_dataset_version']}):")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if errors:
        print(f"\nFAILED with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err.format()}")
        return 1
    print(
        "\nPASSED: Separate 0.4.0-synthetic-golden approval manifest verified against untouched "
        "0.4.0-owner-review-candidate (exact hashes, 16 scenarios / 29 claims / 27 refetch cases, "
        "0 duplicates, DEV/TEST partition preservation, unconfirmed specs non-frozen, and "
        "product_runtime_verified=false across REF-01..REF-08)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
