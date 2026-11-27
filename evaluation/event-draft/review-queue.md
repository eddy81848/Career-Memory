# T-001B Review Status & Remaining Open Items (`1.0.0-mvp-approved`)

- **Dataset Version**: `1.0.0-mvp-approved` (`task_ref: T-001B`, `approval_status: PARTIALLY_APPROVED_MVP_REGRESSION`).
- **Approval Baseline Commit**: `0773ac5740b358f4168777fda1e578231cc6dff9` on `qa/event-fixtures` (base `main`: `efe251071fbee1be9a2ffee680a73f3ca3cd8084`).
- **Approved Scope**:
  - **`EV-001`..`EV-016` (`16` MVP cases)**: Confirmed logical expectations **APPROVED** as MVP synthetic regression ground truth (`APPROVED_MVP_REGRESSION`).
  - **`EV-017`..`EV-018` (`2` Post-MVP `B-003` cases)**: Retained as **unapproved `DRAFT`** (`POST_MVP_DRAFT_EXCLUDED`).
  - **`NEG-001`..`NEG-006` (`6` negative variants)**: Strictly **isolated** under `negative-fixtures/` (`eval_excluded: true`), never part of the evaluation dataset.
- **Truth Comparison Policy**:
  - **Enforced**: Confirmed logical expectations of `EV-001`..`EV-016`, unambiguous official Event API error codes (`NESTED_COMPOSITE_NOT_ALLOWED`, `MEMBER_NOT_ACTIVE`, `EVENT_VERSION_CONFLICT`, `CROSS_REPOSITORY_MEMBER`), and **zero state mutation on every rejected command** (`outcome="REJECTED"`, `state_mutated=false`, `resulting_event_ids=[]`, unmutated Event state).
  - **Excluded from truth comparison**: Unconfirmed HTTP status codes (`409` vs `412`, `If-Match`, unauthorized `403` vs `404`), unconfirmed multi-violation error precedence (`EV-014` `cmd_syn_014_nested`: `MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`), unconfirmed wire error codes for non-Event IDs / unauthorized cross-workspace responses (`EV-013`, `EV-016`), and unconfirmed wire command/flag/diagnostic tokens.

## 1. Confirmed Official Rules Enforced in Approved MVP Ground Truth

1. **Anchor modes**: `PR`, `PR_SEGMENT`, `DIRECT_COMMIT`, `USER_COMPOSITE` (`PR` and `DIRECT_COMMIT` auto-generated; verified default-branch `NO_MATCH_OBSERVED` = 1 candidate per commit; multi-commit auto-clustering out of MVP).
2. **Import runs**: `UNKNOWN` != no PR; incomplete PR association sets Import Run to `PARTIAL` (`PARTIAL_SUCCESS` removed) and promotes `0` Events; default Event search returns `0`.
3. **Lifecycle statuses**: `CANDIDATE`, `CONFIRMED`, `REJECTED`, `SUPERSEDED`, `STALE`, strictly separated from top-level visibility (`is_active_top_level`). Arbitrary `ACTIVE`/`EXCLUDED`/`DISSOLVED` removed.
4. **Split**: Mixed-purpose PR stays 1 `CANDIDATE` with a review flag (`auto_split_count=0`); explicit user split requires disjoint `evidence_scope`, outputs `PR_SEGMENT` (or `DIRECT_COMMIT`), and marks parent `SUPERSEDED`.
5. **Merge**: Same workspace & same repository across `PR`, `PR_SEGMENT`, `DIRECT_COMMIT` (PR + direct commit allowed); checks per-member Event versions; preserves immutable snapshots; marks members `SUPERSEDED`; rolls up member-only search hits once to parent `USER_COMPOSITE`; no auto-inheritance of member Fact/Claim.
6. **Dissolve**: Requires `expected_version`, `reason`, `idempotency_key`; sets Composite `SUPERSEDED` + `composite_membership_status='REMOVED'`; re-evaluates members (`CANDIDATE`/`CONFIRMED`/`STALE`); marks Composite `FACT`, `CLAIM`, and `EXPORT` as `STALE` without deletion or auto-transfer.
7. **Official Event API error codes**: `EVENT_VERSION_CONFLICT`, `CROSS_WORKSPACE_MEMBER`, `CROSS_REPOSITORY_MEMBER`, `DUPLICATE_COMPOSITE_MEMBER`, `NESTED_COMPOSITE_NOT_ALLOWED`, `CANNOT_LINK_CONFLICT`, `MEMBER_NOT_ACTIVE`, `MEMBER_ALREADY_COMPOSED`, `PR_ASSOCIATION_INCOMPLETE`. `CANNOT_LINK` is a relation semantic, not a catch-all wire code.

