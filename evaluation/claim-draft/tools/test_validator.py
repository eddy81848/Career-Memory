"""Unit, regression, and deliberate fault-injection test suite for T-004A/B synthetic regression golden dataset (v0.3.0-synthetic-golden).

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
        self.assertEqual(summary["dataset_version"], "0.3.0-synthetic-golden")
        self.assertEqual(summary["approval_status"], "APPROVED_SYNTHETIC_REGRESSION_GOLDEN")
        self.assertEqual(summary["scenarios"], 14)
        self.assertEqual(summary["canonical_facts"], 26)
        self.assertEqual(summary["fact_supports"], 29)
        self.assertEqual(summary["claims"], 23)
        self.assertEqual(summary["claim_atoms"], 41)
        self.assertEqual(summary["refetch_fixtures_preserved"], 12)
        self.assertEqual(
            summary["partition_counts"],
            {
                "SYNTHETIC_REGRESSION_DEV": {"families": 8, "scenarios": 7, "claims": 11, "refetch_fixtures": 1},
                "SYNTHETIC_REGRESSION_TEST": {"families": 7, "scenarios": 7, "claims": 12, "refetch_fixtures": 11},
            },
        )
        self.assertEqual(summary["official_ref_covered_stub"], ["REF-01", "REF-02", "REF-03"])
        self.assertEqual(summary["official_ref_partial_coverage_gap"], ["REF-04", "REF-05"])
        self.assertEqual(summary["official_ref_coverage_gap"], ["REF-06", "REF-07", "REF-08"])
        self.assertTrue(str(summary["bundle_sha256"]).startswith("sha256:"))
        self.assertGreater(summary["locator_roundtrips_checked"], 60)

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
            "claim-05-2",  # Log fact DIRECT, but user personal audit atom is CONTEXT-only
            "claim-08-2",  # Solo attribution CONTRADICTED, from-scratch unsupported, outcome CONTEXT-only
            "claim-10-1",  # 1/3 required atoms supported (1 CONTEXT-only, 1 missing support)
            "claim-11-1",  # Identity ambiguous
            "claim-12-1",  # User edit v2 failed re-verification (CONTEXT-only on both atoms)
            "claim-12-2",  # User edit v2 pending re-verification (stale v1 support)
            "claim-13-1",  # Source deleted by user (OD-003)
            "claim-13-2",  # Mixed-source Claim purged by user deletion (OD-003)
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

    def test_12_fault_injection_approval_scope_overclaim_rejected(self) -> None:
        docs_a = self._clone_docs()
        docs_a["expected/oracle_draft.json"]["oracle_status"] = "GOLDEN"
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["expected/oracle_draft.json"]["provenance_and_freeze_preconditions"][
            "live_llm_evaluation_in_scope"
        ] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_b))

        docs_c = self._clone_docs()
        docs_c["expected/oracle_draft.json"]["provenance_and_freeze_preconditions"][
            "production_connector_integration_in_scope"
        ] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_c))

    def test_13_fault_injection_fake_coverage_claim_on_unverified_ref04_to_ref08(self) -> None:
        for gap_ref_id in ("REF-06", "REF-07", "REF-08"):
            with self.subTest(official_ref_id=gap_ref_id, mode="coverage_gap"):
                docs = self._clone_docs()
                row = next(
                    r
                    for r in docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"]
                    if r["official_ref_id"] == gap_ref_id
                )
                row["coverage_status"] = "COVERED_SYNTHETIC_STUB"
                row["verified_in_draft"] = True
                row["oracle_status"] = "APPROVED_SYNTHETIC_REGRESSION"
                self.assertIn("FALSE_COVERAGE_CLAIM", self._error_codes(docs))

        for partial_ref_id in ("REF-04", "REF-05"):
            with self.subTest(official_ref_id=partial_ref_id, mode="partial_coverage_gap"):
                docs = self._clone_docs()
                row = next(
                    r
                    for r in docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"]
                    if r["official_ref_id"] == partial_ref_id
                )
                row["verified_in_draft"] = True
                row["oracle_status"] = "APPROVED_SYNTHETIC_REGRESSION"
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

    def test_21_fault_injection_od003_forbids_partially_grounded_after_deletion(self) -> None:
        docs = self._clone_docs()
        ec13_2 = next(
            c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-13-2"
        )
        ec13_2["draft_claim_verdict"] = "DRAFT_CLAIM_PARTIALLY_GROUNDED"
        self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_22_fault_injection_od003_forbids_retained_server_versions_edits_or_approvals(self) -> None:
        for field, bad_val in (
            ("post_cleanup_retained_claim_versions", [1]),
            ("post_cleanup_retained_edited_copies", 1),
            ("post_cleanup_retained_approved_copies", 1),
        ):
            with self.subTest(field=field):
                docs = self._clone_docs()
                c13_2 = next(
                    c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-13-2"
                )
                c13_2["deletion_lifecycle"][field] = bad_val
                self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_23_fault_injection_od003_requires_preview_confirmation_and_immediate_block(self) -> None:
        for field in (
            "deletion_preview_shown",
            "explicit_user_confirmation",
            "immediate_access_blocked_after_confirmation",
        ):
            with self.subTest(field=field):
                docs = self._clone_docs()
                c13_1 = next(
                    c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-13-1"
                )
                c13_1["deletion_lifecycle"][field] = False
                self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_24_fault_injection_od003_preserves_independent_source_record(self) -> None:
        docs = self._clone_docs()
        src13b = next(
            s for s in docs["sources/source_revisions.json"]["source_revisions"] if s["source_key"] == "src13b"
        )
        src13b["deleted_by_user"] = True
        self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_25_fault_injection_ref02c_same_hash_locator_only_mismatch(self) -> None:
        # Subtest A: faking locator_recheck_matched=True when tested_locator offsets are corrupted
        docs_a = self._clone_docs()
        chk02c = next(
            r for r in docs_a["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-02c"
        )
        chk02c["locator_recheck_matched"] = True
        codes_a = self._error_codes(docs_a)
        self.assertIn("OFFSET_OR_ROUNDTRIP_MISMATCH", codes_a)
        self.assertIn("LOCATOR_ONLY_MISMATCH_POLICY_VIOLATION", codes_a)

        # Subtest B: failing to block approval on same-hash locator-only mismatch
        docs_b = self._clone_docs()
        ro02c = next(
            r
            for r in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
            if r["fixture_case_id"] == "refcase-02c"
        )
        ro02c["expected_approval_blocked"] = False
        codes_b = self._error_codes(docs_b)
        self.assertIn("LOCATOR_ONLY_MISMATCH_POLICY_VIOLATION", codes_b)
        self.assertIn("REFETCH_APPROVAL_BLOCK_VIOLATION", codes_b)

    def test_26_fault_injection_ref03_transient_failures_distinct_from_404_and_block_approval(self) -> None:
        for fcid in ("refcase-03c", "refcase-03d", "refcase-03e"):
            with self.subTest(fixture_case_id=fcid, mode="unblock_approval"):
                docs = self._clone_docs()
                chk = next(
                    r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == fcid
                )
                chk["retry_policy_stub"]["approval_blocked_while_unavailable"] = False
                self.assertIn("TRANSIENT_RETRY_POLICY_VIOLATION", self._error_codes(docs))

            with self.subTest(fixture_case_id=fcid, mode="conflate_with_404"):
                docs = self._clone_docs()
                chk = next(
                    r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == fcid
                )
                chk["retry_policy_stub"]["distinguished_from_404"] = False
                self.assertIn("TRANSIENT_RETRY_POLICY_VIOLATION", self._error_codes(docs))

            with self.subTest(fixture_case_id=fcid, mode="oracle_unblock"):
                docs = self._clone_docs()
                ro = next(
                    r
                    for r in docs["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
                    if r["fixture_case_id"] == fcid
                )
                ro["expected_approval_blocked"] = False
                codes = self._error_codes(docs)
                self.assertIn("TRANSIENT_RETRY_POLICY_VIOLATION", codes)
                self.assertIn("REFETCH_APPROVAL_BLOCK_VIOLATION", codes)

    def test_27_claim02_effect_atom_and_claim05_2_attribution_split_invariants(self) -> None:
        # claim-02-1 must require BOTH implementation atom-02-1 AND conditional effect atom-02-2
        docs = self._clone_docs()
        c02 = next(c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-02-1")
        self.assertEqual(c02["claim_atom_ids"], ["atom-02-1", "atom-02-2"])
        docs["stages/stage3_claim_assembly.json"]["claim_atom_supports"] = [
            s
            for s in docs["stages/stage3_claim_assembly.json"]["claim_atom_supports"]
            if s["claim_atom_id"] != "atom-02-2"
        ]
        # Removing support for the conditional-effect atom must reject source_verified_eligible=True
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", self._error_codes(docs))

        # claim-05-2 must separate log-confirmed atom-05-3 (DIRECT) from user-audit atom-05-4 (CONTEXT-only)
        clean = self._clone_docs()
        c05_2 = next(c for c in clean["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-05-2")
        self.assertEqual(c05_2["claim_atom_ids"], ["atom-05-3", "atom-05-4"])
        atoms_by_id = {a["claim_atom_id"]: a for a in clean["expected/oracle_draft.json"]["expected_atoms"]}
        self.assertEqual(atoms_by_id["atom-05-3"]["draft_atom_verdict"], "DRAFT_ATOM_SUPPORTED_DIRECT")
        self.assertEqual(atoms_by_id["atom-05-4"]["draft_atom_verdict"], "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT")
        ec05_2 = next(c for c in clean["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-05-2")
        self.assertEqual(ec05_2["draft_claim_verdict"], "DRAFT_CLAIM_PARTIALLY_GROUNDED")
        self.assertFalse(ec05_2["source_verified_eligible"])

    def test_28_fault_injection_family_and_source_partition_leakage(self) -> None:
        # Clean check: edited claims & refetch fixtures share exact partition with their source scenario
        clean = self._clone_docs()
        parts = clean["APPROVAL_MANIFEST.json"]["evaluation_partitions"]
        self.assertIn("family-src12", parts["SYNTHETIC_REGRESSION_DEV"]["family_ids"])
        self.assertIn("claim-12-1", parts["SYNTHETIC_REGRESSION_DEV"]["claim_ids"])
        self.assertIn("claim-12-2", parts["SYNTHETIC_REGRESSION_DEV"]["claim_ids"])
        self.assertIn("family-src02", parts["SYNTHETIC_REGRESSION_TEST"]["family_ids"])
        self.assertIn("SCN-02", parts["SYNTHETIC_REGRESSION_TEST"]["scenario_ids"])
        self.assertIn("refcase-01a", parts["SYNTHETIC_REGRESSION_TEST"]["refetch_fixture_case_ids"])
        self.assertIn("family-src13", parts["SYNTHETIC_REGRESSION_TEST"]["family_ids"])
        self.assertIn("claim-13-2", parts["SYNTHETIC_REGRESSION_TEST"]["claim_ids"])

        # Case A: Same evaluation_family_id split across DEV and TEST
        docs_a = self._clone_docs()
        rf01a = next(
            r
            for r in docs_a["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
            if r["fixture_case_id"] == "refcase-01a"
        )
        rf01a["evaluation_partition"] = "SYNTHETIC_REGRESSION_DEV"
        self.assertIn("FAMILY_PARTITION_LEAKAGE", self._error_codes(docs_a))

        # Case B: Even if evaluation_family_id is disguised, same source_revision_id split is still caught
        docs_b = self._clone_docs()
        rf01a_b = next(
            r
            for r in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]
            if r["fixture_case_id"] == "refcase-01a"
        )
        rf01a_b["evaluation_family_id"] = "family-disguised-src02"
        rf01a_b["evaluation_partition"] = "SYNTHETIC_REGRESSION_DEV"
        self.assertIn("FAMILY_PARTITION_LEAKAGE", self._error_codes(docs_b))

    def test_29_fault_injection_approval_manifest_hash_tamper(self) -> None:
        docs_a = self._clone_docs()
        docs_a["APPROVAL_MANIFEST.json"]["bundle_sha256"] = "sha256:" + ("0" * 64)
        self.assertIn("MANIFEST_HASH_MISMATCH", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["APPROVAL_MANIFEST.json"]["artifact_sha256"]["expected/oracle_draft.json"] = "sha256:" + ("a" * 64)
        self.assertIn("MANIFEST_HASH_MISMATCH", self._error_codes(docs_b))

    def test_30_fault_injection_approval_manifest_scope_overclaim(self) -> None:
        docs_a = self._clone_docs()
        docs_a["APPROVAL_MANIFEST.json"]["approval_scope"]["live_llm_evaluation_in_scope"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["APPROVAL_MANIFEST.json"]["approval_scope"]["production_connector_integration_in_scope"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_b))


if __name__ == "__main__":
    unittest.main()
