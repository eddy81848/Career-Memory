"""Unit & fault-injection test suite for the separate 0.4.0-synthetic-golden approval manifest
(`evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`) and T-407A single-entrypoint contract.

Run from worktree root:
    python3 -B -m unittest evaluation/claim-draft/tools/test_golden_approval.py -v
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent
REPO_ROOT = BASE_DIR.parent.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from validate_dataset import load_dataset_bundle, validate_bundle  # noqa: E402
from validate_golden_approval import (  # noqa: E402
    load_golden_approval_manifest,
    validate_golden_approval,
)

EXPECTED_CANDIDATE_BUNDLE_SHA256 = "sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059"
EXPECTED_CANDIDATE_MANIFEST_SHA256 = "sha256:2e9ffde13dfa619cee9035fdf2f9746adfd6b925875a7a955790e210b4a0bcea"


class GoldenApprovalManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema, cls.clean_docs = load_dataset_bundle(BASE_DIR)
        cls.clean_approval = load_golden_approval_manifest(BASE_DIR)

    def _clone(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        return copy.deepcopy(self.clean_approval), copy.deepcopy(self.clean_docs)

    def _approval_error_codes(
        self,
        approval_doc: dict[str, Any],
        docs: dict[str, dict[str, Any]],
    ) -> set[str]:
        errs, _ = validate_golden_approval(approval_doc, BASE_DIR, schema=self.schema, docs=docs)
        return {e.code for e in errs}

    def test_01_valid_golden_approval_manifest_matches_untouched_candidate(self) -> None:
        approval, docs = self._clone()
        errs, summary = validate_golden_approval(approval, BASE_DIR, schema=self.schema, docs=docs)
        self.assertEqual(errs, [], f"Expected 0 errors, got: {[e.format() for e in errs]}")
        self.assertEqual(summary["approved_dataset_version"], "0.4.0-synthetic-golden")
        self.assertEqual(summary["approved_from_candidate_version"], "0.4.0-owner-review-candidate")
        self.assertEqual(summary["candidate_bundle_sha256"], EXPECTED_CANDIDATE_BUNDLE_SHA256)
        self.assertEqual(summary["candidate_manifest_sha256"], EXPECTED_CANDIDATE_MANIFEST_SHA256)
        self.assertEqual(summary["candidate_oracle_status_preserved"], "PENDING_OWNER_REVIEW")
        self.assertEqual(summary["scenarios"], 16)
        self.assertEqual(summary["claims"], 29)
        self.assertEqual(summary["refetch_cases"], 27)
        self.assertEqual(summary["dev_claims"], 15)
        self.assertEqual(summary["test_claims"], 14)
        self.assertEqual(summary["dev_refetch_cases"], 2)
        self.assertEqual(summary["test_refetch_cases"], 25)
        self.assertEqual(summary["product_runtime_verified_refs"], [])
        self.assertFalse(summary["live_llm_evaluation_approved"])
        self.assertIsNone(approval["approval_authority"]["approver_name"])
        self.assertIsNone(approval["approval_authority"]["approver_account"])
        self.assertIsNone(approval["approval_authority"]["approved_at"])

    def test_02_fault_injection_candidate_or_artifact_hash_mismatch_rejected(self) -> None:
        approval_a, docs_a = self._clone()
        approval_a["approved_candidate_bundle_sha256"] = "sha256:" + ("0" * 64)
        self.assertIn("APPROVAL_HASH_MISMATCH", self._approval_error_codes(approval_a, docs_a))

        approval_b, docs_b = self._clone()
        approval_b["approved_from_candidate"]["candidate_manifest_sha256"] = "sha256:" + ("1" * 64)
        self.assertIn("APPROVAL_HASH_MISMATCH", self._approval_error_codes(approval_b, docs_b))

        approval_c, docs_c = self._clone()
        approval_c["artifact_sha256"]["expected/oracle_draft.json"] = "sha256:" + ("a" * 64)
        self.assertIn("APPROVAL_HASH_MISMATCH", self._approval_error_codes(approval_c, docs_c))

    def test_03_fault_injection_duplicate_or_double_counted_claims_or_refetch_rejected(self) -> None:
        # Simulate accidental double-counting (e.g., unioning v0.3.0 23 claims + v0.4.0 29 claims)
        approval_a, docs_a = self._clone()
        approval_a["t407a_single_entrypoint"]["approved_claim_ids"].append("claim-02-1")
        self.assertIn("APPROVAL_DUPLICATE_OR_COUNT_MISMATCH", self._approval_error_codes(approval_a, docs_a))

        # Simulate stale fallback to v0.3.0 (dropping added v0.4.0 claims)
        approval_b, docs_b = self._clone()
        approval_b["t407a_single_entrypoint"]["approved_claim_ids"] = list(
            approval_b["t407a_single_entrypoint"]["preserved_from_v030_claim_ids"]
        )
        self.assertIn("APPROVAL_DUPLICATE_OR_COUNT_MISMATCH", self._approval_error_codes(approval_b, docs_b))

        # Simulate duplicate refetch case ID
        approval_c, docs_c = self._clone()
        approval_c["t407a_single_entrypoint"]["approved_refetch_case_ids"].append("refcase-07b")
        self.assertIn("APPROVAL_DUPLICATE_OR_COUNT_MISMATCH", self._approval_error_codes(approval_c, docs_c))

    def test_04_fault_injection_partition_leakage_or_v030_drift_rejected(self) -> None:
        approval, docs = self._clone()
        approval["evaluation_partitions"]["SYNTHETIC_REGRESSION_DEV"]["family_ids"].append("family-src02")
        approval["evaluation_partitions"]["SYNTHETIC_REGRESSION_TEST"]["family_ids"].remove("family-src02")
        self.assertIn("APPROVAL_PARTITION_VIOLATION", self._approval_error_codes(approval, docs))

    def test_05_fault_injection_runtime_or_llm_or_non_frozen_spec_overclaim_rejected(self) -> None:
        approval_a, docs_a = self._clone()
        approval_a["ref_verification_status"]["product_runtime_verified_refs"] = ["REF-01"]
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._approval_error_codes(approval_a, docs_a))

        approval_b, docs_b = self._clone()
        approval_b["approval_scope"]["live_llm_evaluation_approved"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._approval_error_codes(approval_b, docs_b))

        approval_c, docs_c = self._clone()
        approval_c["approval_scope"]["non_frozen_specs_review_required"]["physical_db_schema_frozen"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._approval_error_codes(approval_c, docs_c))

    def test_06_fault_injection_fabricated_approver_identity_or_timestamp_rejected(self) -> None:
        for field, val in (
            ("approver_name", "Fabricated Owner"),
            ("approver_account", "owner@example.test"),
            ("approved_at", "2026-11-27T18:35:00+09:00"),
        ):
            with self.subTest(field=field):
                approval, docs = self._clone()
                approval["approval_authority"][field] = val
                self.assertIn("FABRICATED_APPROVAL_METADATA", self._approval_error_codes(approval, docs))

    def test_07_fault_injection_candidate_mutated_to_approved_rejected(self) -> None:
        approval, docs = self._clone()
        docs["CANDIDATE_MANIFEST.json"]["candidate_status"] = "APPROVED_SYNTHETIC_REGRESSION_GOLDEN"
        docs["CANDIDATE_MANIFEST.json"]["human_approved"] = True
        codes = self._approval_error_codes(approval, docs)
        self.assertIn("CANDIDATE_PRESERVATION_VIOLATION", codes)
        self.assertIn("UNAPPROVED_VERSION_MARKED_APPROVED", codes)

    def test_08_t407a_single_entrypoint_loads_and_preserves_semantic_fault_guards(self) -> None:
        """Load dataset files strictly via GOLDEN_APPROVAL_MANIFEST_v0.4.0.json and verify all
        semantic guards fire independently of MANIFEST_HASH_MISMATCH."""
        files_map = self.clean_approval["t407a_single_entrypoint"]["dataset_files"]
        loaded_schema = json.loads((REPO_ROOT / files_map["schema"]).read_text(encoding="utf-8"))
        key_to_bundle_rel = {
            "source_revisions": "sources/source_revisions.json",
            "refetch_snapshots": "sources/refetch_snapshots.json",
            "stage1_content_units": "stages/stage1_content_units.json",
            "stage2_fact_extraction": "stages/stage2_fact_extraction.json",
            "stage3_claim_assembly": "stages/stage3_claim_assembly.json",
            "stage4_refetch_checks": "stages/stage4_refetch_checks.json",
            "expected_oracle": "expected/oracle_draft.json",
            "expected_refetch_oracle": "expected/refetch_oracle_draft.json",
            "candidate_manifest": "CANDIDATE_MANIFEST.json",
        }
        loaded_docs = {
            bundle_rel: json.loads((REPO_ROOT / files_map[k]).read_text(encoding="utf-8"))
            for k, bundle_rel in key_to_bundle_rel.items()
        }

        def sem_codes(mutated: dict[str, dict[str, Any]]) -> set[str]:
            errs, _ = validate_bundle(loaded_schema, mutated)
            return {e.code for e in errs if e.code != "MANIFEST_HASH_MISMATCH"}

        # 1) Cross-Event Fact on atom-15-1 -> fact-16-3
        d1 = copy.deepcopy(loaded_docs)
        next(
            c for c in d1["stages/stage3_claim_assembly.json"]["claim_atom_supports"]
            if c["claim_atom_support_id"] == "cas-15-1"
        )["canonical_fact_id"] = "fact-16-3"
        self.assertIn("CROSS_EVENT_EVIDENCE_MISMATCH", sem_codes(d1))

        # 2) Missing validator inputs on claim-16-1
        d2 = copy.deepcopy(loaded_docs)
        next(
            r for r in d2["stages/stage3_claim_assembly.json"]["role_execution_runs"]
            if r["run_id"] == "run-claim-validate-claim-16-1-v1"
        )["input_version_refs"] = []
        self.assertIn("VALIDATOR_INPUT_EVIDENCE_MISMATCH", sem_codes(d2))

        # 3) Missing role contribution evidence on claim-15-1 Tech Lead
        d3 = copy.deepcopy(loaded_docs)
        next(
            f for f in d3["stages/stage2_fact_extraction.json"]["canonical_facts"]
            if f["canonical_fact_id"] == "fact-15-1"
        )["role_attribution_grounding"]["active_contribution_fact_support_ids"] = []
        self.assertIn("ROLE_EVIDENCE_VALIDATION_ERROR", sem_codes(d3))

        # 4) Unapproved (refcase-07f) & version-mismatched (refcase-07g) Export allowed
        for fcid in ("refcase-07f", "refcase-07g"):
            with self.subTest(fixture_case_id=fcid):
                d4 = copy.deepcopy(loaded_docs)
                next(
                    c for c in d4["stages/stage4_refetch_checks.json"]["refetch_checks"]
                    if c["fixture_case_id"] == fcid
                )["operation_refetch_stub"]["export_precondition_input"]["export_artifact_emitted"] = True
                self.assertIn("REF07_EXPORT_PRECONDITION_VIOLATION", sem_codes(d4))


if __name__ == "__main__":
    unittest.main()
