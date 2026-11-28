"""Strictly READ-ONLY Frozen Oracle Comparator for 1.1.0-mvp-approved (T-306A).

ABSOLUTE PROHIBITION (ADR-018, T-306A Step 5):
This module NEVER writes, mutates, or auto-updates any file under
``evaluation/event-draft/**`` or any expected oracle structure.
All comparisons strictly respect T-306A-HANDOFF.md §3 exclusions while
verifying every confirmed logical expectation against real PostgreSQL 18 outputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from career_memory_api.event_eval.models import (
    CaseOracleComparison,
    CommandExecutionRecord,
    ObservedCaseSnapshot,
    ObservedWorkEvent,
    OracleDiffEntry,
)


def _match_observed_events_to_expected(
    *,
    expected_events: list[dict[str, Any]],
    observed_events: tuple[ObservedWorkEvent, ...],
) -> tuple[dict[str, ObservedWorkEvent], list[ObservedWorkEvent]]:
    """Deterministically align dynamic runtime WorkEvent UUIDs to expected logical event IDs."""
    remaining = list(observed_events)
    matched: dict[str, ObservedWorkEvent] = {}

    # Pass 1: exact logical_event_id match (pre-seeded initial_events)
    for exp in expected_events:
        exp_id = str(exp["event_id"])
        for idx, obs in enumerate(remaining):
            if obs.logical_event_id == exp_id:
                matched[exp_id] = obs
                remaining.pop(idx)
                break

    # Pass 2: semantic anchor matching for newly created events (builder, split, merge)
    for exp in expected_events:
        exp_id = str(exp["event_id"])
        if exp_id in matched:
            continue

        exp_mode = str(exp["anchor_mode"])
        exp_pr = exp.get("primary_pr_id")
        exp_commits = set(exp.get("source_commit_ids") or [])
        exp_members = set(exp.get("member_event_ids") or [])
        exp_title = str(exp.get("title") or exp.get("title_hint") or "").strip()

        best_idx: int | None = None
        best_score = -1
        for idx, obs in enumerate(remaining):
            if obs.anchor_mode != exp_mode:
                continue
            if obs.workspace_id != str(exp["workspace_id"]):
                continue
            score = 0
            if exp_pr is not None and obs.primary_pr_id == exp_pr:
                score += 10
            if exp_commits and set(obs.source_commit_ids) == exp_commits:
                score += 20
            if exp_members and set(obs.member_event_ids) == exp_members:
                score += 25
            if exp_title and obs.title.strip() == exp_title:
                score += 15
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is not None:
            matched[exp_id] = remaining.pop(best_idx)

    return matched, remaining


def compare_case_against_frozen_oracle(
    *,
    case_id: str,
    case_data: Mapping[str, Any],
    observed: ObservedCaseSnapshot,
    command_records: tuple[CommandExecutionRecord, ...],
) -> CaseOracleComparison:
    """Strictly read-only comparison of observed PG18 state vs frozen expected_proposal."""
    expected = case_data.get("expected_proposal") or {}
    diffs: list[OracleDiffEntry] = []
    excluded_notes: list[str] = []
    checks = 0

    def _check(path: str, exp_val: Any, act_val: Any, msg: str) -> None:
        nonlocal checks
        checks += 1
        if exp_val != act_val:
            diffs.append(
                OracleDiffEntry(
                    path=path,
                    expected=exp_val,
                    actual=act_val,
                    message=msg,
                )
            )

    # 1. Events 1:1 bijection & per-event confirmed logical fields
    exp_events: list[dict[str, Any]] = list(expected.get("events") or [])
    matched_events, unmatched_obs = _match_observed_events_to_expected(
        expected_events=exp_events,
        observed_events=observed.events,
    )
    uuid_to_expected_id: dict[str, str] = {
        obs.event_uuid: exp_id for exp_id, obs in matched_events.items()
    }

    _check(
        "events.count",
        len(exp_events),
        len(observed.events),
        "Total WorkEvent count mismatch",
    )
    for extra in unmatched_obs:
        checks += 1
        diffs.append(
            OracleDiffEntry(
                path=f"events.unexpected[{extra.event_uuid}]",
                expected="<ABSENT>",
                actual={
                    "anchor_mode": extra.anchor_mode,
                    "status": extra.lifecycle_status,
                    "title": extra.title,
                },
                message="Unexpected extra WorkEvent produced",
            )
        )

    for exp_ev in exp_events:
        exp_id = str(exp_ev["event_id"])
        obs = matched_events.get(exp_id)
        prefix = f"events[{exp_id}]"
        if obs is None:
            checks += 1
            diffs.append(
                OracleDiffEntry(
                    path=prefix,
                    expected=exp_ev["anchor_mode"],
                    actual="<MISSING>",
                    message="Expected WorkEvent was not found in PG18",
                )
            )
            continue

        _check(f"{prefix}.workspace_id", exp_ev["workspace_id"], obs.workspace_id, "Workspace mismatch")
        _check(f"{prefix}.repo_id", exp_ev["repo_id"], obs.repo_id, "Repository mismatch")
        _check(f"{prefix}.anchor_mode", exp_ev["anchor_mode"], obs.anchor_mode, "anchor_mode mismatch")
        _check(
            f"{prefix}.lifecycle_status",
            exp_ev["lifecycle_status"],
            obs.lifecycle_status,
            "lifecycle_status mismatch",
        )
        _check(
            f"{prefix}.is_active_top_level",
            bool(exp_ev["is_active_top_level"]),
            obs.is_active_top_level,
            "is_active_top_level mismatch",
        )
        _check(f"{prefix}.version", int(exp_ev["version"]), obs.version, "version mismatch")
        _check(
            f"{prefix}.primary_pr_id",
            exp_ev.get("primary_pr_id"),
            obs.primary_pr_id,
            "primary_pr_id mismatch",
        )
        _check(
            f"{prefix}.source_commit_ids",
            sorted(exp_ev.get("source_commit_ids") or []),
            sorted(obs.source_commit_ids),
            "MUST_LINK source_commit_ids mismatch",
        )
        _check(
            f"{prefix}.context_ticket_ids",
            sorted(exp_ev.get("context_ticket_ids") or []),
            sorted(obs.context_ticket_ids),
            "CONTEXT_ONLY context_ticket_ids mismatch",
        )
        # Ensure CONTEXT_ONLY tickets and CANNOT_LINK sources never contaminate MUST_LINK commits
        overlap_ctx = sorted(set(obs.source_commit_ids) & set(obs.context_ticket_ids))
        _check(
            f"{prefix}.must_link_context_disjoint",
            [],
            overlap_ctx,
            "CONTEXT_ONLY ticket leaked into MUST_LINK source_commit_ids",
        )
        overlap_cl = sorted(set(obs.source_commit_ids) & set(obs.cannot_link_source_ids))
        _check(
            f"{prefix}.must_link_cannot_link_disjoint",
            [],
            overlap_cl,
            "CANNOT_LINK source leaked into MUST_LINK source_commit_ids",
        )

        _check(
            f"{prefix}.member_event_ids",
            sorted(exp_ev.get("member_event_ids") or []),
            sorted(obs.member_event_ids),
            "member_event_ids mismatch",
        )
        if exp_ev.get("parent_event_id") is not None:
            actual_parent = (
                uuid_to_expected_id.get(obs.parent_event_id, obs.parent_event_id)
                if obs.parent_event_id is not None
                else None
            )
            _check(
                f"{prefix}.parent_event_id",
                exp_ev["parent_event_id"],
                actual_parent,
                "parent_event_id mismatch",
            )
        if exp_ev["anchor_mode"] == "USER_COMPOSITE":
            # T-304B absolute invariant: USER_COMPOSITE has 0 direct MUST_LINK rows
            _check(
                f"{prefix}.composite_direct_must_link_count",
                0,
                obs.composite_direct_must_link_count,
                "USER_COMPOSITE must have 0 direct MUST_LINK event_memberships rows",
            )
            if "composite_membership_status" in exp_ev and exp_ev["composite_membership_status"] is not None:
                _check(
                    f"{prefix}.composite_membership_status",
                    exp_ev["composite_membership_status"],
                    obs.composite_membership_status,
                    "composite_membership_status mismatch",
                )
            if exp_ev.get("member_snapshots_preserved") is True:
                _check(
                    f"{prefix}.member_snapshots_preserved",
                    True,
                    obs.member_snapshots_preserved,
                    "Immutable member snapshots not preserved in work_event_versions",
                )
        if exp_ev.get("prior_version_preserved") is True:
            _check(
                f"{prefix}.prior_version_preserved",
                True,
                obs.prior_version_preserved,
                "Prior immutable version snapshot not preserved in work_event_versions",
            )
        if "searchable_as_top_level" in exp_ev:
            _check(
                f"{prefix}.searchable_as_top_level",
                bool(exp_ev["searchable_as_top_level"]),
                obs.searchable_as_top_level,
                "searchable_as_top_level mismatch",
            )
        if "counted_in_metrics" in exp_ev:
            _check(
                f"{prefix}.counted_in_metrics",
                bool(exp_ev["counted_in_metrics"]),
                obs.counted_in_metrics,
                "counted_in_metrics mismatch",
            )
        if isinstance(exp_ev.get("title"), str):
            _check(f"{prefix}.title", exp_ev["title"], obs.title, "curated title mismatch")
        if isinstance(exp_ev.get("summary"), str):
            _check(f"{prefix}.summary", exp_ev["summary"], obs.summary, "curated summary mismatch")

        # EV-007 mixed-purpose review_flags semantic check (exact_flag_token excluded per HANDOFF §3)
        exp_rf = exp_ev.get("review_flags")
        if isinstance(exp_rf, Mapping):
            if exp_rf.get("mixed_purpose_hint_present") is True:
                _check(
                    f"{prefix}.review_flags.mixed_purpose_hint_present",
                    True,
                    len(obs.review_flags) > 0,
                    "Mixed-purpose PR must surface a review flag",
                )
                excluded_notes.append(f"{prefix}.review_flags.exact_flag_token (HANDOFF §3)")
            if "auto_split_count" in exp_rf:
                actual_auto_splits = sum(
                    1 for e in observed.events if e.anchor_mode == "PR_SEGMENT"
                )
                _check(
                    f"{prefix}.review_flags.auto_split_count",
                    int(exp_rf["auto_split_count"]),
                    actual_auto_splits,
                    "Mixed-purpose PR must perform 0 automatic splits",
                )

        # DELTA-EV-002 & CAND-EV-019 direct-commit confirmation semantic check (wire names excluded)
        exp_dc = exp_ev.get("direct_commit_confirmation_expectation")
        if isinstance(exp_dc, Mapping):
            _check(
                f"{prefix}.direct_commit_confirmation_expectation.requires_user_association_confirmation",
                bool(exp_dc["requires_user_association_confirmation"]),
                obs.requires_user_confirmation,
                "DIRECT_COMMIT candidate must require user association confirmation",
            )
            _check(
                f"{prefix}.direct_commit_confirmation_expectation.no_match_observed_means_definitely_no_pr",
                bool(exp_dc["no_match_observed_means_definitely_no_pr"]),
                obs.no_match_observed_means_definitely_no_pr,
                "NO_MATCH_OBSERVED must never be marked as definitely no PR",
            )
            excluded_notes.append(
                f"{prefix}.direct_commit_confirmation_expectation.spec_api_field_name/spec_badge_text (HANDOFF §3)"
            )

        # EV-008 disjoint evidence_scope check
        exp_scope = exp_ev.get("evidence_scope")
        if isinstance(exp_scope, Mapping):
            act_scope = obs.anchor_scope or {}
            for skey in ("file_paths", "diff_hunks", "review_thread_ids", "content_unit_ids"):
                if skey in exp_scope:
                    _check(
                        f"{prefix}.evidence_scope.{skey}",
                        sorted(exp_scope[skey]),
                        sorted(act_scope.get(skey) or []),
                        f"Split evidence_scope.{skey} mismatch",
                    )

        # CAND-EV-021 dependent_artifacts_unchanged
        exp_dep = exp_ev.get("dependent_artifacts_unchanged")
        if isinstance(exp_dep, Mapping):
            _check(
                f"{prefix}.dependent_artifacts_unchanged.fact_ids",
                sorted(exp_dep.get("fact_ids") or []),
                sorted(obs.dependent_fact_ids),
                "Dependent Fact IDs changed unexpectedly",
            )
            _check(
                f"{prefix}.dependent_artifacts_unchanged.claim_ids",
                sorted(exp_dep.get("claim_ids") or []),
                sorted(obs.dependent_claim_ids),
                "Dependent Claim IDs changed unexpectedly",
            )

    # 2. Command results (outcome, zero mutation on rejection, confirmed spec_error_code, resulting_event_ids)
    exp_cmds: list[dict[str, Any]] = list(expected.get("command_results") or [])
    rec_by_cmd_id = {r.command_id: r for r in command_records}
    _check("command_results.count", len(exp_cmds), len(command_records), "Command execution count mismatch")
    for exp_cmd in exp_cmds:
        cid = str(exp_cmd["command_id"])
        rec = rec_by_cmd_id.get(cid)
        prefix = f"command_results[{cid}]"
        if rec is None:
            checks += 1
            diffs.append(
                OracleDiffEntry(
                    path=prefix,
                    expected=exp_cmd["outcome"],
                    actual="<NOT_EXECUTED>",
                    message="Expected command was not executed",
                )
            )
            continue

        _check(f"{prefix}.outcome", exp_cmd["outcome"], rec.outcome, "Command outcome mismatch")
        _check(
            f"{prefix}.state_mutated",
            bool(exp_cmd["state_mutated"]),
            rec.state_mutated,
            "state_mutated mismatch (rejected commands MUST have 0 DB state mutations)",
        )
        if exp_cmd["outcome"] == "REJECTED":
            _check(
                f"{prefix}.zero_mutation_state_hash",
                rec.pre_state_sha256,
                rec.post_state_sha256,
                "DB logical state hash changed on REJECTED command",
            )

        mapped_result_ids = sorted(
            uuid_to_expected_id.get(uid, uid) for uid in rec.resulting_event_ids
        )
        _check(
            f"{prefix}.resulting_event_ids",
            sorted(exp_cmd.get("resulting_event_ids") or []),
            mapped_result_ids,
            "resulting_event_ids mismatch",
        )

        # Confirmed spec_error_code (skip UNCONFIRMED per HANDOFF §3)
        exp_err_code = exp_cmd.get("spec_error_code")
        if exp_err_code is not None and exp_err_code != "UNCONFIRMED":
            _check(
                f"{prefix}.spec_error_code",
                exp_err_code,
                rec.error_code,
                "Confirmed domain spec_error_code mismatch",
            )
        elif exp_err_code == "UNCONFIRMED":
            cand_codes = exp_cmd.get("candidate_error_codes")
            if isinstance(cand_codes, list) and cand_codes:
                checks += 1
                if rec.error_code not in cand_codes:
                    diffs.append(
                        OracleDiffEntry(
                            path=f"{prefix}.candidate_error_codes",
                            expected=cand_codes,
                            actual=rec.error_code,
                            message="Error code outside allowed candidate_error_codes",
                        )
                    )
            excluded_notes.append(f"{prefix}.spec_error_code/http_status (HANDOFF §3)")

        # EV-016 internal_policy_code verification (CROSS_WORKSPACE_MEMBER)
        if "internal_policy_code" in exp_cmd and exp_cmd["internal_policy_code"] is not None:
            _check(
                f"{prefix}.internal_policy_code",
                exp_cmd["internal_policy_code"],
                rec.internal_policy_code,
                "Domain internal_policy_code mismatch",
            )

    # 3. Domain ReviewActions (CAND-EV-022 CONFIRM, CAND-EV-023 EDIT; external response token excluded)
    exp_rev_actions: list[dict[str, Any]] = list(expected.get("review_actions") or [])
    if exp_rev_actions:
        _check(
            "review_actions.count",
            len(exp_rev_actions),
            len(observed.review_actions),
            "Domain ReviewAction count mismatch",
        )
        for idx, exp_ra in enumerate(exp_rev_actions):
            prefix = f"review_actions[{idx}]"
            if idx >= len(observed.review_actions):
                break
            obs_ra = observed.review_actions[idx]
            _check(
                f"{prefix}.domain_review_action",
                exp_ra["domain_review_action"],
                obs_ra.action,
                "Domain ReviewAction mismatch",
            )
            _check(
                f"{prefix}.event_id",
                exp_ra["event_id"],
                uuid_to_expected_id.get(obs_ra.event_id, obs_ra.event_id),
                "ReviewAction target event_id mismatch",
            )
            _check(
                f"{prefix}.from_version",
                int(exp_ra["from_version"]),
                obs_ra.from_version,
                "ReviewAction from_version mismatch",
            )
            _check(
                f"{prefix}.to_version",
                int(exp_ra["to_version"]),
                obs_ra.to_version,
                "ReviewAction to_version mismatch",
            )
            _check(
                f"{prefix}.creates_new_event_version",
                bool(exp_ra["creates_new_event_version"]),
                obs_ra.creates_new_event_version,
                "ReviewAction creates_new_event_version mismatch",
            )
            _check(
                f"{prefix}.is_event_merge",
                bool(exp_ra["is_event_merge"]),
                obs_ra.is_event_merge,
                "ReviewAction is_event_merge mismatch",
            )
            if "changed_fields" in exp_ra:
                _check(
                    f"{prefix}.changed_fields",
                    sorted(exp_ra["changed_fields"]),
                    sorted(obs_ra.changed_fields),
                    "ReviewAction changed_fields mismatch",
                )
            excluded_notes.append(f"{prefix}.external_response_action_token (HANDOFF §3)")

    # 4. Downstream side effects (EV-012 STALE Facts/Claims + Export outbox boundary; EV-011 source preservation)
    exp_downstream: list[dict[str, Any]] = list(expected.get("downstream_side_effects") or [])
    obs_down_by_key = {
        (d.artifact_type, d.artifact_id): d for d in observed.downstream_artifacts
    }
    for exp_d in exp_downstream:
        key = (str(exp_d["artifact_type"]), str(exp_d["artifact_id"]))
        prefix = f"downstream_side_effects[{key[0]}:{key[1]}]"
        obs_d = obs_down_by_key.get(key)
        if obs_d is None:
            checks += 1
            diffs.append(
                OracleDiffEntry(
                    path=prefix,
                    expected=exp_d["expected_status"],
                    actual="<MISSING>",
                    message="Expected downstream artifact side effect not observed",
                )
            )
            continue
        _check(f"{prefix}.status", exp_d["expected_status"], obs_d.status, "Downstream status mismatch")
        _check(
            f"{prefix}.retroactively_deleted",
            bool(exp_d["retroactively_deleted"]),
            not obs_d.still_exists,
            "Downstream artifact was retroactively deleted",
        )
        _check(
            f"{prefix}.auto_transferred_to_members",
            bool(exp_d["auto_transferred_to_members"]),
            obs_d.transferred_to_members,
            "Downstream artifact was auto-transferred to members",
        )

    # EV-011 & general source provenance preservation invariant: 0 source rows deleted!
    for tbl, before_cnt in observed.source_provenance_counts_before.items():
        after_cnt = observed.source_provenance_counts_after.get(tbl, -1)
        _check(
            f"source_provenance_preserved.{tbl}",
            before_cnt,
            after_cnt,
            f"Source table {tbl} row count changed during curation (source provenance must be preserved)",
        )

    # 5. Held items (EV-003, EV-004, EV-013, CAND-EV-019, CAND-EV-020)
    exp_held: list[dict[str, Any]] = list(expected.get("held_items") or [])
    obs_held_by_src = {h.source_id: h for h in observed.held_items}
    _check("held_items.count", len(exp_held), len(observed.held_items), "Held items count mismatch")
    for exp_h in exp_held:
        src_id = str(exp_h["source_id"])
        obs_h = obs_held_by_src.get(src_id)
        prefix = f"held_items[{src_id}]"
        if obs_h is None:
            checks += 1
            diffs.append(
                OracleDiffEntry(
                    path=prefix,
                    expected=exp_h["record_kind"],
                    actual="<MISSING>",
                    message="Expected held/non-eligible source item not observed",
                )
            )
            continue
        _check(f"{prefix}.record_kind", exp_h["record_kind"], obs_h.record_kind, "Held record_kind mismatch")
        _check(
            f"{prefix}.interpreted_as_no_pr",
            bool(exp_h["interpreted_as_no_pr"]),
            obs_h.interpreted_as_no_pr,
            "UNKNOWN/held item was wrongly interpreted as NO_MATCH_OBSERVED",
        )
        excluded_notes.append(f"{prefix}.spec_hold_status (HANDOFF §3)")

    # 6. Out-of-window PR dedup checks (CAND-EV-019; diagnostic_persistence_format excluded per HANDOFF §3)
    exp_oow: list[dict[str, Any]] = list(expected.get("out_of_window_pr_dedup_checks") or [])
    obs_oow_by_cmt = {o.commit_id: o for o in observed.out_of_window_dedups}
    for exp_o in exp_oow:
        cmt_id = str(exp_o["commit_id"])
        obs_o = obs_oow_by_cmt.get(cmt_id)
        prefix = f"out_of_window_pr_dedup_checks[{cmt_id}]"
        if obs_o is None:
            checks += 1
            diffs.append(
                OracleDiffEntry(
                    path=prefix,
                    expected="deduplicated",
                    actual="<MISSING>",
                    message="Expected out-of-window PR deduplication check missing",
                )
            )
            continue
        _check(
            f"{prefix}.creates_new_pr_event",
            bool(exp_o["creates_new_pr_event"]),
            obs_o.creates_new_pr_event,
            "Out-of-window PR wrongly created a new PR Event",
        )
        _check(
            f"{prefix}.creates_direct_commit_event",
            bool(exp_o["creates_direct_commit_event"]),
            obs_o.creates_direct_commit_event,
            "Out-of-window PR commit wrongly created a DIRECT_COMMIT Event",
        )
        excluded_notes.append(f"{prefix}.diagnostic_persistence_format (HANDOFF §3)")

    # 7. Search checks (contract-level collapse & projection via query_searchable_top_level_work_events)
    exp_searches: list[dict[str, Any]] = list(expected.get("search_checks") or [])
    _check(
        "search_checks.count",
        len(exp_searches),
        len(observed.search_checks),
        "search_checks count mismatch",
    )
    for idx, exp_sc in enumerate(exp_searches):
        if idx >= len(observed.search_checks):
            break
        obs_sc = observed.search_checks[idx]
        prefix = f"search_checks[{idx}:{exp_sc['query']}]"
        mapped_hits = sorted(
            uuid_to_expected_id.get(uid, uid) for uid in obs_sc.matched_event_uuids
        )
        _check(
            f"{prefix}.expected_result_count",
            int(exp_sc["expected_result_count"]),
            obs_sc.result_count,
            "Collapsed search result count mismatch",
        )
        _check(
            f"{prefix}.expected_event_ids",
            sorted(exp_sc.get("expected_event_ids") or []),
            mapped_hits,
            "Collapsed search matched event IDs mismatch",
        )
        _check(
            f"{prefix}.duplicate_count",
            0,
            obs_sc.duplicate_count,
            "Duplicate logical event returned in search collapse",
        )

    # 8. Aggregation checks & anti-double-counting invariants
    exp_agg = expected.get("aggregation_check") or {}
    if "total_countable_events" in exp_agg:
        _check(
            "aggregation_check.total_countable_events",
            int(exp_agg["total_countable_events"]),
            observed.total_countable_events,
            "total_countable_events mismatch",
        )
    if "total_user_composite_events" in exp_agg:
        _check(
            "aggregation_check.total_user_composite_events",
            int(exp_agg["total_user_composite_events"]),
            observed.total_user_composite_events,
            "total_user_composite_events mismatch",
        )
    if "total_held_items" in exp_agg:
        _check(
            "aggregation_check.total_held_items",
            int(exp_agg["total_held_items"]),
            observed.total_held_items,
            "total_held_items mismatch",
        )
    if "total_rejected_events" in exp_agg:
        _check(
            "aggregation_check.total_rejected_events",
            int(exp_agg["total_rejected_events"]),
            observed.total_rejected_events,
            "total_rejected_events mismatch",
        )
    if "total_direct_commit_events" in exp_agg:
        _check(
            "aggregation_check.total_direct_commit_events",
            int(exp_agg["total_direct_commit_events"]),
            observed.total_direct_commit_events,
            "total_direct_commit_events mismatch",
        )
    if "promoted_events_from_partial_runs" in exp_agg:
        _check(
            "aggregation_check.promoted_events_from_partial_runs",
            int(exp_agg["promoted_events_from_partial_runs"]),
            observed.promoted_events_from_partial_runs,
            "PARTIAL sync run must promote 0 Events",
        )
    if isinstance(exp_agg.get("per_workspace_countable_events"), Mapping):
        _check(
            "aggregation_check.per_workspace_countable_events",
            dict(exp_agg["per_workspace_countable_events"]),
            dict(observed.per_workspace_api_counts),
            "Per-workspace API-visible countable event isolation mismatch",
        )
    _check(
        "aggregation_check.top_level_duplicate_count",
        0,
        observed.top_level_duplicate_count,
        "Duplicate top-level events detected across list/search",
    )

    return CaseOracleComparison(
        case_id=case_id,
        matched=(len(diffs) == 0),
        diffs=tuple(diffs),
        assertions_checked=checks,
        excluded_unconfirmed_checks=tuple(dict.fromkeys(excluded_notes)),
    )


__all__ = ["compare_case_against_frozen_oracle"]
