"""Automated unit and regression tests for T-001A Event synthetic fixture linter.

Validates:
1. All 18 existing draft cases pass format, referential, governance, and official Event-domain invariants.
2. Exact 18-case count (16 MVP + 2 POST_MVP/B-003), DRAFT-only status, and official FR-006/FR-007/EVENT-10/11/12 traceability.
3. Every isolated negative fixture (NEG-001..NEG-006) fails with its expected rule codes.
4. Official spec rules are regression-tested via targeted mutations:
   - PARTIAL_SUCCESS rejected & PARTIAL run promoting an Event rejected.
   - Arbitrary ACTIVE / EXCLUDED / DISSOLVED lifecycle statuses rejected.
   - Unmerged PR cannot be a PR / PR_SEGMENT candidate.
   - DIRECT_COMMIT requires COMPLETED lookup + NO_MATCH_OBSERVED + default branch verified at start and end.
   - Overlapping split evidence_scope rejected & PR split must emit PR_SEGMENT.
   - Active USER_COMPOSITE members must be SUPERSEDED.
   - Cross-repository USER_COMPOSITE & nested USER_COMPOSITE acceptance rejected.
   - Dissolve requires idempotency_key and must mark dependent FACT, CLAIM, and EXPORT as STALE.
   - Catch-all CANNOT_LINK wire code and guessed hold tokens rejected.
   - Non-merge cross-workspace reject cannot use merge-only error codes, and multi-workspace cases must separate per-workspace counts.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lint_fixtures import (
    lint_case_data,
    lint_case_file,
    lint_dataset,
    verify_negative_variants,
)

DATASET_DIR = Path(__file__).resolve().parent.parent


class TestEventDraftFixtureLint(unittest.TestCase):
    def test_valid_draft_dataset_passes_lint(self) -> None:
        issues = lint_dataset(DATASET_DIR)
        self.assertEqual(
            issues,
            [],
            msg="Valid draft dataset produced unexpected lint issues:\n"
            + "\n".join(i.format() for i in issues),
        )

    def test_exact_18_cases_scope_traceability_and_still_draft(self) -> None:
        manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["approval_status"], "DRAFT")
        cases = manifest["cases"]
        self.assertEqual(len(cases), 18)
        mvp_cases = [c for c in cases if c["scope"] == "MVP"]
        post_mvp_cases = [c for c in cases if c["scope"] == "POST_MVP"]
        self.assertEqual(len(mvp_cases), 16)
        self.assertEqual(len(post_mvp_cases), 2)
        for case in post_mvp_cases:
            self.assertEqual(case["backlog_ref"], "B-003")

        by_test_ref: dict[str, list[str]] = {}
        for c in cases:
            for tref in c["official_test_refs"]:
                by_test_ref.setdefault(tref, []).append(c["case_id"])
            cdata = json.loads((DATASET_DIR / c["path"]).read_text(encoding="utf-8"))
            self.assertEqual(cdata["expected_proposal"]["approval_status"], "DRAFT")
            self.assertIsNone(cdata["expected_proposal"]["approved_by"])

        self.assertEqual(by_test_ref["EVENT-10"], ["EV-001", "EV-002", "EV-003", "EV-004"])
        self.assertEqual(by_test_ref["EVENT-11"], ["EV-009", "EV-010"])
        self.assertEqual(by_test_ref["EVENT-12"], ["EV-012", "EV-013", "EV-014", "EV-015", "EV-016"])

    def test_all_isolated_negative_variants_fail_as_expected(self) -> None:
        ok, lines = verify_negative_variants(DATASET_DIR)
        self.assertTrue(ok, msg="\n".join(lines))
        self.assertEqual(len(lines), 6)

    def test_neg001_to_neg006_individual_rules(self) -> None:
        checks = [
            ("negative-fixtures/NEG-001-broken-id-ref.json", {"ID_REFERENCE_INTEGRITY"}),
            ("negative-fixtures/NEG-002-missing-required-field.json", {"SCHEMA_REQUIRED_FIELD"}),
            (
                "negative-fixtures/NEG-003-unknown-as-no-pr.json",
                {"INVARIANT_UNKNOWN_NOT_NO_PR", "INVARIANT_PARTIAL_RUN_NO_PROMOTION"},
            ),
            ("negative-fixtures/NEG-004-composite-double-count.json", {"INVARIANT_NO_COMPOSITE_DOUBLE_COUNT"}),
            ("negative-fixtures/NEG-005-context-only-auto-merge.json", {"INVARIANT_NO_CONTEXT_AUTO_MERGE"}),
            (
                "negative-fixtures/NEG-006-premature-approval-and-od007.json",
                {"APPROVAL_STATUS_MUST_BE_DRAFT", "SCOPE_TAG_POLICY", "OD007_NO_PHYSICAL_TABLE"},
            ),
        ]
        for rel_path, expected in checks:
            issues = lint_case_file(DATASET_DIR / rel_path)
            codes = {i.rule_code for i in issues}
            for code in expected:
                self.assertIn(code, codes, msg=f"{rel_path} did not trigger {code}; got {codes}")

    def test_duplicate_case_id_caught_in_temp_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp) / "event-draft"
            shutil.copytree(DATASET_DIR, tmp_dir)
            manifest_path = tmp_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"].append(copy.deepcopy(manifest["cases"][0]))
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            issues = lint_dataset(tmp_dir)
            self.assertIn("CASE_UNIQUENESS", {i.rule_code for i in issues})

    def test_partial_success_and_partial_promotion_rejected(self) -> None:
        ev003 = json.loads((DATASET_DIR / "cases/EV-003-unknown-association-hold.json").read_text(encoding="utf-8"))
        ev003["synthetic_sources"]["sync_runs"][0]["status"] = "PARTIAL_SUCCESS"
        issues = lint_case_data(ev003, "mutated-EV-003-partial-success")
        self.assertIn("OFFICIAL_IMPORT_RUN_STATUS", {i.rule_code for i in issues})

        ev003_prom = json.loads((DATASET_DIR / "cases/EV-003-unknown-association-hold.json").read_text(encoding="utf-8"))
        ev003_prom["synthetic_sources"]["sync_runs"][0]["promotes_events"] = True
        issues_p = lint_case_data(ev003_prom, "mutated-EV-003-promotes")
        self.assertIn("INVARIANT_PARTIAL_RUN_NO_PROMOTION", {i.rule_code for i in issues_p})

    def test_arbitrary_lifecycle_statuses_rejected(self) -> None:
        for bad_status in ("ACTIVE", "EXCLUDED", "DISSOLVED"):
            ev001 = json.loads((DATASET_DIR / "cases/EV-001-pr-centered-basic.json").read_text(encoding="utf-8"))
            ev001["expected_proposal"]["events"][0]["lifecycle_status"] = bad_status
            issues = lint_case_data(ev001, f"mutated-EV-001-{bad_status}")
            self.assertIn("OFFICIAL_LIFECYCLE_STATUS", {i.rule_code for i in issues})

    def test_unmerged_pr_candidate_rejected(self) -> None:
        ev001 = json.loads((DATASET_DIR / "cases/EV-001-pr-centered-basic.json").read_text(encoding="utf-8"))
        ev001["synthetic_sources"]["pull_requests"][0]["state"] = "OPEN"
        issues = lint_case_data(ev001, "mutated-EV-001")
        self.assertIn("INVARIANT_MERGED_PR_CANDIDATE", {i.rule_code for i in issues})

    def test_direct_commit_requires_completed_lookup_no_match_and_start_end_verification(self) -> None:
        ev002 = json.loads((DATASET_DIR / "cases/EV-002-default-branch-direct-commit.json").read_text(encoding="utf-8"))
        ev002["synthetic_sources"]["commits"][0]["pr_association_lookup_status"] = "INCOMPLETE_OR_FAILED"
        issues = lint_case_data(ev002, "mutated-EV-002-lookup")
        self.assertIn("INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED", {i.rule_code for i in issues})

        ev002_ver = json.loads((DATASET_DIR / "cases/EV-002-default-branch-direct-commit.json").read_text(encoding="utf-8"))
        ev002_ver["synthetic_sources"]["commits"][0]["default_branch_verified_at_start_and_end"] = False
        issues_v = lint_case_data(ev002_ver, "mutated-EV-002-start-end")
        self.assertIn("INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED", {i.rule_code for i in issues_v})

    def test_overlapping_split_evidence_scope_rejected(self) -> None:
        ev008 = json.loads((DATASET_DIR / "cases/EV-008-user-split-mixed-pr.json").read_text(encoding="utf-8"))
        overlap_path = ev008["user_commands"][0]["payload"]["partitions"][0]["evidence_scope"]["file_paths"][0]
        ev008["user_commands"][0]["payload"]["partitions"][1]["evidence_scope"]["file_paths"].append(overlap_path)
        ev008["expected_proposal"]["events"][2]["evidence_scope"]["file_paths"].append(overlap_path)
        issues = lint_case_data(ev008, "mutated-EV-008-overlap")
        self.assertIn("INVARIANT_SPLIT_DISJOINT_SCOPE", {i.rule_code for i in issues})

    def test_subsumed_composite_member_must_be_superseded(self) -> None:
        ev009 = json.loads((DATASET_DIR / "cases/EV-009-user-manual-merge-composite.json").read_text(encoding="utf-8"))
        ev009["expected_proposal"]["events"][1]["lifecycle_status"] = "CANDIDATE"
        issues = lint_case_data(ev009, "mutated-EV-009-not-superseded")
        self.assertIn("INVARIANT_SUPERSEDED_HISTORY", {i.rule_code for i in issues})

    def test_cross_repo_manual_merge_and_nested_composite_acceptance_rejected(self) -> None:
        ev016 = json.loads((DATASET_DIR / "cases/EV-016-workspace-boundary-conflict.json").read_text(encoding="utf-8"))
        ev016["expected_proposal"]["command_results"][2]["outcome"] = "ACCEPTED"
        ev016["expected_proposal"]["command_results"][2]["rejection_category"] = None
        ev016["expected_proposal"]["command_results"][2]["spec_error_code"] = None
        ev016["expected_proposal"]["command_results"][2]["state_mutated"] = True
        issues = lint_case_data(ev016, "mutated-EV-016-cross-repo")
        self.assertIn("INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING", {i.rule_code for i in issues})

        ev014 = json.loads((DATASET_DIR / "cases/EV-014-cannot-link-nested-and-excluded.json").read_text(encoding="utf-8"))
        ev014["expected_proposal"]["command_results"][1]["outcome"] = "ACCEPTED"
        ev014["expected_proposal"]["command_results"][1]["rejection_category"] = None
        ev014["expected_proposal"]["command_results"][1]["spec_error_code"] = None
        ev014["expected_proposal"]["command_results"][1]["state_mutated"] = True
        issues_n = lint_case_data(ev014, "mutated-EV-014-nested")
        self.assertIn("INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING", {i.rule_code for i in issues_n})

    def test_dissolve_requires_idempotency_key_and_marks_fact_claim_export_stale(self) -> None:
        ev012_idem = json.loads((DATASET_DIR / "cases/EV-012-user-dissolve-composite.json").read_text(encoding="utf-8"))
        ev012_idem["user_commands"][0]["idempotency_key"] = ""
        issues_i = lint_case_data(ev012_idem, "mutated-EV-012-no-idem")
        self.assertIn("COMMAND_VERSION_RULES", {i.rule_code for i in issues_i})

        ev012_stale = json.loads((DATASET_DIR / "cases/EV-012-user-dissolve-composite.json").read_text(encoding="utf-8"))
        # Remove EXPORT side effect
        ev012_stale["expected_proposal"]["downstream_side_effects"] = [
            e for e in ev012_stale["expected_proposal"]["downstream_side_effects"] if e["artifact_type"] != "EXPORT"
        ]
        issues_s = lint_case_data(ev012_stale, "mutated-EV-012-missing-export-stale")
        self.assertIn("INVARIANT_DISSOLVE_MARKS_DEPENDENT_ARTIFACTS_STALE", {i.rule_code for i in issues_s})

    def test_catch_all_cannot_link_wire_code_and_guessed_hold_token_rejected(self) -> None:
        ev013 = json.loads((DATASET_DIR / "cases/EV-013-cannot-link-context-and-unknown.json").read_text(encoding="utf-8"))
        ev013["expected_proposal"]["command_results"][0]["spec_error_code"] = "CANNOT_LINK"
        issues = lint_case_data(ev013, "mutated-EV-013-cannot-link-wire")
        self.assertIn("NO_GUESSED_UNCONFIRMED_SPEC_TOKENS", {i.rule_code for i in issues})

        ev003 = json.loads((DATASET_DIR / "cases/EV-003-unknown-association-hold.json").read_text(encoding="utf-8"))
        ev003["expected_proposal"]["held_items"][0]["spec_hold_status"] = "CLASSIFICATION_HELD"
        issues_h = lint_case_data(ev003, "mutated-EV-003-guessed-hold")
        self.assertIn("NO_GUESSED_UNCONFIRMED_SPEC_TOKENS", {i.rule_code for i in issues_h})

    def test_non_merge_cannot_use_merge_only_error_and_workspace_counts_separated(self) -> None:
        ev016_err = json.loads((DATASET_DIR / "cases/EV-016-workspace-boundary-conflict.json").read_text(encoding="utf-8"))
        ev016_err["expected_proposal"]["command_results"][1]["spec_error_code"] = "CROSS_WORKSPACE_MEMBER"
        issues_e = lint_case_data(ev016_err, "mutated-EV-016-merge-err-on-exclude")
        self.assertIn("COMMAND_VERSION_RULES", {i.rule_code for i in issues_e})

        ev016_ws = json.loads((DATASET_DIR / "cases/EV-016-workspace-boundary-conflict.json").read_text(encoding="utf-8"))
        del ev016_ws["expected_proposal"]["aggregation_check"]["per_workspace_countable_events"]
        issues_w = lint_case_data(ev016_ws, "mutated-EV-016-missing-per-ws")
        self.assertIn("WORKSPACE_AGGREGATION_SEPARATION", {i.rule_code for i in issues_w})


if __name__ == "__main__":
    unittest.main()
