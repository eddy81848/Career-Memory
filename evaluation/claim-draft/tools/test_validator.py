"""Unit, regression, and deliberate fault-injection test suite for T-004A synthetic dataset (v0.2.0-draft).

Run from repo/worktree root:
    python3 -m unittest discover -s evaluation/claim-draft/tools -p "test_*.py" -v
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
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from build_fixtures import CANARY_TOKEN_REF04, build_all_artifacts  # noqa: E402
from validate_dataset import (  # noqa: E402
    OFFICIAL_REF_REQUIREMENTS,
    detect_canary_in_obj,
    load_dataset_bundle,
    validate_bundle,
)


class ClaimDraftDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema, cls.clean_docs = load_dataset_bundle(BASE_DIR)

    def _clone_docs(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.clean_docs)

    def _error_codes(self, docs: dict[str, dict[str, Any]]) -> set[str]:
        errors, _ = validate_bundle(self.schema, docs)
        return {e.code for e in errors}

    def test_01_valid_dataset_passes_structural_and_official_ref_matrix_checks(self) -> None:
        errors, summary = validate_bundle(self.schema, self._clone_docs())
        self.assertEqual(errors, [], f"Expected 0 errors, got: {[e.format() for e in errors]}")
        self.assertEqual(summary["scenarios"], 14)
        self.assertEqual(summary["claim_atoms"], 30)
        self.assertEqual(summary["refetch_fixtures_preserved"], 8)
        self.assertEqual(summary["official_ref_covered_stub"], ["REF-01", "REF-02", "REF-03"])
        self.assertEqual(summary["official_ref_partial_coverage_gap"], ["REF-04", "REF-05"])
        self.assertEqual(summary["official_ref_coverage_gap"], ["REF-06", "REF-07", "REF-08"])
        self.assertGreater(summary["locator_roundtrips_checked"], 50)

    def test_02_programmatic_builder_matches_checked_in_files_byte_for_byte(self) -> None:
        generated = build_all_artifacts()
        for rel_path, expected_payload in generated.items():
            on_disk = json.loads((BASE_DIR / rel_path).read_text(encoding="utf-8"))
            self.assertEqual(
                on_disk,
                expected_payload,
                f"{rel_path} drifted from programmatic builder output",
            )

    def test_03_fault_injection_corrupted_source_content_hash(self) -> None:
        docs = self._clone_docs()
        docs["sources/source_revisions.json"]["source_revisions"][0]["content_sha256"] = "sha256:" + ("0" * 64)
        self.assertIn("HASH_MISMATCH", self._error_codes(docs))

    def test_04_fault_injection_corrupted_span_hash(self) -> None:
        docs = self._clone_docs()
        docs["stages/stage2_fact_extraction.json"]["fact_supports"][0]["locator"]["span_sha256"] = (
            "sha256:" + ("f" * 64)
        )
        self.assertIn("SPAN_HASH_MISMATCH", self._error_codes(docs))

    def test_05_fault_injection_corrupted_char_offset(self) -> None:
        docs = self._clone_docs()
        loc = docs["stages/stage1_content_units.json"]["content_units"][1]["locator"]
        loc["char_start"] += 1
        loc["char_end"] += 1
        codes = self._error_codes(docs)
        self.assertTrue(
            {"OFFSET_OR_ROUNDTRIP_MISMATCH", "BYTE_OFFSET_MISMATCH", "SPAN_HASH_MISMATCH"} & codes,
            f"Expected offset/hash mismatch, got {codes}",
        )

    def test_06_fault_injection_corrupted_utf8_byte_offset_on_korean_fixture(self) -> None:
        docs = self._clone_docs()
        korean_cu = next(
            cu for cu in docs["stages/stage1_content_units.json"]["content_units"] if cu["content_unit_id"] == "cu-03-0"
        )
        korean_cu["locator"]["utf8_byte_end"] -= 1
        self.assertIn("BYTE_OFFSET_MISMATCH", self._error_codes(docs))

    def test_07_fault_injection_broken_cross_stage_reference(self) -> None:
        docs = self._clone_docs()
        docs["stages/stage3_claim_assembly.json"]["claim_atom_supports"][0]["canonical_fact_id"] = "fact-does-not-exist"
        self.assertIn("BROKEN_REFERENCE", self._error_codes(docs))

    def test_08_fault_injection_self_evidencing_claim(self) -> None:
        docs_a = self._clone_docs()
        docs_a["stages/stage3_claim_assembly.json"]["claims"][1]["derived_from_claim_ids"] = ["claim-02-1"]
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["stages/stage3_claim_assembly.json"]["claim_atom_supports"][0]["canonical_fact_id"] = "claim-02-1"
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_b))

        docs_c = self._clone_docs()
        docs_c["stages/stage2_fact_extraction.json"]["fact_supports"][0]["content_unit_id"] = "claim-02-1"
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_c))

    def test_09_fault_injection_illegal_source_verified_on_context_only_missing_required_atom_or_unreverified_edit(
        self,
    ) -> None:
        for target_claim_id in (
            "claim-01-1",  # Required atom has 0 supports
            "claim-04-2",  # Required atom has CONTEXT-only support
            "claim-08-2",  # Team CONTEXT-only support
            "claim-10-1",  # 1/3 required atoms supported (1 CONTEXT-only, 1 missing support)
            "claim-11-1",  # Identity ambiguous
            "claim-12-1",  # User edit v2 failed re-verification (CONTRADICTS)
            "claim-12-2",  # User edit v2 pending re-verification (stale v1 support)
            "claim-13-1",  # Source deleted by user
            "claim-13-2",  # 1 of 2 required atoms deleted by user
            "claim-14-1",  # Hard negatives (CONTRADICTED + CONTEXT-only)
        ):
            with self.subTest(claim_id=target_claim_id):
                docs = self._clone_docs()
                ec = next(
                    c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == target_claim_id
                )
                ec["source_verified_eligible"] = True
                ec["product_status_proposal"] = "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW"
                self.assertIn("ILLEGAL_SOURCE_VERIFIED", self._error_codes(docs))

    def test_10_fault_injection_stale_v1_support_cannot_verify_edited_v2_even_if_state_faked(self) -> None:
        docs = self._clone_docs()
        c12_2 = next(c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-12-2")
        c12_2["reverification_state_on_new_version"] = "REVERIFIED_PASSED_ON_NEW_VERSION"
        ec12_2 = next(
            c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-12-2"
        )
        ec12_2["source_verified_eligible"] = True
        ec12_2["draft_claim_verdict"] = "DRAFT_CLAIM_GROUNDED_CANDIDATE"
        ec12_2["product_status_proposal"] = "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW"
        ea12_2 = next(
            a for a in docs["expected/oracle_draft.json"]["expected_atoms"] if a["claim_atom_id"] == "atom-12-2"
        )
        ea12_2["draft_atom_verdict"] = "DRAFT_ATOM_SUPPORTED_DIRECT"
        # cas-12-2 still has evaluated_on_claim_version=1 (< claim_version=2) -> must be rejected!
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", self._error_codes(docs))

    def test_11_fault_injection_provisional_draft_verdict_conflated_with_product_status(self) -> None:
        docs = self._clone_docs()
        ec = next(c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-02-1")
        ec["draft_claim_verdict"] = "SOURCE_VERIFIED"
        codes = self._error_codes(docs)
        self.assertIn("VERDICT_NAMESPACE_CONFLATION", codes)
        self.assertIn("SCHEMA_VALIDATION_ERROR", codes)

    def test_12_fault_injection_premature_golden_freeze_before_t004b(self) -> None:
        docs = self._clone_docs()
        docs["expected/oracle_draft.json"]["oracle_status"] = "GOLDEN"
        docs["expected/oracle_draft.json"]["frozen_for_golden"] = True
        self.assertIn("PREMATURE_GOLDEN_FREEZE", self._error_codes(docs))

    def test_13_fault_injection_fake_coverage_claim_on_missing_ref06_ref07_ref08(self) -> None:
        for gap_ref_id in ("REF-06", "REF-07", "REF-08"):
            with self.subTest(official_ref_id=gap_ref_id):
                docs = self._clone_docs()
                row = next(
                    r
                    for r in docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"]
                    if r["official_ref_id"] == gap_ref_id
                )
                row["coverage_status"] = "COVERED_SYNTHETIC_STUB"
                row["verified_in_draft"] = True
                self.assertIn("FALSE_COVERAGE_CLAIM", self._error_codes(docs))

    def test_14_fault_injection_official_ref_label_mismatch(self) -> None:
        docs = self._clone_docs()
        docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"][0]["official_requirement_label"] = "임의 라벨"
        self.assertIn("OFFICIAL_REF_MAPPING_ERROR", self._error_codes(docs))

    def test_15_fault_injection_ref04_canary_leak_into_persisted_payload(self) -> None:
        docs = self._clone_docs()
        ref04_chk = next(
            r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-04a"
        )
        self.assertEqual(detect_canary_in_obj(ref04_chk["persisted_payload_snapshot"], [CANARY_TOKEN_REF04]), [])
        self.assertEqual(
            detect_canary_in_obj(ref04_chk["negative_leaky_payload_fixture"], [CANARY_TOKEN_REF04]),
            [CANARY_TOKEN_REF04],
        )
        ref04_chk["persisted_payload_snapshot"]["accidental_raw_snippet"] = f"leak: {CANARY_TOKEN_REF04}"
        self.assertIn("CANARY_LEAK_DETECTED", self._error_codes(docs))

    def test_16_fault_injection_ref05_unsafe_url_allowed(self) -> None:
        docs = self._clone_docs()
        ref05b = next(
            r for r in docs["sources/refetch_snapshots.json"]["refetch_snapshots"] if r["fixture_case_id"] == "refcase-05b"
        )
        ref05b["url_safety_precheck"] = "ALLOWED"
        ref05b["fetch_attempted"] = True
        self.assertIn("UNSAFE_URL_POLICY_VIOLATION", self._error_codes(docs))

    def test_17_fault_injection_ref03_unavailable_conflated_with_deleted_or_false(self) -> None:
        # Case A: Conflated with historically false
        docs_a = self._clone_docs()
        ref03a = next(
            r
            for r in docs_a["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
            if r["fixture_case_id"] == "refcase-03a"
        )
        ref03a["expected_historical_fact_falsified"] = True
        self.assertIn("UNVERIFIABLE_VS_DELETED_OR_FALSIFIED_CONFLATION", self._error_codes(docs_a))

        # Case B: Conflated with confirmed deletion
        docs_b = self._clone_docs()
        ref03b = next(
            r
            for r in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
            if r["fixture_case_id"] == "refcase-03b"
        )
        ref03b["expected_source_deleted_confirmed"] = True
        self.assertIn("UNVERIFIABLE_VS_DELETED_OR_FALSIFIED_CONFLATION", self._error_codes(docs_b))

    def test_18_fault_injection_post_mvp_auto_claim_mutation(self) -> None:
        docs = self._clone_docs()
        ref02b = next(
            r
            for r in docs["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
            if r["fixture_case_id"] == "refcase-02b"
        )
        ref02b["expected_claim_auto_mutated"] = True
        self.assertIn("POST_MVP_VIOLATION", self._error_codes(docs))

    def test_19_fault_injection_schema_missing_required_field(self) -> None:
        docs = self._clone_docs()
        del docs["stages/stage2_fact_extraction.json"]["fact_supports"][0]["support_role"]
        self.assertIn("SCHEMA_VALIDATION_ERROR", self._error_codes(docs))

    def test_20_official_ref_requirements_table_complete(self) -> None:
        self.assertEqual(len(OFFICIAL_REF_REQUIREMENTS), 8)


if __name__ == "__main__":
    unittest.main()
