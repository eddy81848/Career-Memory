"""Automated unit tests for T-001A Event synthetic fixture linter.

Validates:
1. The checked-in draft dataset passes all format, referential, governance, and invariant checks.
2. Every isolated negative fixture (NEG-001..NEG-006) fails with its expected lint rule codes.
3. Duplicate case_id and stray unlisted case files are caught by CASE_UNIQUENESS.
4. No valid draft case interprets UNKNOWN as NO_PR, double-counts composite members, or auto-merges via CONTEXT_ONLY.
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

    def test_minimum_case_count_and_scope_split(self) -> None:
        manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
        cases = manifest["cases"]
        self.assertGreaterEqual(len(cases), 16)
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

    def test_neg001_broken_id_reference_caught(self) -> None:
        issues = lint_case_file(DATASET_DIR / "negative-fixtures/NEG-001-broken-id-ref.json")
        codes = {i.rule_code for i in issues}
        self.assertIn("ID_REFERENCE_INTEGRITY", codes)

    def test_neg002_missing_required_fields_caught(self) -> None:
        issues = lint_case_file(DATASET_DIR / "negative-fixtures/NEG-002-missing-required-field.json")
        codes = {i.rule_code for i in issues}
        self.assertIn("SCHEMA_REQUIRED_FIELD", codes)

    def test_neg003_unknown_as_no_pr_caught(self) -> None:
        issues = lint_case_file(DATASET_DIR / "negative-fixtures/NEG-003-unknown-as-no-pr.json")
        codes = {i.rule_code for i in issues}
        self.assertIn("INVARIANT_UNKNOWN_NOT_NO_PR", codes)

    def test_neg004_composite_double_count_caught(self) -> None:
        issues = lint_case_file(DATASET_DIR / "negative-fixtures/NEG-004-composite-double-count.json")
        codes = {i.rule_code for i in issues}
        self.assertIn("INVARIANT_NO_COMPOSITE_DOUBLE_COUNT", codes)

    def test_neg005_context_only_auto_merge_caught(self) -> None:
        issues = lint_case_file(DATASET_DIR / "negative-fixtures/NEG-005-context-only-auto-merge.json")
        codes = {i.rule_code for i in issues}
        self.assertIn("INVARIANT_NO_CONTEXT_AUTO_MERGE", codes)

    def test_neg006_approval_scope_and_od007_caught(self) -> None:
        issues = lint_case_file(DATASET_DIR / "negative-fixtures/NEG-006-premature-approval-and-od007.json")
        codes = {i.rule_code for i in issues}
        self.assertIn("APPROVAL_STATUS_MUST_BE_DRAFT", codes)
        self.assertIn("SCOPE_TAG_POLICY", codes)
        self.assertIn("OD007_NO_PHYSICAL_TABLE", codes)

    def test_duplicate_case_id_caught_in_temp_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp) / "event-draft"
            shutil.copytree(DATASET_DIR, tmp_dir)
            manifest_path = tmp_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            dup_entry = copy.deepcopy(manifest["cases"][0])
            manifest["cases"].append(dup_entry)
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            issues = lint_dataset(tmp_dir)
            codes = {i.rule_code for i in issues}
            self.assertIn("CASE_UNIQUENESS", codes)

    def test_branch_default_mismatch_and_version_conflict_rule(self) -> None:
        ev002 = json.loads((DATASET_DIR / "cases/EV-002-default-branch-direct-commit.json").read_text(encoding="utf-8"))
        ev002["synthetic_sources"]["commits"][0]["on_default_branch"] = False
        issues = lint_case_data(ev002, "mutated-EV-002")
        self.assertIn("BRANCH_ASSOCIATION_RULES", {i.rule_code for i in issues})

        ev015 = json.loads((DATASET_DIR / "cases/EV-015-version-conflict-optimistic-lock.json").read_text(encoding="utf-8"))
        ev015["expected_proposal"]["command_results"][0]["outcome"] = "ACCEPTED"
        ev015["expected_proposal"]["command_results"][0]["error_code"] = None
        ev015["expected_proposal"]["command_results"][0]["state_mutated"] = True
        issues_v = lint_case_data(ev015, "mutated-EV-015")
        self.assertIn("COMMAND_VERSION_RULES", {i.rule_code for i in issues_v})


if __name__ == "__main__":
    unittest.main()
