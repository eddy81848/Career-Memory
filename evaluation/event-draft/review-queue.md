# T-001B Human Review Queue (Event Dataset Draft)

> **Important**: All `expected_proposal` blocks in `cases/EV-001..EV-018` are **`DRAFT`** proposals authored by the EVENT-QA agent without live access to the Notion Event Contract, Annotation Guide, ADR-018, or Snapshot Import documents.  
> **Nothing in this dataset is approved or frozen.** Independent human reviewers in **T-001B** must inspect each case, resolve the `REVIEW_REQUIRED` questions below against the canonical Notion docs, and explicitly freeze ground truth.

## High-Priority Contract Alignment Questions (P0)

1. **Canonical Enum & Error Code Table (Notion Event Contract / ADR-018)**
   - Provisional Event kinds: `PR_CENTERED`, `DIRECT_COMMIT`, `COMPOSITE`, `SPLIT_CHILD` (`EV-001`, `EV-002`, `EV-008`, `EV-009`).
   - Provisional lifecycle / hold statuses: `ACTIVE`, `EXCLUDED`, `DISSOLVED`, `SUPERSEDED`, `CLASSIFICATION_HELD`, `NON_ELIGIBLE_BRANCH`, `DEFERRED_POST_MVP` (`EV-003`, `EV-004`, `EV-008`, `EV-011`, `EV-012`, `EV-017`).
   - Provisional error codes: `CANNOT_LINK`, `VERSION_CONFLICT`, `WORKSPACE_CONFLICT` (`EV-013..EV-016`).
2. **UNKNOWN Association Visibility (`EV-003`, `EV-013`, `EV-018`)**
   - Confirmed invariant: `UNKNOWN` is NEVER interpreted as `NO_PR` / `DIRECT_COMMIT`.
   - Open decision: Are held `UNKNOWN` items completely absent from default Event search (`expected_result_count=0`) and surfaced only in a dedicated pending/unclassified list?
3. **Mixed-Purpose PR Handling (`EV-007`, `EV-008`)**
   - Open decision: Does MVP emit any non-destructive `mixed_purpose_hint` on `EV-007`, and how does `SPLIT` partition a 1-commit squash-merged PR?
4. **Composite Membership & Search Roll-Up (`EV-009`, `EV-010`, `EV-012`, `EV-014`)**
   - Confirmed invariant: Composite members are NEVER double-counted in search or metrics.
   - Open decisions:
     - Do subsumed members keep `lifecycle_status="ACTIVE"` (filtered by `membership_state`) or transition to a dedicated status?
     - Does searching text that appears only inside one member PR roll up to the owning Composite Event (`EV-010`)?
     - Are nested Composites strictly forbidden via `CANNOT_LINK` (`EV-014`), or is appending to an existing Composite supported?
5. **OD-010 Cross-Workspace Error Exposure (`EV-016`)**
   - Open decision: When a command references an Event in another Workspace, should the API return `WORKSPACE_CONFLICT`, `CANNOT_LINK`, or an existence-hiding `403/404` per OD-010?
6. **Optimistic Concurrency Granularity (`EV-015`)**
   - Open decision: Is `expected_version` checked per target Event (`target_versions`), per Workspace, or via HTTP `If-Match` / ETag?
7. **Cross-Domain Boundary with CLAIM-QA (`EV-012`)**
   - Open decision: What happens to Claims referencing a Composite Event when the user runs `DISSOLVE`?

## Case-by-Case Review Queue

