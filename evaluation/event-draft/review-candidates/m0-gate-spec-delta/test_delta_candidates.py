#!/usr/bin/env python3
"""Deterministic unit & negative-mutation tests for M0-GATE spec-delta review candidates."""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

DELTA_DIR = Path(__file__).resolve().parent
DATASET_DIR = DELTA_DIR.parent.parent
sys.path.insert(0, str(DELTA_DIR))

import validate_delta_candidates as validator  # noqa: E402


class TestM0GateSpecDeltaCandidates(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((DELTA_DIR / "schemas" / "delta-candidate.schema.json").read_text(encoding="utf-8"))

    def _load_candidate(self, filename: str) -> dict:
        return json.loads((DELTA_DIR / "candidates" / filename).read_text(encoding="utf-8"))

    def test_valid_delta_pack_passes_and_baseline_untouched(self) -> None:
        issues = validator.validate_delta_pack(DELTA_DIR, DATASET_DIR)
        self.assertEqual([], issues, [i.format() for i in issues])

    def test_ev002_delta_rejects_frozen_wire_badge_or_no_pr_claim(self) -> None:
        bad_badge = self._load_candidate("DELTA-EV-002-direct-commit-confirmation-needed.json")
        bad_badge["expected_proposal"]["events"][0]["direct_commit_confirmation_expectation"]["spec_badge_text"] = "Unlinked Commit"
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_badge, self.schema, DATASET_DIR, "bad_badge")}
        self.assertIn("GAP1_EV002_CONFIRM_HINT", codes)

        bad_no_pr = self._load_candidate("DELTA-EV-002-direct-commit-confirmation-needed.json")
        bad_no_pr["expected_proposal"]["events"][0]["direct_commit_confirmation_expectation"]["no_match_observed_means_definitely_no_pr"] = True
        codes2 = {i.rule_code for i in validator.validate_candidate_data(bad_no_pr, self.schema, DATASET_DIR, "bad_no_pr")}
        self.assertIn("GAP1_EV002_CONFIRM_HINT", codes2)

    def test_trunk_non_default_main_and_pr_dedup_invariants(self) -> None:
        bad_branch = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        bad_branch["synthetic_sources"]["commits"][0]["on_default_branch"] = True
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_branch, self.schema, DATASET_DIR, "bad_branch")}
        self.assertIn("BRANCH_ASSOCIATION_RULES", codes)

        bad_dup = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        dup_ev = copy.deepcopy(bad_dup["expected_proposal"]["events"][2])
        dup_ev["event_id"] = "ev_cand_019_bad_dup_squash"
        dup_ev["source_commit_ids"] = ["cmt_syn_1903_squash_trunk"]
        bad_dup["expected_proposal"]["events"].append(dup_ev)
        bad_dup["expected_proposal"]["aggregation_check"]["total_countable_events"] += 1
        bad_dup["expected_proposal"]["aggregation_check"]["total_direct_commit_events"] += 1
        codes2 = {i.rule_code for i in validator.validate_candidate_data(bad_dup, self.schema, DATASET_DIR, "bad_dup")}
        self.assertIn("GAP2_DEFAULT_BRANCH_PR_DEDUP", codes2)

    def test_out_of_window_pr_dedup_chronology_and_no_direct_or_pr_emission(self) -> None:
        # 1. Ignoring the out-of-window provider PR association and emitting a DIRECT_COMMIT MUST fail
        bad_direct = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        dup_ev = copy.deepcopy(bad_direct["expected_proposal"]["events"][2])
        dup_ev["event_id"] = "ev_cand_019_bad_oow_direct"
        dup_ev["source_commit_ids"] = ["cmt_syn_1905_out_of_window_pr"]
        bad_direct["expected_proposal"]["events"].append(dup_ev)
        bad_direct["expected_proposal"]["aggregation_check"]["total_countable_events"] += 1
        bad_direct["expected_proposal"]["aggregation_check"]["total_direct_commit_events"] += 1
        codes1 = {i.rule_code for i in validator.validate_candidate_data(bad_direct, self.schema, DATASET_DIR, "bad_oow_direct")}
        self.assertIn("GAP2_OUT_OF_WINDOW_PR_DEDUP", codes1)
        self.assertIn("GAP2_DEFAULT_BRANCH_PR_DEDUP", codes1)

        # 2. Creating a new PR Event from the out-of-window PR MUST fail
        bad_pr_ev = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        oow_pr_ev = copy.deepcopy(bad_pr_ev["expected_proposal"]["events"][0])
        oow_pr_ev["event_id"] = "ev_cand_019_bad_oow_pr"
        oow_pr_ev["primary_pr_id"] = "pr_syn_1903_out_of_window"
        oow_pr_ev["source_commit_ids"] = ["cmt_syn_1905_out_of_window_pr"]
        bad_pr_ev["expected_proposal"]["events"].append(oow_pr_ev)
        bad_pr_ev["expected_proposal"]["aggregation_check"]["total_countable_events"] += 1
        codes2 = {i.rule_code for i in validator.validate_candidate_data(bad_pr_ev, self.schema, DATASET_DIR, "bad_oow_pr")}
        self.assertIn("GAP2_OUT_OF_WINDOW_PR_DEDUP", codes2)

        # 3. Chronological contradiction (PR merged BEFORE observed commit) MUST fail
        bad_chrono = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        bad_chrono["synthetic_sources"]["out_of_window_pr_associations"][0]["pr_merged_at"] = "2026-09-25T16:00:00Z"
        codes3 = {i.rule_code for i in validator.validate_candidate_data(bad_chrono, self.schema, DATASET_DIR, "bad_chrono")}
        self.assertIn("GAP2_OUT_OF_WINDOW_PR_DEDUP", codes3)

        # 4. Already-disqualified commit (committed outside window) MUST fail so rule isn't vacuously tested
        bad_disq = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        bad_disq["synthetic_sources"]["commits"][4]["committed_at"] = "2026-09-20T10:00:00Z"
        bad_disq["synthetic_sources"]["commits"][4]["within_import_window"] = False
        codes4 = {i.rule_code for i in validator.validate_candidate_data(bad_disq, self.schema, DATASET_DIR, "bad_disq")}
        self.assertIn("GAP2_OUT_OF_WINDOW_PR_DEDUP", codes4)

        # 5. Prescribing an internal persisted diagnostic held_item for out-of-window PR commit MUST fail
        bad_held = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        bad_held["expected_proposal"]["held_items"].append(
            {
                "held_id": "hold_bad_oow",
                "record_kind": "NON_ELIGIBLE_SOURCE_RECORD",
                "source_type": "COMMIT",
                "source_id": "cmt_syn_1905_out_of_window_pr",
                "logical_disposition": "not_direct_commit_candidate_non_default_branch",
                "spec_hold_status": "UNCONFIRMED",
                "hold_reason": "Improperly prescribed diagnostic persistence.",
                "interpreted_as_no_pr": False,
                "unconfirmed_fields": ["spec_hold_status"],
            }
        )
        bad_held["expected_proposal"]["aggregation_check"]["total_held_items"] += 1
        codes5 = {i.rule_code for i in validator.validate_candidate_data(bad_held, self.schema, DATASET_DIR, "bad_held")}
        self.assertIn("GAP2_OUT_OF_WINDOW_PR_DEDUP", codes5)

    def test_heuristic_link_of_no_match_decoy_commit_rejected(self) -> None:
        bad_heur = self._load_candidate("CAND-EV-019-trunk-default-branch-pr-dedup.json")
        bad_heur["expected_proposal"]["events"][0]["source_commit_ids"].append("cmt_syn_1906_direct_similar")
        bad_heur["expected_proposal"]["events"].pop(2)
        bad_heur["expected_proposal"]["aggregation_check"]["total_countable_events"] = 2
        bad_heur["expected_proposal"]["aggregation_check"]["total_direct_commit_events"] = 0
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_heur, self.schema, DATASET_DIR, "bad_heur")}
        self.assertIn("GAP2_NO_HEURISTIC_LINK", codes)

    def test_master_unknown_partial_run_promotes_zero_events(self) -> None:
        bad_partial = self._load_candidate("CAND-EV-020-master-default-branch-unknown-hold.json")
        bad_partial["expected_proposal"]["events"].append(
            {
                "event_id": "ev_bad_partial_promoted",
                "workspace_id": "ws_syn_020",
                "repo_id": "repo_syn_020",
                "anchor_mode": "PR",
                "lifecycle_status": "CANDIDATE",
                "is_active_top_level": True,
                "version": 1,
                "merge_origin": "NONE",
                "origin_command_id": None,
                "primary_pr_id": "pr_syn_2001",
                "source_commit_ids": ["cmt_syn_2003_pr_branch"],
                "context_ticket_ids": [],
                "member_event_ids": [],
                "parent_event_id": None,
                "membership_role": "standalone",
                "searchable_as_top_level": True,
                "counted_in_metrics": True,
                "unconfirmed_fields": [],
            }
        )
        bad_partial["expected_proposal"]["aggregation_check"]["total_countable_events"] = 1
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_partial, self.schema, DATASET_DIR, "bad_partial")}
        self.assertIn("GAP2_UNKNOWN_HOLD", codes)

    def test_cannot_link_requires_clean_preconditions_and_zero_mutation(self) -> None:
        bad_ver = self._load_candidate("CAND-EV-021-active-events-cannot-link-conflict.json")
        bad_ver["user_commands"][0]["target_versions"]["ev_init_021_b"] = 1
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_ver, self.schema, DATASET_DIR, "bad_ver")}
        self.assertIn("GAP3_ACTIVE_CANNOT_LINK", codes)

        bad_mut = self._load_candidate("CAND-EV-021-active-events-cannot-link-conflict.json")
        bad_mut["expected_proposal"]["command_results"][0]["state_mutated"] = True
        codes2 = {i.rule_code for i in validator.validate_candidate_data(bad_mut, self.schema, DATASET_DIR, "bad_mut")}
        self.assertIn("GAP3_ACTIVE_CANNOT_LINK", codes2)

        bad_http = self._load_candidate("CAND-EV-021-active-events-cannot-link-conflict.json")
        bad_http["expected_proposal"]["command_results"][0]["http_status"] = 409
        codes3 = {i.rule_code for i in validator.validate_candidate_data(bad_http, self.schema, DATASET_DIR, "bad_http")}
        self.assertIn("SCHEMA_VALIDATION", codes3)

    def test_confirm_requires_version_bump_review_action_and_no_invented_keep_api(self) -> None:
        # 1. Inventing UI label 'keep' as API command_action or route must fail
        bad_keep = self._load_candidate("CAND-EV-022-confirm-event-version-review-action.json")
        bad_keep["user_commands"][0]["command_action"] = "keep"
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_keep, self.schema, DATASET_DIR, "bad_keep")}
        self.assertIn("NO_INVENTED_UI_API_NAME", codes)

        bad_keep_route = self._load_candidate("CAND-EV-022-confirm-event-version-review-action.json")
        bad_keep_route["user_commands"][0]["spec_command_name"] = "POST /work-events/{id}/actions/keep"
        codes_route = {i.rule_code for i in validator.validate_candidate_data(bad_keep_route, self.schema, DATASET_DIR, "bad_keep_route")}
        self.assertIn("NO_INVENTED_UI_API_NAME", codes_route)
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes_route)

        # 2. Freezing unconfirmed external response action token must fail
        bad_ext_tok = self._load_candidate("CAND-EV-022-confirm-event-version-review-action.json")
        bad_ext_tok["expected_proposal"]["review_actions"][0]["external_response_action_token"] = "CONFIRMED_EVENT"
        codes_tok = {i.rule_code for i in validator.validate_candidate_data(bad_ext_tok, self.schema, DATASET_DIR, "bad_ext_tok")}
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes_tok)
        self.assertIn("SCHEMA_VALIDATION", codes_tok)

        # 3. Omitting ReviewAction or failing to increment version must fail
        bad_no_ra = self._load_candidate("CAND-EV-022-confirm-event-version-review-action.json")
        bad_no_ra["expected_proposal"]["review_actions"] = []
        bad_no_ra["expected_proposal"]["events"][0]["version"] = 1
        codes2 = {i.rule_code for i in validator.validate_candidate_data(bad_no_ra, self.schema, DATASET_DIR, "bad_no_ra")}
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes2)

    def test_edit_title_summary_is_distinct_from_merge_and_emits_review_action(self) -> None:
        bad_merge = self._load_candidate("CAND-EV-023-edit-event-title-summary-review-action.json")
        bad_merge["expected_proposal"]["events"][0]["anchor_mode"] = "USER_COMPOSITE"
        bad_merge["expected_proposal"]["events"][1]["lifecycle_status"] = "SUPERSEDED"
        codes = {i.rule_code for i in validator.validate_candidate_data(bad_merge, self.schema, DATASET_DIR, "bad_merge")}
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes)

    def test_edit_rejects_confirmed_or_unapproved_lifecycle_status(self) -> None:
        # 1. Re-introducing CONFIRMED -> CONFIRMED edit expectation MUST fail
        bad_conf = self._load_candidate("CAND-EV-023-edit-event-title-summary-review-action.json")
        bad_conf["synthetic_sources"]["initial_events"][0]["lifecycle_status"] = "CONFIRMED"
        bad_conf["expected_proposal"]["events"][0]["lifecycle_status"] = "CONFIRMED"
        codes1 = {i.rule_code for i in validator.validate_candidate_data(bad_conf, self.schema, DATASET_DIR, "bad_conf")}
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes1)

        # 2. Implicitly promoting CANDIDATE -> CONFIRMED during edit MUST fail
        bad_promo = self._load_candidate("CAND-EV-023-edit-event-title-summary-review-action.json")
        bad_promo["expected_proposal"]["events"][0]["lifecycle_status"] = "CONFIRMED"
        codes2 = {i.rule_code for i in validator.validate_candidate_data(bad_promo, self.schema, DATASET_DIR, "bad_promo")}
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes2)

        # 3. Re-introducing an unapproved lifecycle status (e.g., 'EDITED' or 'ACTIVE') MUST fail
        bad_unapproved = self._load_candidate("CAND-EV-023-edit-event-title-summary-review-action.json")
        bad_unapproved["expected_proposal"]["events"][0]["lifecycle_status"] = "EDITED"
        codes3 = {i.rule_code for i in validator.validate_candidate_data(bad_unapproved, self.schema, DATASET_DIR, "bad_unapproved")}
        self.assertIn("SCHEMA_VALIDATION", codes3)
        self.assertIn("GAP4_KEEP_EDIT_TRACKING", codes3)

    def test_agent_self_approval_same_source_bundle_split_and_hash_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_delta = Path(tmp) / "m0-gate-spec-delta"
            shutil.copytree(DELTA_DIR, tmp_delta)

            ev23_path = tmp_delta / "candidates" / "CAND-EV-023-edit-event-title-summary-review-action.json"
            ev23 = json.loads(ev23_path.read_text(encoding="utf-8"))
            ev23["eval_grouping"]["eval_bundle_id"] = "bundle_cand_split_illegal"
            ev23["expected_proposal"]["approval_status"] = "APPROVED_MVP_REGRESSION"
            ev23_path.write_text(json.dumps(ev23, indent=2) + "\n", encoding="utf-8")

            codes = {i.rule_code for i in validator.validate_delta_pack(tmp_delta, DATASET_DIR)}
            self.assertIn("INTEGRITY_HASH_MISMATCH", codes)
            self.assertIn("EVAL_BUNDLE_LINEAGE_ISOLATION", codes)
            self.assertIn("SCHEMA_VALIDATION", codes)


if __name__ == "__main__":
    unittest.main()
