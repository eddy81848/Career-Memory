"""Unit and deliberate fault-injection test suite for T-004A synthetic dataset and validator.

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

from build_fixtures import CANARY_TOKEN_REF07, build_all_artifacts  # noqa: E402
from validate_dataset import (  # noqa: E402
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

    def test_01_valid_dataset_passes_structural_and_coverage_checks(self) -> None:
        errors, summary = validate_bundle(self.schema, self._clone_docs())
        self.assertEqual(errors, [], f"Expected 0 errors, got: {[e.format() for e in errors]}")
        self.assertGreaterEqual(summary["scenarios"], 12)
        self.assertGreaterEqual(summary["claim_atoms"], 24)
        self.assertEqual(summary["refetch_cases"], 8)
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
        codes = self._error_codes(docs)
        self.assertIn("HASH_MISMATCH", codes)

    def test_04_fault_injection_corrupted_span_hash(self) -> None:
        docs = self._clone_docs()
        docs["stages/stage2_fact_extraction.json"]["fact_supports"][0]["locator"]["span_sha256"] = (
            "sha256:" + ("f" * 64)
        )
        codes = self._error_codes(docs)
        self.assertIn("SPAN_HASH_MISMATCH", codes)

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
        # cu-03-0 is Korean multi-byte UTF-8 text
        korean_cu = next(
            cu for cu in docs["stages/stage1_content_units.json"]["content_units"] if cu["content_unit_id"] == "cu-03-0"
        )
        korean_cu["locator"]["utf8_byte_end"] -= 1  # Splits a 3-byte Hangul UTF-8 sequence
        codes = self._error_codes(docs)
        self.assertIn("BYTE_OFFSET_MISMATCH", codes)

    def test_07_fault_injection_broken_cross_stage_reference(self) -> None:
        docs = self._clone_docs()
        docs["stages/stage3_claim_assembly.json"]["claim_atom_supports"][0]["canonical_fact_id"] = "fact-does-not-exist"
        codes = self._error_codes(docs)
        self.assertIn("BROKEN_REFERENCE", codes)

    def test_08_fault_injection_self_evidencing_claim(self) -> None:
        # Case A: Claim cites itself via derived_from_claim_ids
        docs_a = self._clone_docs()
        docs_a["stages/stage3_claim_assembly.json"]["claims"][1]["derived_from_claim_ids"] = ["claim-02-1"]
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_a))

        # Case B: ClaimAtomSupport cites a Claim ID as canonical_fact_id
        docs_b = self._clone_docs()
        docs_b["stages/stage3_claim_assembly.json"]["claim_atom_supports"][0]["canonical_fact_id"] = "claim-02-1"
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_b))

        # Case C: FactSupport cites a Claim ID as content_unit_id
        docs_c = self._clone_docs()
        docs_c["stages/stage2_fact_extraction.json"]["fact_supports"][0]["content_unit_id"] = "claim-02-1"
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_c))

    def test_09_fault_injection_illegal_source_verified_on_edited_unsupported_partial_deleted(self) -> None:
        for target_claim_id in (
            "claim-01-1",  # 0 supports
            "claim-04-2",  # CONTEXT-only support
            "claim-10-1",  # Partially supported (1 of 3 atoms)
            "claim-11-1",  # Identity ambiguous
            "claim-12-1",  # User substantive edit
            "claim-12-2",  # User minor paraphrase edit
            "claim-13-1",  # Source deleted by user
            "claim-13-2",  # Partial support after deletion
        ):
            with self.subTest(claim_id=target_claim_id):
                docs = self._clone_docs()
                ec = next(
                    c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == target_claim_id
                )
                ec["expected_claim_status"] = "SOURCE_VERIFIED"
                codes = self._error_codes(docs)
                self.assertIn("ILLEGAL_SOURCE_VERIFIED", codes)

    def test_10_fault_injection_premature_golden_freeze_before_t004b(self) -> None:
        docs = self._clone_docs()
        docs["expected/oracle_draft.json"]["oracle_status"] = "GOLDEN"
        docs["expected/oracle_draft.json"]["frozen_for_golden"] = True
        codes = self._error_codes(docs)
        self.assertIn("PREMATURE_GOLDEN_FREEZE", codes)

    def test_11_fault_injection_canary_leak_into_persisted_payload(self) -> None:
        docs = self._clone_docs()
        ref07_chk = next(
            r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["refetch_case_id"] == "REF-07"
        )
        # Confirm clean snapshot has zero canary, negative fixture has canary
        self.assertEqual(detect_canary_in_obj(ref07_chk["persisted_payload_snapshot"], [CANARY_TOKEN_REF07]), [])
        self.assertEqual(
            detect_canary_in_obj(ref07_chk["negative_leaky_payload_fixture"], [CANARY_TOKEN_REF07]),
            [CANARY_TOKEN_REF07],
        )
        # Inject canary into persisted snapshot -> validator must fail
        ref07_chk["persisted_payload_snapshot"]["accidental_raw_snippet"] = f"leak: {CANARY_TOKEN_REF07}"
        codes = self._error_codes(docs)
        self.assertIn("CANARY_LEAK_DETECTED", codes)

    def test_12_fault_injection_unsafe_url_allowed(self) -> None:
        docs = self._clone_docs()
        ref06 = next(
            r for r in docs["sources/refetch_snapshots.json"]["refetch_snapshots"] if r["refetch_case_id"] == "REF-06"
        )
        ref06["url_safety_precheck"] = "ALLOWED"
        ref06["fetch_attempted"] = True
        codes = self._error_codes(docs)
        self.assertIn("UNSAFE_URL_POLICY_VIOLATION", codes)

    def test_13_fault_injection_unverifiable_conflated_with_historically_false(self) -> None:
        docs = self._clone_docs()
        ref03_oracle = next(
            r
            for r in docs["expected/refetch_oracle_draft.json"]["expected_refetch_cases"]
            if r["refetch_case_id"] == "REF-03"
        )
        ref03_oracle["expected_historical_fact_falsified"] = True
        codes = self._error_codes(docs)
        self.assertIn("UNVERIFIABLE_VS_FALSIFIED_CONFLATION", codes)

    def test_14_fault_injection_post_mvp_auto_claim_mutation(self) -> None:
        docs = self._clone_docs()
        ref08_oracle = next(
            r
            for r in docs["expected/refetch_oracle_draft.json"]["expected_refetch_cases"]
            if r["refetch_case_id"] == "REF-08"
        )
        ref08_oracle["expected_claim_auto_mutated"] = True
        codes = self._error_codes(docs)
        self.assertIn("POST_MVP_VIOLATION", codes)

    def test_15_fault_injection_schema_missing_required_field(self) -> None:
        docs = self._clone_docs()
        del docs["stages/stage2_fact_extraction.json"]["fact_supports"][0]["support_role"]
        codes = self._error_codes(docs)
        self.assertIn("SCHEMA_VALIDATION_ERROR", codes)


if __name__ == "__main__":
    unittest.main()