| Review ID | Case ID | Scope | Priority | Case File | Key `REVIEW_REQUIRED` Tokens | Reviewer Decision Focus |
| --- | --- | --- | --- | --- | --- | --- |
| `RQ-001` | `EV-001` | `MVP` | P1 | `cases/EV-001-pr-centered-basic.json` | `event_kind:PR_CENTERED`, `lifecycle_status:ACTIVE` | Confirm PR-centered enum & merge-commit inclusion |
| `RQ-002` | `EV-002` | `MVP` | P1 | `cases/EV-002-default-branch-direct-commit.json` | `event_kind:DIRECT_COMMIT` | Confirm direct-commit enum & 1:1 commit mapping |
| `RQ-003` | `EV-003` | `MVP` | P0 | `cases/EV-003-unknown-association-hold.json` | `hold_status:CLASSIFICATION_HELD` | Confirm exact hold status & default search exclusion |
| `RQ-004` | `EV-004` | `MVP` | P1 | `cases/EV-004-non-default-branch-no-pr.json` | `hold_status:NON_ELIGIBLE_BRANCH` | Confirm Snapshot Import filter vs Event Builder ignore |
| `RQ-005` | `EV-005` | `MVP` | P1 | `cases/EV-005-issue-ticket-context-only.json` | `relation_role:CONTEXT_ONLY` | Confirm ticket-key search matching & unreferenced tickets |
| `RQ-006` | `EV-006` | `MVP` | P0 | `cases/EV-006-shared-ticket-no-auto-merge.json` | `merge_origin:NONE` | Confirm zero auto-merge hints/actions on shared ticket |
| `RQ-007` | `EV-007` | `MVP` | P0 | `cases/EV-007-mixed-purpose-pr.json` | `mixed_purpose_flag:REVIEW_REQUIRED` | Confirm no auto-split & whether advisory flag exists |
| `RQ-008` | `EV-008` | `MVP` | P0 | `cases/EV-008-user-split-mixed-pr.json` | `lifecycle_status:SUPERSEDED`, `event_kind:SPLIT_CHILD` | Confirm parent terminal status & squash-PR split rule |
| `RQ-009` | `EV-009` | `MVP` | P0 | `cases/EV-009-user-manual-merge-composite.json` | `event_kind:COMPOSITE`, `merge_origin:USER_MANUAL_MERGE` | Confirm member status & PR+direct-commit merge eligibility |
| `RQ-010` | `EV-010` | `MVP` | P0 | `cases/EV-010-composite-no-double-counting.json` | `composite_search_roll_up:REVIEW_REQUIRED` | Confirm member-text roll-up to Composite & direct-ID lookup |
| `RQ-011` | `EV-011` | `MVP` | P1 | `cases/EV-011-user-exclude-event.json` | `lifecycle_status:EXCLUDED`, `command_type:EXCLUDE` | Confirm undo/restore scope & Composite exclude cascade |
| `RQ-012` | `EV-012` | `MVP` | P0 | `cases/EV-012-user-dissolve-composite.json` | `lifecycle_status:DISSOLVED`, `command_type:DISSOLVE` | Confirm dissolved status & Claim detachment (with CLAIM-QA) |
| `RQ-013` | `EV-013` | `MVP` | P0 | `cases/EV-013-cannot-link-context-and-unknown.json` | `error_code:CANNOT_LINK` | Confirm `CANNOT_LINK` vs validation error on non-Event IDs |
| `RQ-014` | `EV-014` | `MVP` | P0 | `cases/EV-014-cannot-link-nested-and-excluded.json` | `error_code:CANNOT_LINK` | Confirm flat Composite rule & excluded-target rejection |
| `RQ-015` | `EV-015` | `MVP` | P0 | `cases/EV-015-version-conflict-optimistic-lock.json` | `error_code:VERSION_CONFLICT` | Confirm exact error code, HTTP status & lock scope |
| `RQ-016` | `EV-016` | `MVP` | P0 | `cases/EV-016-workspace-boundary-conflict.json` | `error_code:WORKSPACE_CONFLICT`, `od_010_resource_hiding` | Align cross-workspace rejection with OD-010 hiding policy |
| `RQ-017` | `EV-017` | `POST_MVP` | P2 | `cases/EV-017-post-mvp-b003-auto-change-discovery.json` | `hold_status:DEFERRED_POST_MVP`, `backlog_ref:B-003` | Confirm POST_MVP assertion style for M3/B-003 runner |
| `RQ-018` | `EV-018` | `POST_MVP` | P2 | `cases/EV-018-post-mvp-b003-late-association-recovery.json` | `backlog_ref:B-003` | Confirm held-item lock policy & late `NONE` variant need |
