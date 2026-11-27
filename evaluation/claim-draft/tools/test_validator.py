"""Unit, regression, and deliberate fault-injection test suite for T-004A/B owner-review candidate (v0.4.0-owner-review-candidate).

Run from repo/worktree root:
    python3 -B -m unittest discover -s evaluation/claim-draft/tools -p "test_*.py" -v
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from build_fixtures import CANARY_TOKEN_REF04, PRIOR_BASELINE_BUNDLE_SHA256, build_all_artifacts  # noqa: E402
from validate_dataset import (  # noqa: E402
    OFFICIAL_ATOM_STATUSES,
    OFFICIAL_FACT_TYPES,
    OFFICIAL_REF_REQUIREMENTS,
    check_no_stale_approval_manifest,
    detect_canary_in_obj,
    load_dataset_bundle,
    validate_bundle,
)


class ClaimCandidateDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema, cls.clean_docs = load_dataset_bundle(BASE_DIR)

    def _clone_docs(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.clean_docs)

    def _error_codes(self, docs: dict[str, dict[str, Any]]) -> set[str]:
        errors, _ = validate_bundle(self.schema, docs)
        return {e.code for e in errors}

    def test_01_valid_candidate_dataset_passes_all_checks(self) -> None:
        self.assertEqual(check_no_stale_approval_manifest(BASE_DIR), [])
        errors, summary = validate_bundle(self.schema, self._clone_docs())
        self.assertEqual(errors, [], f"Expected 0 errors, got: {[e.format() for e in errors]}")
        self.assertEqual(summary["dataset_version"], "0.4.0-owner-review-candidate")
        self.assertEqual(summary["candidate_status"], "PENDING_OWNER_REVIEW")
        self.assertFalse(summary["human_approved"])
        self.assertEqual(summary["source_revisions"], 20)
        self.assertEqual(summary["content_units"], 30)
        self.assertEqual(summary["fact_candidates"], 34)
        self.assertEqual(summary["fact_support_validations"], 38)
        self.assertEqual(summary["canonical_facts"], 31)
        self.assertEqual(summary["fact_supports"], 35)
        self.assertEqual(summary["official_fact_types_covered"], sorted(OFFICIAL_FACT_TYPES))
        self.assertEqual(summary["confirmed_events"], 16)
        self.assertEqual(summary["scenarios"], 16)
        self.assertEqual(summary["claims"], 29)
        self.assertEqual(summary["claim_atoms"], 50)
        self.assertEqual(summary["official_atom_statuses_covered"], sorted(OFFICIAL_ATOM_STATUSES))
        self.assertEqual(summary["refetch_fixtures_prepared"], 27)
        self.assertEqual(
            summary["partition_counts"],
            {
                "SYNTHETIC_REGRESSION_DEV": {"families": 9, "scenarios": 8, "claims": 15, "refetch_fixtures": 2},
                "SYNTHETIC_REGRESSION_TEST": {"families": 8, "scenarios": 8, "claims": 14, "refetch_fixtures": 25},
            },
        )
        self.assertEqual(summary["official_ref_synthetic_stubs"], ["REF-01", "REF-02", "REF-03"])
        self.assertEqual(summary["official_ref_partial_static"], ["REF-04", "REF-05"])
        self.assertEqual(summary["official_ref_synthetic_cases_runtime_unverified"], ["REF-06", "REF-07", "REF-08"])
        self.assertEqual(summary["product_runtime_verified_refs"], [])
        self.assertNotEqual(summary["bundle_sha256"], PRIOR_BASELINE_BUNDLE_SHA256)
        self.assertGreater(summary["locator_roundtrips_checked"], 140)

    def test_02_programmatic_builder_matches_checked_in_files_byte_for_byte(self) -> None:
        generated = build_all_artifacts()
        for rel_path, expected_payload in generated.items():
            on_disk = json.loads((BASE_DIR / rel_path).read_text(encoding="utf-8"))
            self.assertEqual(on_disk, expected_payload, f"{rel_path} drifted from programmatic builder output")

    def test_03_fault_injection_corrupted_source_content_hash(self) -> None:
        docs = self._clone_docs()
        docs["sources/source_revisions.json"]["source_revisions"][0]["content_sha256"] = "sha256:" + ("0" * 64)
        self.assertIn("HASH_MISMATCH", self._error_codes(docs))

    def test_04_fault_injection_corrupted_span_hash(self) -> None:
        docs = self._clone_docs()
        docs["stages/stage2_fact_extraction.json"]["fact_supports"][0]["locator"]["span_sha256"] = "sha256:" + ("f" * 64)
        self.assertIn("SPAN_HASH_MISMATCH", self._error_codes(docs))

    def test_05_fault_injection_corrupted_char_offset(self) -> None:
        docs = self._clone_docs()
        loc = docs["stages/stage1_content_units.json"]["content_units"][1]["locator"]
        loc["char_start"] += 1
        loc["char_end"] += 1
        codes = self._error_codes(docs)
        self.assertTrue({"OFFSET_OR_ROUNDTRIP_MISMATCH", "BYTE_OFFSET_MISMATCH", "SPAN_HASH_MISMATCH"} & codes)

    def test_06_fault_injection_corrupted_utf8_byte_offset_on_korean_fixture(self) -> None:
        docs = self._clone_docs()
        korean_cu = next(cu for cu in docs["stages/stage1_content_units.json"]["content_units"] if cu["content_unit_id"] == "cu-03-0")
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

    def test_09_fault_injection_illegal_source_verified_on_all_unverified_claims(self) -> None:
        for target_claim_id in (
            "claim-01-1",
            "claim-04-2",
            "claim-05-2",
            "claim-08-2",
            "claim-10-1",
            "claim-11-1",
            "claim-12-1",
            "claim-12-2",
            "claim-12-4",  # USER_CONFIRMED-only atom MUST NOT allow SOURCE_VERIFIED!
            "claim-13-1",
            "claim-13-2",
            "claim-14-1",
            "claim-15-2",  # CLAIM-05 role fail
            "claim-16-2",  # CLAIM-06 metric fail
        ):
            with self.subTest(claim_id=target_claim_id):
                docs = self._clone_docs()
                ec = next(c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == target_claim_id)
                ec["expected_source_verified_computed"] = True
                ec["fixture_test_classification"]["product_status_proposal"] = "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW"
                self.assertIn("ILLEGAL_SOURCE_VERIFIED", self._error_codes(docs))

    def test_10_fault_injection_stale_v1_support_cannot_verify_edited_v2_even_if_state_faked(self) -> None:
        docs = self._clone_docs()
        c12_2 = next(c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-12-2")
        c12_2["reverification_state_on_new_version"] = "REVERIFIED_PASSED_ON_NEW_VERSION"
        ec12_2 = next(c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-12-2")
        ec12_2["expected_source_verified_computed"] = True
        ec12_2["fixture_test_classification"]["draft_claim_verdict"] = "DRAFT_CLAIM_GROUNDED_CANDIDATE"
        ec12_2["fixture_test_classification"]["product_status_proposal"] = "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW"
        ea12_2 = next(a for a in docs["expected/oracle_draft.json"]["expected_atoms"] if a["claim_atom_id"] == "atom-12-2")
        ea12_2["expected_domain_atom_status"] = "SUPPORTED"
        codes = self._error_codes(docs)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", codes)
        self.assertIn("ILLEGAL_SUPPORTED_ATOM", codes)

    def test_11_fault_injection_provisional_draft_verdict_conflated_with_product_status(self) -> None:
        docs = self._clone_docs()
        ec = next(c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-02-1")
        ec["fixture_test_classification"]["draft_claim_verdict"] = "SOURCE_VERIFIED"
        codes = self._error_codes(docs)
        self.assertIn("VERDICT_NAMESPACE_CONFLATION", codes)
        self.assertIn("SCHEMA_VALIDATION_ERROR", codes)

    def test_12_fault_injection_scope_overclaim_rejected(self) -> None:
        docs_a = self._clone_docs()
        docs_a["expected/oracle_draft.json"]["provenance_and_freeze_preconditions"]["live_llm_evaluation_in_scope"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["expected/oracle_draft.json"]["provenance_and_freeze_preconditions"]["production_connector_integration_in_scope"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_b))

    def test_13_fault_injection_ref_product_runtime_verified_overclaim(self) -> None:
        for ref_id in ("REF-01", "REF-04", "REF-05", "REF-06", "REF-07", "REF-08"):
            with self.subTest(official_ref_id=ref_id):
                docs = self._clone_docs()
                row = next(r for r in docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"] if r["official_ref_id"] == ref_id)
                row["product_runtime_verified"] = True
                self.assertIn("FALSE_PRODUCT_VERIFICATION_CLAIM", self._error_codes(docs))

    def test_14_fault_injection_official_ref_label_mismatch(self) -> None:
        docs = self._clone_docs()
        docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"][0]["official_requirement_label"] = "임의 라벨"
        self.assertIn("OFFICIAL_REF_MAPPING_ERROR", self._error_codes(docs))

    def test_15_fault_injection_ref04_canary_leak_into_persisted_payload(self) -> None:
        for fcid in ("refcase-04a", "refcase-04b"):
            with self.subTest(fixture_case_id=fcid):
                docs = self._clone_docs()
                ref04_chk = next(r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == fcid)
                self.assertEqual(detect_canary_in_obj(ref04_chk["persisted_payload_snapshot"], [CANARY_TOKEN_REF04]), [])
                self.assertEqual(detect_canary_in_obj(ref04_chk["negative_leaky_payload_fixture"], [CANARY_TOKEN_REF04]), [CANARY_TOKEN_REF04])
                ref04_chk["persisted_payload_snapshot"]["accidental_raw_snippet"] = f"leak: {CANARY_TOKEN_REF04}"
                self.assertIn("CANARY_LEAK_DETECTED", self._error_codes(docs))

    def test_16_fault_injection_ref05_unsafe_url_allowed(self) -> None:
        docs = self._clone_docs()
        ref05b = next(r for r in docs["sources/refetch_snapshots.json"]["refetch_snapshots"] if r["fixture_case_id"] == "refcase-05b")
        ref05b["url_safety_precheck"] = "ALLOWED"
        ref05b["fetch_attempted"] = True
        self.assertIn("UNSAFE_URL_POLICY_VIOLATION", self._error_codes(docs))

    def test_17_fault_injection_ref03_unavailable_conflated_with_deleted_or_false(self) -> None:
        docs_a = self._clone_docs()
        ref03a = next(r for r in docs_a["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-03a")
        ref03a["expected_historical_fact_falsified"] = True
        self.assertIn("UNVERIFIABLE_VS_DELETED_OR_FALSIFIED_CONFLATION", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        ref03b = next(r for r in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-03b")
        ref03b["expected_source_deleted_confirmed"] = True
        self.assertIn("UNVERIFIABLE_VS_DELETED_OR_FALSIFIED_CONFLATION", self._error_codes(docs_b))

    def test_18_fault_injection_post_mvp_auto_claim_mutation(self) -> None:
        docs = self._clone_docs()
        ref02b = next(r for r in docs["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-02b")
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
        ec13_2 = next(c for c in docs["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-13-2")
        ec13_2["fixture_test_classification"]["draft_claim_verdict"] = "DRAFT_CLAIM_PARTIALLY_GROUNDED"
        self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_22_fault_injection_od003_forbids_retained_server_versions_edits_or_approvals(self) -> None:
        for field, bad_val in (
            ("post_cleanup_retained_claim_versions", [1]),
            ("post_cleanup_retained_edited_copies", 1),
            ("post_cleanup_retained_approved_copies", 1),
        ):
            with self.subTest(field=field):
                docs = self._clone_docs()
                c13_2 = next(c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-13-2")
                c13_2["deletion_lifecycle"][field] = bad_val
                self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_23_fault_injection_od003_requires_preview_confirmation_and_immediate_block(self) -> None:
        for field in ("deletion_preview_shown", "explicit_user_confirmation", "immediate_access_blocked_after_confirmation"):
            with self.subTest(field=field):
                docs = self._clone_docs()
                c13_1 = next(c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-13-1")
                c13_1["deletion_lifecycle"][field] = False
                self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_24_fault_injection_od003_preserves_independent_source_record(self) -> None:
        docs = self._clone_docs()
        src13b = next(s for s in docs["sources/source_revisions.json"]["source_revisions"] if s["source_key"] == "src13b")
        src13b["deleted_by_user"] = True
        self.assertIn("OD003_DELETION_POLICY_VIOLATION", self._error_codes(docs))

    def test_25_fault_injection_ref02c_same_hash_locator_only_mismatch(self) -> None:
        docs_a = self._clone_docs()
        chk02c = next(r for r in docs_a["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-02c")
        chk02c["locator_recheck_matched"] = True
        codes_a = self._error_codes(docs_a)
        self.assertIn("OFFSET_OR_ROUNDTRIP_MISMATCH", codes_a)
        self.assertIn("LOCATOR_ONLY_MISMATCH_POLICY_VIOLATION", codes_a)

        docs_b = self._clone_docs()
        ro02c = next(r for r in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-02c")
        ro02c["expected_approval_blocked"] = False
        codes_b = self._error_codes(docs_b)
        self.assertIn("LOCATOR_ONLY_MISMATCH_POLICY_VIOLATION", codes_b)
        self.assertIn("REFETCH_APPROVAL_BLOCK_VIOLATION", codes_b)

    def test_26_fault_injection_ref03_transient_failures_distinct_from_404_and_block_approval(self) -> None:
        for fcid in ("refcase-03c", "refcase-03d", "refcase-03e"):
            with self.subTest(fixture_case_id=fcid, mode="unblock_approval"):
                docs = self._clone_docs()
                chk = next(r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == fcid)
                chk["retry_policy_stub"]["approval_blocked_while_unavailable"] = False
                self.assertIn("TRANSIENT_RETRY_POLICY_VIOLATION", self._error_codes(docs))

            with self.subTest(fixture_case_id=fcid, mode="conflate_with_404"):
                docs = self._clone_docs()
                chk = next(r for r in docs["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == fcid)
                chk["retry_policy_stub"]["distinguished_from_404"] = False
                self.assertIn("TRANSIENT_RETRY_POLICY_VIOLATION", self._error_codes(docs))

    def test_27_claim02_effect_atom_and_claim05_2_attribution_split_invariants(self) -> None:
        docs = self._clone_docs()
        c02 = next(c for c in docs["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-02-1")
        self.assertEqual(c02["claim_atom_ids"], ["atom-02-1", "atom-02-2"])
        docs["stages/stage3_claim_assembly.json"]["claim_atom_supports"] = [
            s for s in docs["stages/stage3_claim_assembly.json"]["claim_atom_supports"] if s["claim_atom_id"] != "atom-02-2"
        ]
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", self._error_codes(docs))

        clean = self._clone_docs()
        atoms_by_id = {a["claim_atom_id"]: a for a in clean["expected/oracle_draft.json"]["expected_atoms"]}
        self.assertEqual(atoms_by_id["atom-05-3"]["expected_domain_atom_status"], "SUPPORTED")
        self.assertEqual(atoms_by_id["atom-05-4"]["expected_domain_atom_status"], "UNSUPPORTED")
        ec05_2 = next(c for c in clean["expected/oracle_draft.json"]["expected_claims"] if c["claim_id"] == "claim-05-2")
        self.assertFalse(ec05_2["expected_source_verified_computed"])

    def test_28_fault_injection_family_and_source_partition_leakage_and_v030_boundary(self) -> None:
        clean = self._clone_docs()
        parts = clean["CANDIDATE_MANIFEST.json"]["evaluation_partitions"]
        for cid in ("claim-12-1", "claim-12-2", "claim-12-3", "claim-12-4"):
            self.assertIn(cid, parts["SYNTHETIC_REGRESSION_DEV"]["claim_ids"])
        for fcid in ("refcase-01a", "refcase-06a", "refcase-07a", "refcase-08a"):
            self.assertIn(fcid, parts["SYNTHETIC_REGRESSION_TEST"]["refetch_fixture_case_ids"])

        # Split family-src02 across DEV/TEST
        docs_a = self._clone_docs()
        rf06a = next(r for r in docs_a["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-06a")
        rf06a["evaluation_partition"] = "SYNTHETIC_REGRESSION_DEV"
        self.assertIn("FAMILY_PARTITION_LEAKAGE", self._error_codes(docs_a))

        # Disguised family name but same source_revision_id
        docs_b = self._clone_docs()
        rf06a_b = next(r for r in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-06a")
        rf06a_b["evaluation_family_id"] = "family-disguised-src02"
        rf06a_b["evaluation_partition"] = "SYNTHETIC_REGRESSION_DEV"
        self.assertIn("FAMILY_PARTITION_LEAKAGE", self._error_codes(docs_b))

    def test_29_fault_injection_candidate_manifest_hash_tamper(self) -> None:
        docs_a = self._clone_docs()
        docs_a["CANDIDATE_MANIFEST.json"]["bundle_sha256"] = "sha256:" + ("0" * 64)
        self.assertIn("MANIFEST_HASH_MISMATCH", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["CANDIDATE_MANIFEST.json"]["artifact_sha256"]["expected/oracle_draft.json"] = "sha256:" + ("a" * 64)
        self.assertIn("MANIFEST_HASH_MISMATCH", self._error_codes(docs_b))

    def test_30_fault_injection_candidate_manifest_scope_overclaim(self) -> None:
        docs_a = self._clone_docs()
        docs_a["CANDIDATE_MANIFEST.json"]["scope_statement"]["live_llm_evaluation_in_scope"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["CANDIDATE_MANIFEST.json"]["scope_statement"]["product_e2e_verified"] = True
        self.assertIn("APPROVAL_SCOPE_OVERCLAIM", self._error_codes(docs_b))

    # --- NEW v0.4.0 Spec-Sync Mandatory Fault-Injection Tests ---

    def test_31_fault_injection_role_run_id_reuse_between_generation_and_validation(self) -> None:
        # Case A: Stage 2B FactSupportValidator reuses Stage 2A FactCandidateExtractor run_id
        docs_a = self._clone_docs()
        docs_a["stages/stage2_fact_extraction.json"]["fact_support_validations"][0]["validator_run_id"] = (
            docs_a["stages/stage2_fact_extraction.json"]["fact_candidates"][0]["extractor_run_id"]
        )
        self.assertIn("ROLE_RUN_ID_REUSE_VIOLATION", self._error_codes(docs_a))

        # Case B: Stage 3C ClaimSupportValidator reuses Stage 3A ClaimGenerator run_id
        docs_b = self._clone_docs()
        c02 = next(c for c in docs_b["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-02-1")
        c02["validator_run_id"] = c02["generator_run_id"]
        self.assertIn("ROLE_RUN_ID_REUSE_VIOLATION", self._error_codes(docs_b))

        # Case C: ClaimAtomSupport cites ClaimGenerator run_id instead of ClaimSupportValidator run_id
        docs_c = self._clone_docs()
        c02_c = next(c for c in docs_c["stages/stage3_claim_assembly.json"]["claims"] if c["claim_id"] == "claim-02-1")
        docs_c["stages/stage3_claim_assembly.json"]["claim_atom_supports"][0]["validator_run_id"] = c02_c["generator_run_id"]
        self.assertIn("ROLE_RUN_ID_REUSE_VIOLATION", self._error_codes(docs_c))

        # Case D: ClaimSupportValidator uses Claim draft / AI explanation as evidence
        docs_d = self._clone_docs()
        val_run = next(r for r in docs_d["stages/stage3_claim_assembly.json"]["role_execution_runs"] if r["pipeline_role"] == "ClaimSupportValidator")
        val_run["uses_claim_draft_or_ai_explanation_as_evidence"] = True
        self.assertIn("SELF_EVIDENCE_VIOLATION", self._error_codes(docs_d))

    def test_32_fault_injection_ungrounded_supported_or_source_verified_rejected(self) -> None:
        # Case A: FactCandidate marks itself SUPPORTED via extraction alone
        docs_a = self._clone_docs()
        docs_a["stages/stage2_fact_extraction.json"]["fact_candidates"][0]["candidate_status"] = "SUPPORTED"
        self.assertIn("EXTRACTION_CANNOT_SELF_SUPPORT", self._error_codes(docs_a))

        # Case B: Rejected candidate promoted into CanonicalFact
        docs_b = self._clone_docs()
        docs_b["stages/stage2_fact_extraction.json"]["canonical_facts"][0]["source_candidate_id"] = "fcand-06-3-rejected"
        self.assertIn("ILLEGAL_PROMOTION_OF_REJECTED_CANDIDATE", self._error_codes(docs_b))

        # Case C: Contradicted fact-05-1 marked active SUPPORTED
        docs_c = self._clone_docs()
        f05_1 = next(f for f in docs_c["stages/stage2_fact_extraction.json"]["canonical_facts"] if f["canonical_fact_id"] == "fact-05-1")
        f05_1["fact_support_status"] = "SUPPORTED"
        f05_1["is_active_supported_fact"] = True
        self.assertIn("ILLEGAL_SUPPORTED_FACT", self._error_codes(docs_c))

        # Case D: USER_CONFIRMED / CONTEXT-only / unsupported atoms marked SUPPORTED
        for bad_atom_id in ("atom-01-1", "atom-04-2", "atom-12-6", "atom-15-3", "atom-16-3"):
            with self.subTest(atom_id=bad_atom_id):
                docs_d = self._clone_docs()
                ea = next(a for a in docs_d["expected/oracle_draft.json"]["expected_atoms"] if a["claim_atom_id"] == bad_atom_id)
                ea["expected_domain_atom_status"] = "SUPPORTED"
                self.assertIn("ILLEGAL_SUPPORTED_ATOM", self._error_codes(docs_d))

        # Case E: Claim marked SOURCE_VERIFIED without same-job refetch verification
        docs_e = self._clone_docs()
        val02 = next(r for r in docs_e["stages/stage3_claim_assembly.json"]["role_execution_runs"] if r["run_id"] == "run-claim-validate-claim-02-1-v1")
        val02["reverification_job_context"]["refetch_checked_in_same_job"] = False
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", self._error_codes(docs_e))

    def test_33_fault_injection_missing_ref06_ref07_ref08_mapping_fails(self) -> None:
        for ref_id in ("REF-06", "REF-07", "REF-08"):
            with self.subTest(official_ref_id=ref_id):
                docs = self._clone_docs()
                row = next(r for r in docs["expected/refetch_oracle_draft.json"]["official_ref_matrix"] if r["official_ref_id"] == ref_id)
                row["linked_fixture_case_ids"] = []
                self.assertIn("MISSING_OFFICIAL_REF_CASE_MAPPING", self._error_codes(docs))

    def test_34_fault_injection_inter_stage_raw_text_payload_leaks_fail(self) -> None:
        src02_span = (
            "I implemented an idempotent retry queue with exponential backoff for billing webhooks "
            "in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts."
        )
        # Case A: Forbidden raw_text key in Stage 2 extractor_runs
        docs_a = self._clone_docs()
        docs_a["stages/stage2_fact_extraction.json"]["extractor_runs"][0]["raw_text"] = "leaked raw doc"
        self.assertIn("INTER_STAGE_RAW_TEXT_LEAK", self._error_codes(docs_a))

        # Case B: Raw excerpt leaked into REF-08 separate-job handoff payload
        docs_b = self._clone_docs()
        chk08b = next(r for r in docs_b["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-08b")
        chk08b["limits_and_boundary_stub"]["inter_job_handoff_payload"]["leaked_note"] = src02_span
        self.assertIn("INTER_STAGE_RAW_TEXT_LEAK", self._error_codes(docs_b))

        # Case C: Raw source quote leaked into REF-07 Export payload
        docs_c = self._clone_docs()
        chk07b = next(r for r in docs_c["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-07b")
        chk07b["operation_refetch_stub"]["export_payload_snapshot"]["quoted_body"] = src02_span
        self.assertIn("INTER_STAGE_RAW_TEXT_LEAK", self._error_codes(docs_c))

        # Case D: Raw source excerpt leaked into REF-06 checkpoint_payload
        docs_d = self._clone_docs()
        chk06b = next(r for r in docs_d["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-06b")
        chk06b["crash_replay_stub"]["checkpoint_payload"]["cached_body"] = src02_span
        self.assertIn("INTER_STAGE_RAW_TEXT_LEAK", self._error_codes(docs_d))

    def test_35_fault_injection_unapproved_version_marked_approved_rejected_adr018(self) -> None:
        docs_a = self._clone_docs()
        docs_a["expected/oracle_draft.json"]["oracle_status"] = "APPROVED_SYNTHETIC_REGRESSION"
        docs_a["expected/oracle_draft.json"]["frozen_for_golden"] = True
        docs_a["expected/oracle_draft.json"]["human_approved"] = True
        self.assertIn("UNAPPROVED_VERSION_MARKED_APPROVED", self._error_codes(docs_a))

        docs_b = self._clone_docs()
        docs_b["CANDIDATE_MANIFEST.json"]["candidate_status"] = "APPROVED_SYNTHETIC_REGRESSION_GOLDEN"
        docs_b["CANDIDATE_MANIFEST.json"]["human_approved"] = True
        self.assertIn("UNAPPROVED_VERSION_MARKED_APPROVED", self._error_codes(docs_b))

        docs_c = self._clone_docs()
        docs_c["CANDIDATE_MANIFEST.json"]["bundle_sha256"] = PRIOR_BASELINE_BUNDLE_SHA256
        self.assertIn("UNAPPROVED_VERSION_MARKED_APPROVED", self._error_codes(docs_c))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "APPROVAL_MANIFEST.json").write_text("{}", encoding="utf-8")
            stale_errs = check_no_stale_approval_manifest(tmp_path)
            self.assertEqual({e.code for e in stale_errs}, {"UNAPPROVED_VERSION_MARKED_APPROVED"})

    def test_36_fault_injection_ref06_ref07_ref08_behavioral_invariants(self) -> None:
        # REF-06: raw fallback path or duplicate records
        docs_6 = self._clone_docs()
        chk06b = next(r for r in docs_6["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-06b")
        chk06b["crash_replay_stub"]["raw_text_fallback_path_exists"] = True
        chk06b["crash_replay_stub"]["duplicate_records_created"] = 1
        self.assertIn("REF06_CRASH_REPLAY_POLICY_VIOLATION", self._error_codes(docs_6))

        # REF-07: prior-check-only allowed OR TOCTOU allowed
        docs_7 = self._clone_docs()
        ro07d = next(r for r in docs_7["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if r["fixture_case_id"] == "refcase-07d")
        ro07d["expected_ref07_assertions"]["expected_operation_allowed"] = True
        ro07d["expected_approval_blocked"] = False
        self.assertIn("REF07_OPERATION_REFETCH_POLICY_VIOLATION", self._error_codes(docs_7))

        # REF-08: silent truncation on limit breach
        docs_8 = self._clone_docs()
        chk08c = next(r for r in docs_8["stages/stage4_refetch_checks.json"]["refetch_checks"] if r["fixture_case_id"] == "refcase-08c")
        chk08c["limits_and_boundary_stub"]["silent_truncation_occurred"] = True
        self.assertIn("REF08_LIMITS_MEMORY_BOUNDARY_VIOLATION", self._error_codes(docs_8))

    def test_37_claim05_claim06_claim08_pass_fail_pairs_verified(self) -> None:
        clean = self._clone_docs()
        claims_exp = {c["claim_id"]: c for c in clean["expected/oracle_draft.json"]["expected_claims"]}
        atoms_exp = {a["claim_atom_id"]: a for a in clean["expected/oracle_draft.json"]["expected_atoms"]}
        # CLAIM-05 pass vs fail
        self.assertTrue(claims_exp["claim-15-1"]["expected_source_verified_computed"])
        self.assertFalse(claims_exp["claim-15-2"]["expected_source_verified_computed"])
        # CLAIM-06 pass vs fail
        self.assertTrue(claims_exp["claim-16-1"]["expected_source_verified_computed"])
        self.assertFalse(claims_exp["claim-16-2"]["expected_source_verified_computed"])
        # CLAIM-08 4-case matrix (pass v2 re-verified vs fail v2 edit vs STALE v2 vs USER_CONFIRMED-only)
        self.assertTrue(claims_exp["claim-12-3"]["expected_source_verified_computed"])
        self.assertEqual(atoms_exp["atom-12-4"]["expected_domain_atom_status"], "SUPPORTED")
        self.assertFalse(claims_exp["claim-12-1"]["expected_source_verified_computed"])
        self.assertFalse(claims_exp["claim-12-2"]["expected_source_verified_computed"])
        self.assertEqual(atoms_exp["atom-12-2"]["expected_domain_atom_status"], "STALE")
        self.assertFalse(claims_exp["claim-12-4"]["expected_source_verified_computed"])
        self.assertEqual(atoms_exp["atom-12-6"]["expected_domain_atom_status"], "USER_CONFIRMED")

    # --- NEW CLAIM-QA-SPEC-FIX Mandatory Semantic Fault-Injection Tests ---

    def _semantic_error_codes(self, docs: dict[str, dict[str, Any]]) -> set[str]:
        """Return validation error codes EXCLUDING MANIFEST_HASH_MISMATCH so tests prove semantic rules fired."""
        return self._error_codes(docs) - {"MANIFEST_HASH_MISMATCH"}

    def test_38_fault_injection_orchestrator_evidence_linkage_rules(self) -> None:
        # Orchestrator Case 1: Mutate atom-15-1 direct support Fact to fact-16-3 (from another ConfirmedEvent!)
        docs_a = self._clone_docs()
        cas_15_1 = next(
            c for c in docs_a["stages/stage3_claim_assembly.json"]["claim_atom_supports"]
            if c["claim_atom_support_id"] == "cas-15-1"
        )
        cas_15_1["canonical_fact_id"] = "fact-16-3"
        sem_a = self._semantic_error_codes(docs_a)
        self.assertIn("CROSS_EVENT_EVIDENCE_MISMATCH", sem_a)
        self.assertIn("VALIDATOR_INPUT_EVIDENCE_MISMATCH", sem_a)
        self.assertIn("ROLE_EVIDENCE_VALIDATION_ERROR", sem_a)
        self.assertIn("ILLEGAL_SUPPORTED_ATOM", sem_a)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", sem_a)

        # Orchestrator Case 2: Empty claim-16-1 ClaimSupportValidator.input_version_refs
        docs_b = self._clone_docs()
        val_16_1 = next(
            r for r in docs_b["stages/stage3_claim_assembly.json"]["role_execution_runs"]
            if r["run_id"] == "run-claim-validate-claim-16-1-v1"
        )
        val_16_1["input_version_refs"] = []
        sem_b = self._semantic_error_codes(docs_b)
        self.assertIn("VALIDATOR_INPUT_EVIDENCE_MISMATCH", sem_b)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", sem_b)

        # Orchestrator Case 3: Replace fact-16-3 in claim-16-1 ClaimSupportValidator.input_version_refs with fact-15-1
        docs_c = self._clone_docs()
        fact_15_1_hash = next(
            f["fixture_classification"]["fixture_fact_sha256"]
            for f in docs_c["stages/stage2_fact_extraction.json"]["canonical_facts"]
            if f["canonical_fact_id"] == "fact-15-1"
        )
        val_16_1_c = next(
            r for r in docs_c["stages/stage3_claim_assembly.json"]["role_execution_runs"]
            if r["run_id"] == "run-claim-validate-claim-16-1-v1"
        )
        for ref in val_16_1_c["input_version_refs"]:
            if ref["entity_type"] == "CANONICAL_FACT" and ref["entity_id"] == "fact-16-3":
                ref["entity_id"] = "fact-15-1"
                ref["version_or_hash"] = fact_15_1_hash
        sem_c = self._semantic_error_codes(docs_c)
        self.assertIn("CROSS_EVENT_EVIDENCE_MISMATCH", sem_c)
        self.assertIn("VALIDATOR_INPUT_EVIDENCE_MISMATCH", sem_c)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", sem_c)

        # Case 4: Status string ('CONFIRMED' / 'SUPPORTED') used as version_or_hash identifier MUST fail semantically
        docs_d = self._clone_docs()
        gen_16_1 = next(
            r for r in docs_d["stages/stage3_claim_assembly.json"]["role_execution_runs"]
            if r["run_id"] == "run-claim-gen-claim-16-1-v1"
        )
        gen_16_1["input_version_refs"][0]["version_or_hash"] = "CONFIRMED"
        sem_d = self._semantic_error_codes(docs_d)
        self.assertIn("STATUS_USED_AS_VERSION_IDENTIFIER", sem_d)
        self.assertIn("VALIDATOR_INPUT_EVIDENCE_MISMATCH", sem_d)

        # Case 5: Broken verified_evidence_bindings on SOURCE_VERIFIED=true claim MUST fail semantically
        docs_e = self._clone_docs()
        val_16_1_e = next(
            r for r in docs_e["stages/stage3_claim_assembly.json"]["role_execution_runs"]
            if r["run_id"] == "run-claim-validate-claim-16-1-v1"
        )
        val_16_1_e["reverification_job_context"]["verified_evidence_bindings"] = []
        sem_e = self._semantic_error_codes(docs_e)
        self.assertIn("VALIDATOR_INPUT_EVIDENCE_MISMATCH", sem_e)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", sem_e)

    def test_39_fault_injection_claim05_tech_lead_missing_or_wrong_person_contribution(self) -> None:
        # Verify clean baseline binds claim-15-1 Tech Lead atom to Alex Kim + evt-career-15 + fs-15-1b contribution
        clean = self._clone_docs()
        f15_1 = next(
            f for f in clean["stages/stage2_fact_extraction.json"]["canonical_facts"]
            if f["canonical_fact_id"] == "fact-15-1"
        )
        rag = f15_1["role_attribution_grounding"]
        self.assertEqual(rag["representation_scope"], "FIXTURE_SEMANTICS_FOR_DB_MODEL_3_8_NOT_PRODUCT_TABLE")
        self.assertEqual(rag["confirmed_event_id"], "evt-career-15")
        self.assertEqual(rag["subject_person_ref"], "person-alex-kim-verified")
        self.assertEqual(rag["contribution_contributor_person_ref"], "person-alex-kim-verified")
        self.assertEqual(rag["active_contribution_fact_support_ids"], ["fs-15-1b"])
        self.assertTrue(rag["author_match_or_role_string_alone_insufficient"])

        # Case A: Same Tech Lead claim with missing active contribution evidence (author still Alex Kim & 'Tech Lead' text present!)
        docs_a = self._clone_docs()
        f15_1_a = next(
            f for f in docs_a["stages/stage2_fact_extraction.json"]["canonical_facts"]
            if f["canonical_fact_id"] == "fact-15-1"
        )
        f15_1_a["role_attribution_grounding"]["active_contribution_fact_support_ids"] = []
        sem_a = self._semantic_error_codes(docs_a)
        self.assertIn("ROLE_EVIDENCE_VALIDATION_ERROR", sem_a)
        self.assertIn("ILLEGAL_SUPPORTED_ATOM", sem_a)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", sem_a)

        # Case B: Same Tech Lead claim where the concrete contribution belongs to another person!
        docs_b = self._clone_docs()
        f15_1_b = next(
            f for f in docs_b["stages/stage2_fact_extraction.json"]["canonical_facts"]
            if f["canonical_fact_id"] == "fact-15-1"
        )
        f15_1_b["role_attribution_grounding"]["contribution_contributor_person_ref"] = "person-jordan-lee-other"
        sem_b = self._semantic_error_codes(docs_b)
        self.assertIn("ROLE_EVIDENCE_VALIDATION_ERROR", sem_b)
        self.assertIn("ILLEGAL_SUPPORTED_ATOM", sem_b)
        self.assertIn("ILLEGAL_SOURCE_VERIFIED", sem_b)

    def test_40_fault_injection_ref07_export_preconditions_and_contrast_cases(self) -> None:
        # Clean check: refcase-07b binds approved claim-02-1 v1 + verification metadata without raw quotes,
        # while refcase-07f (unapproved) and refcase-07g (v2 != approved v1) pass HTTP 200 refetch yet block Export!
        clean = self._clone_docs()
        snaps = {s["fixture_case_id"]: s for s in clean["sources/refetch_snapshots.json"]["refetch_snapshots"]}
        chks = {c["fixture_case_id"]: c for c in clean["stages/stage4_refetch_checks.json"]["refetch_checks"]}
        oracles = {o["fixture_case_id"]: o for o in clean["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"]}

        op07b = chks["refcase-07b"]["operation_refetch_stub"]
        self.assertTrue(op07b["export_precondition_input"]["claim_version_approved"])
        self.assertEqual(op07b["export_precondition_input"]["approved_claim_version"], 1)
        self.assertTrue(op07b["export_precondition_input"]["export_artifact_emitted"])
        vm07b = op07b["export_payload_snapshot"]["verification_metadata_fixture_only"]
        self.assertEqual(vm07b["layout_scope"], "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN")
        self.assertFalse(op07b["export_payload_snapshot"]["raw_source_quote_included"])

        for contrast_id in ("refcase-07f", "refcase-07g"):
            self.assertEqual(snaps[contrast_id]["http_status"], 200)
            self.assertTrue(chks[contrast_id]["hash_recheck_matched"])
            self.assertTrue(chks[contrast_id]["locator_recheck_matched"])
            op_c = chks[contrast_id]["operation_refetch_stub"]
            self.assertFalse(op_c["export_precondition_input"]["export_artifact_emitted"])
            self.assertIsNone(op_c["export_payload_snapshot"])
            self.assertFalse(oracles[contrast_id]["expected_ref07_assertions"]["expected_operation_allowed"])
            self.assertFalse(oracles[contrast_id]["expected_ref07_assertions"]["expected_export_artifact_emitted"])
            self.assertTrue(oracles[contrast_id]["expected_approval_blocked"])

        # Case A: Unapproved refcase-07f allowed to emit Export -> REF07_EXPORT_PRECONDITION_VIOLATION
        docs_a = self._clone_docs()
        chk07f = next(c for c in docs_a["stages/stage4_refetch_checks.json"]["refetch_checks"] if c["fixture_case_id"] == "refcase-07f")
        ro07f = next(o for o in docs_a["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if o["fixture_case_id"] == "refcase-07f")
        chk07f["operation_refetch_stub"]["export_precondition_input"]["export_artifact_emitted"] = True
        ro07f["expected_ref07_assertions"]["expected_operation_allowed"] = True
        ro07f["expected_ref07_assertions"]["expected_export_artifact_emitted"] = True
        ro07f["expected_approval_blocked"] = False
        self.assertIn("REF07_EXPORT_PRECONDITION_VIOLATION", self._semantic_error_codes(docs_a))

        # Case B: Version-mismatched refcase-07g (requested v2 vs approved v1) allowed to emit Export -> REF07_EXPORT_PRECONDITION_VIOLATION
        docs_b = self._clone_docs()
        chk07g = next(c for c in docs_b["stages/stage4_refetch_checks.json"]["refetch_checks"] if c["fixture_case_id"] == "refcase-07g")
        ro07g = next(o for o in docs_b["expected/refetch_oracle_draft.json"]["expected_refetch_fixtures"] if o["fixture_case_id"] == "refcase-07g")
        chk07g["operation_refetch_stub"]["export_precondition_input"]["export_artifact_emitted"] = True
        ro07g["expected_ref07_assertions"]["expected_operation_allowed"] = True
        ro07g["expected_ref07_assertions"]["expected_export_artifact_emitted"] = True
        ro07g["expected_approval_blocked"] = False
        self.assertIn("REF07_EXPORT_PRECONDITION_VIOLATION", self._semantic_error_codes(docs_b))

        # Case C: refcase-07b Export missing required verification metadata (checked_at / source_content_sha256) -> REF07_EXPORT_PRECONDITION_VIOLATION
        docs_c = self._clone_docs()
        chk07b = next(c for c in docs_c["stages/stage4_refetch_checks.json"]["refetch_checks"] if c["fixture_case_id"] == "refcase-07b")
        del chk07b["operation_refetch_stub"]["export_payload_snapshot"]["verification_metadata_fixture_only"]["checked_at"]
        self.assertIn("REF07_EXPORT_PRECONDITION_VIOLATION", self._semantic_error_codes(docs_c))


if __name__ == "__main__":
    unittest.main()
