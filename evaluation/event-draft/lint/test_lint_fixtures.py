"""Automated unit and regression tests for T-001A Event synthetic fixture linter.

Validates:
1. All 18 existing draft cases pass format, referential, governance, and confirmed Event-domain invariants.
2. Every isolated negative fixture (NEG-001..NEG-006) fails with its expected rule codes.
3. Confirmed official spec rules are regression-tested via targeted mutations:
   - Unmerged PR cannot be a default merged_pr_event candidate.
   - Direct commit requires COMPLETED lookup + NO_MATCH_OBSERVED + default branch.
   - Cross-repository USER_COMPOSITE is forbidden.
   - Nested USER_COMPOSITE is forbidden.
   - Dissolving a USER_COMPOSITE with dependent Claims must mark them STALE.
   - Guessing unconfirmed status names or error codes is caught.
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

    def test_exact_18_cases_and_scope_split_still_draft(self) -> None:
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

    def test_all_isolated_negative_variants_fail_as_expected(self) -> None:
        ok, lines = verify_negative_variants(DATASET_DIR)
        self.assertTrue(ok, msg="\n".join(lines))
        self.assertEqual(len(lines), 6)

    def test_neg001_to_neg006_individual_rules(self) -> None:
        checks = [
            ("negative-fixtures/NEG-001-broken-id-ref.json", {"ID_REFERENCE_INTEGRITY"}),
            ("negative-fixtures/NEG-002-missing-required-field.json", {"SCHEMA_REQUIRED_FIELD"}),
            ("negative-fixtures/NEG-003-unknown-as-no-pr.json", {"INVARIANT_UNKNOWN_NOT_NO_PR"}),
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

    def test_unmerged_pr_candidate_rejected(self) -> None:
        ev001 = json.loads((DATASET_DIR / "cases/EV-001-pr-centered-basic.json").read_text(encoding="utf-8"))
        ev001["synthetic_sources"]["pull_requests"][0]["state"] = "OPEN"
        issues = lint_case_data(ev001, "mutated-EV-001")
        self.assertIn("INVARIANT_MERGED_PR_CANDIDATE", {i.rule_code for i in issues})

    def test_direct_commit_requires_completed_lookup_and_no_match_observed(self) -> None:
        ev002 = json.loads((DATASET_DIR / "cases/EV-002-default-branch-direct-commit.json").read_text(encoding="utf-8"))
        ev002["synthetic_sources"]["commits"][0]["pr_association_lookup_status"] = "INCOMPLETE_OR_FAILED"
        issues = lint_case_data(ev002, "mutated-EV-002-lookup")
        self.assertIn("INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED", {i.rule_code for i in issues})

        ev002_branch = json.loads((DATASET_DIR / "cases/EV-002-default-branch-direct-commit.json").read_text(encoding="utf-8"))
        ev002_branch["synthetic_sources"]["commits"][0]["on_default_branch"] = False
        issues_b = lint_case_data(ev002_branch, "mutated-EV-002-branch")
        codes_b = {i.rule_code for i in issues_b}
        self.assertIn("BRANCH_ASSOCIATION_RULES", codes_b)
        self.assertIn("INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED", codes_b)

    def test_cross_repo_manual_merge_acceptance_rejected(self) -> None:
        ev016 = json.loads((DATASET_DIR / "cases/EV-016-workspace-boundary-conflict.json").read_text(encoding="utf-8"))
        # Mutate same-workspace cross-repo command to ACCEPTED
        ev016["expected_proposal"]["command_results"][2]["outcome"] = "ACCEPTED"
        ev016["expected_proposal"]["command_results"][2]["rejection_category"] = None
        ev016["expected_proposal"]["command_results"][2]["spec_error_code"] = None
        ev016["expected_proposal"]["command_results"][2]["state_mutated"] = True
        issues = lint_case_data(ev016, "mutated-EV-016-cross-repo")
        self.assertIn("INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING", {i.rule_code for i in issues})

    def test_nested_user_composite_rejected(self) -> None:
        ev014 = json.loads((DATASET_DIR / "cases/EV-014-cannot-link-nested-and-excluded.json").read_text(encoding="utf-8"))
        ev014["expected_proposal"]["command_results"][1]["outcome"] = "ACCEPTED"
        ev014["expected_proposal"]["command_results"][1]["rejection_category"] = None
        ev014["expected_proposal"]["command_results"][1]["spec_error_code"] = None
        ev014["expected_proposal"]["command_results"][1]["state_mutated"] = True
        issues = lint_case_data(ev014, "mutated-EV-014-nested")
        self.assertIn("INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING", {i.rule_code for i in issues})

    def test_dissolve_missing_stale_dependent_claim_rejected(self) -> None:
        ev012 = json.loads((DATASET_DIR / "cases/EV-012-user-dissolve-composite.json").read_text(encoding="utf-8"))
        ev012["expected_proposal"]["claim_side_effects"] = []
        issues = lint_case_data(ev012, "mutated-EV-012-no-stale")
        self.assertIn("INVARIANT_DISSOLVE_MARKS_DEPENDENT_CLAIMS_STALE", {i.rule_code for i in issues})

    def test_guessed_unconfirmed_status_or_error_code_rejected(self) -> None:
        ev015 = json.loads((DATASET_DIR / "cases/EV-015-version-conflict-optimistic-lock.json").read_text(encoding="utf-8"))
        ev015["expected_proposal"]["command_results"][0]["spec_error_code"] = "VERSION_CONFLICT"
        issues = lint_case_data(ev015, "mutated-EV-015-guessed-err")
        self.assertIn("NO_GUESSED_UNCONFIRMED_SPEC_TOKENS", {i.rule_code for i in issues})

        ev003 = json.loads((DATASET_DIR / "cases/EV-003-unknown-association-hold.json").read_text(encoding="utf-8"))
        ev003["expected_proposal"]["held_items"][0]["spec_hold_status"] = "CLASSIFICATION_HELD"
        issues_h = lint_case_data(ev003, "mutated-EV-003-guessed-hold")
        self.assertIn("NO_GUESSED_UNCONFIRMED_SPEC_TOKENS", {i.rule_code for i in issues_h})


if __name__ == "__main__":
    unittest.main()
