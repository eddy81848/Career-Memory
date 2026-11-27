#!/usr/bin/env python3
"""Deterministic tests for approved 1.1.0-mvp-approved 21-case release & T-306A anti-misload guards."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

RELEASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = RELEASE_DIR.parent.parent
MANIFEST_PATH = RELEASE_DIR / "manifest.json"
sys.path.insert(0, str(RELEASE_DIR))

import validate_approved_release as rel  # noqa: E402


class TestApprovedRelease110(unittest.TestCase):
    def _write_temp_manifest(self, tmp_dir: str, data: dict) -> Path:
        p = Path(tmp_dir) / "manifest.json"
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return p

    def test_valid_1_1_0_release_passes_and_loads_exact_21_cases(self) -> None:
        issues = rel.validate_release_manifest(MANIFEST_PATH, DATASET_DIR)
        self.assertEqual([], issues, [i.format() for i in issues])

        loaded = rel.load_approved_21_case_set(MANIFEST_PATH, DATASET_DIR)
        self.assertEqual(21, len(loaded))
        ids = [c["case_id"] for c in loaded]
        self.assertEqual(21, len(set(ids)))
        self.assertNotIn("EV-017", ids)
        self.assertNotIn("EV-018", ids)

        ev002_items = [c for c in loaded if c["case_id"] == "EV-002"]
        self.assertEqual(1, len(ev002_items))
        self.assertEqual("DELTA-EV-002", ev002_items[0]["source_record_id"])
        self.assertTrue(ev002_items[0]["path"].endswith("DELTA-EV-002-direct-commit-confirmation-needed.json"))

        ev021 = next(c for c in loaded if c["case_id"] == "EV-021")
        self.assertEqual("POST /work-events/actions/merge", ev021["confirmed_api_route_handoff"])

    def test_reading_baseline_1_0_0_manifest_alone_is_rejected(self) -> None:
        # 1. Passing baseline 1.0.0 manifest directly to T-306A release loader MUST fail
        issues = rel.validate_release_manifest(DATASET_DIR / "manifest.json", DATASET_DIR)
        codes = {i.rule_code for i in issues}
        self.assertIn("BASELINE_ONLY_MISLOAD", codes)
        with self.assertRaises(ValueError):
            rel.load_approved_21_case_set(DATASET_DIR / "manifest.json", DATASET_DIR)

        # 2. Dropping new candidates EV-019..023 from release manifest MUST fail
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            m["approved_cases"] = [c for c in m["approved_cases"] if c["origin"] != "NEW_FROM_M0_GATE_DELTA"]
            mp = self._write_temp_manifest(tmp, m)
            codes2 = {i.rule_code for i in rel.validate_release_manifest(mp, DATASET_DIR)}
            self.assertIn("APPROVED_CASE_COUNT_MISMATCH", codes2)
            self.assertIn("MISSING_APPROVED_CASES", codes2)

    def test_naive_union_duplicating_ev002_or_using_old_ev002_is_rejected(self) -> None:
        # 1. Naive union including BOTH baseline EV-002 AND DELTA-EV-002 -> duplicate EV-002 caught
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            dup_ev002 = copy.deepcopy(m["approved_cases"][1])
            dup_ev002["source_record_id"] = "EV-002"
            dup_ev002["origin"] = "BASELINE_1_0_0"
            dup_ev002["path"] = "cases/EV-002-default-branch-direct-commit.json"
            dup_ev002["file_sha256"] = rel.EXPECTED_SUPERSEDED_EV002_SHA256
            m["approved_cases"].append(dup_ev002)
            mp = self._write_temp_manifest(tmp, m)
            codes = {i.rule_code for i in rel.validate_release_manifest(mp, DATASET_DIR)}
            self.assertIn("APPROVED_CASE_COUNT_MISMATCH", codes)
            self.assertIn("DUPLICATE_CASE_ID", codes)
            self.assertIn("SUPERSEDED_EV002_CO_LOADED", codes)

        # 2. Keeping old baseline EV-002 instead of DELTA-EV-002 MUST fail
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            for c in m["approved_cases"]:
                if c["case_id"] == "EV-002":
                    c["source_record_id"] = "EV-002"
                    c["origin"] = "BASELINE_1_0_0"
                    c["path"] = "cases/EV-002-default-branch-direct-commit.json"
                    c["file_sha256"] = rel.EXPECTED_SUPERSEDED_EV002_SHA256
            mp = self._write_temp_manifest(tmp, m)
            codes2 = {i.rule_code for i in rel.validate_release_manifest(mp, DATASET_DIR)}
            self.assertIn("SUPERSEDED_EV002_CO_LOADED", codes2)
            self.assertIn("EV002_NOT_REPLACED_BY_DELTA", codes2)

    def test_leaking_post_mvp_or_negative_fixture_into_release_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            m["approved_cases"].append(
                {
                    "case_id": "EV-017",
                    "source_record_id": "EV-017",
                    "origin": "BASELINE_1_0_0",
                    "path": "cases/EV-017-post-mvp-b003-auto-change-discovery.json",
                    "file_sha256": "4087c1feafc058751ba126d5ca5a4d49d859d51b91dec685fe683d2a3aa42bd3",
                    "eval_bundle_id": "bundle_post_mvp_b003_draft",
                    "source_lineage_group": "src_ledger_service",
                    "draft_requirement_ids": ["REQ-EV-POST-01"],
                    "draft_test_ids": ["T-001A-17"],
                    "official_requirement_refs": ["FR-006"],
                    "official_test_refs": [],
                    "verification_tests": [],
                }
            )
            m["evaluation_bundles"][0]["case_ids"].append("NEG-001")
            mp = self._write_temp_manifest(tmp, m)
            codes = {i.rule_code for i in rel.validate_release_manifest(mp, DATASET_DIR)}
            self.assertIn("EXCLUDED_CASE_LEAKED", codes)

    def test_invented_approver_metadata_and_missing_ev021_route_and_hash_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            m["approval_record"]["approved_by_name_or_account"] = "fake-owner-handle"
            m["approval_record"]["approved_at_utc"] = "2026-11-27T08:41:50Z"
            m["approval_record"]["comparison_policy"]["confirmed_api_routes_for_handoff"]["manual_merge"] = "UNCONFIRMED"
            m["integrity_hashes"]["approved_21_bundle_sha256"] = "0" * 64
            mp = self._write_temp_manifest(tmp, m)
            codes = {i.rule_code for i in rel.validate_release_manifest(mp, DATASET_DIR)}
            self.assertIn("NO_INVENTED_APPROVAL_METADATA", codes)
            self.assertIn("CAND_EV021_HANDOFF_ROUTE_REQUIRED", codes)
            self.assertIn("INTEGRITY_HASH_MISMATCH", codes)

    def test_splitting_same_source_bundle_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            for c in m["approved_cases"]:
                if c["case_id"] == "EV-023":
                    c["eval_bundle_id"] = "bundle_illegal_split"
            mp = self._write_temp_manifest(tmp, m)
            codes = {i.rule_code for i in rel.validate_release_manifest(mp, DATASET_DIR)}
            self.assertIn("EVAL_BUNDLE_LINEAGE_ISOLATION", codes)


if __name__ == "__main__":
    unittest.main()