## 2. Unconfirmed Wire / Precedence Items Excluded from MVP Truth Comparison

| Area | Affected Cases | Comparison Policy in `1.0.0-mvp-approved` |
| --- | --- | --- |
| HTTP status codes & `If-Match` transport | `EV-013`..`EV-016` | **Excluded** from truth comparison (`409` vs `412`, `If-Match`, `403` vs `404`). Rejection + `state_mutated=false` **retained**. |
| Multi-violation error precedence | `EV-014` (`cmd_syn_014_nested`) | **Excluded** (`MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE` unconfirmed). Rejection + `state_mutated=false` **retained**. |
| Non-Event ID / unauthorized cross-workspace external wire codes | `EV-013`, `EV-016` | **Excluded** (validation/404 vs domain error; external HTTP exposure). Rejection + `state_mutated=false` **retained**. |
| Command wire names (`spec_command_name`) | `EV-008`, `EV-009`, `EV-011`..`EV-016` | **Excluded** (`"UNCONFIRMED"`). Command logical semantics **retained**. |
| Review flag string / diagnostic observation status token | `EV-003`, `EV-004`, `EV-007`, `EV-013` | **Excluded** (`exact_flag_token` / `spec_hold_status = "UNCONFIRMED"`). Logical verdict **retained**. |

## 3. Case-by-Case Approval & Open-Item Status (18 Cases)

| Review ID | Case | Scope / Status | Eval Bundle | Official Refs | Excluded from Truth Comparison / Open Future Items |
| --- | --- | --- | --- | --- | --- |
| `RQ-001` | [EV-001](cases/EV-001-pr-centered-basic.json) | `MVP` / **APPROVED** | `bundle_mvp_ledger_service` | `FR-006`, `EVENT-10` | Excluded: whether default-branch merge-commit SHAs belong in `source_commit_ids`. |
| `RQ-002` | [EV-002](cases/EV-002-default-branch-direct-commit.json) | `MVP` / **APPROVED** | `bundle_mvp_edge_gateway` | `FR-006`, `EVENT-10` | None (logical expectation approved). |
| `RQ-003` | [EV-003](cases/EV-003-unknown-association-hold.json) | `MVP` / **APPROVED** | `bundle_mvp_billing_engine` | `FR-006`, `EVENT-10` | Excluded: diagnostic `spec_hold_status` token & UI presentation (`PARTIAL` run, 0 Events, 0 search results enforced). |
| `RQ-004` | [EV-004](cases/EV-004-non-default-branch-no-pr.json) | `MVP` / **APPROVED** | `bundle_mvp_search_indexer` | `FR-006`, `EVENT-10` | Excluded: diagnostic `spec_hold_status` token & physical collection stage placement (0 direct-commit candidates enforced). |
| `RQ-005` | [EV-005](cases/EV-005-issue-ticket-context-only.json) | `MVP` / **APPROVED** | `bundle_mvp_notification_hub` | `FR-006` | Excluded: ticket-only keyword search indexing/ranking (`CONTEXT_ONLY` membership enforced). |
| `RQ-006` | [EV-006](cases/EV-006-shared-ticket-no-auto-merge.json) | `MVP` / **APPROVED** | `bundle_mvp_catalog_api` | `FR-006` | Excluded: non-binding UI hints / ticket-only keyword ranking (no auto-merge enforced). |
| `RQ-007` | [EV-007](cases/EV-007-mixed-purpose-pr.json) | `MVP` / **APPROVED** | `bundle_mvp_reporting_core` | `FR-006`, `FR-007` | Excluded: exact review flag string token & UI presentation (1 `CANDIDATE`, hint present, 0 auto-split enforced). |
| `RQ-008` | [EV-008](cases/EV-008-user-split-mixed-pr.json) | `MVP` / **APPROVED** | `bundle_mvp_reporting_core` | `FR-007` | Excluded: split `spec_command_name` & undo-split command existence (disjoint `PR_SEGMENT` + `SUPERSEDED` parent enforced). |
| `RQ-009` | [EV-009](cases/EV-009-user-manual-merge-composite.json) | `MVP` / **APPROVED** | `bundle_mvp_catalog_api` | `FR-007`, `EVENT-11` | Excluded: manual-merge `spec_command_name` (`USER_COMPOSITE` + `SUPERSEDED` members + snapshots enforced). |
| `RQ-010` | [EV-010](cases/EV-010-composite-no-double-counting.json) | `MVP` / **APPROVED** | `bundle_mvp_queue_dispatcher` | `FR-007`, `EVENT-11` | Excluded: direct detail GET response shape for historical `SUPERSEDED` member IDs (single-count & roll-up enforced). |
| `RQ-011` | [EV-011](cases/EV-011-user-exclude-event.json) | `MVP` / **APPROVED** | `bundle_mvp_web_portal` | `FR-007` | Excluded: exclude `spec_command_name`, restore command, Composite-exclude cascade (`REJECTED` + provenance enforced). |
| `RQ-012` | [EV-012](cases/EV-012-user-dissolve-composite.json) | `MVP` / **APPROVED** | `bundle_mvp_catalog_api` | `FR-007`, `EVENT-12` | Excluded: dissolve `spec_command_name` & non-composite dissolve error code (`SUPERSEDED`+`REMOVED`, re-eval, `STALE` enforced). |
| `RQ-013` | [EV-013](cases/EV-013-cannot-link-context-and-unknown.json) | `MVP` / **APPROVED** | `bundle_mvp_auth_services` | `FR-007`, `EVENT-12` | Excluded: non-Event ID wire error code, HTTP status, `spec_hold_status`, `spec_command_name`. **Rejection + 0 mutation enforced.** |
| `RQ-014` | [EV-014](cases/EV-014-cannot-link-nested-and-excluded.json) | `MVP` / **APPROVED** | `bundle_mvp_analytics_pipe` | `FR-007`, `EVENT-12` | Excluded: HTTP statuses & `cmd_syn_014_nested` precedence (`MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`). **`NESTED_COMPOSITE_NOT_ALLOWED`, `MEMBER_NOT_ACTIVE`, and 0 mutation enforced.** |
| `RQ-015` | [EV-015](cases/EV-015-version-conflict-optimistic-lock.json) | `MVP` / **APPROVED** | `bundle_mvp_ledger_service` | `FR-007`, `EVENT-12` | Excluded: HTTP `409` vs `412`, `If-Match`, `spec_command_name`. **`EVENT_VERSION_CONFLICT` and 0 mutation enforced.** |
| `RQ-016` | [EV-016](cases/EV-016-workspace-boundary-conflict.json) | `MVP` / **APPROVED** | `bundle_mvp_multi_workspace` | `FR-007`, `EVENT-12` | Excluded: unauthorized foreign-workspace external HTTP/wire error (`403`/`404`) & `spec_command_name`. **`CROSS_REPOSITORY_MEMBER`, cross-workspace isolation, and 0 mutation enforced.** |
| `RQ-017` | [EV-017](cases/EV-017-post-mvp-b003-auto-change-discovery.json) | `POST_MVP` (`B-003`) / **DRAFT** | `bundle_post_mvp_b003_draft` | `FR-006` | **Unapproved Post-MVP draft** (excluded from MVP evaluation); confirm future `B-003` harness execution mode. |
| `RQ-018` | [EV-018](cases/EV-018-post-mvp-b003-late-association-recovery.json) | `POST_MVP` (`B-003`) / **DRAFT** | `bundle_post_mvp_b003_draft` | `FR-006` | **Unapproved Post-MVP draft** (excluded from MVP evaluation); define post-MVP `B-003` `DIRECT_COMMIT` superseding mechanics. |
