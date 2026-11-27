# T-001B Human Review Queue (from T-001A Draft)

- **Approval Status**: `DRAFT` (`approved_by: null`, `human_approval_required: true`).
- **Rule Discipline**: Official rules from the reviewer spec correction brief (`CHANGES REQUESTED` on `817eb23`) are applied across all 18 cases and are **not** re-opened here. Only genuinely unconfirmed wire details and open contracts remain queued for **T-001B** human review and freeze.

## 1. Confirmed Official Rules Already Applied (Do Not Re-Decide)

1. **Anchor modes**: `PR`, `PR_SEGMENT`, `DIRECT_COMMIT`, `USER_COMPOSITE` (`PR` and `DIRECT_COMMIT` auto-generated; verified default-branch `NO_MATCH_OBSERVED` = 1 candidate per commit; multi-commit auto-clustering out of MVP).
2. **Import runs**: `UNKNOWN` != no PR; incomplete PR association sets Import Run to `PARTIAL` (`PARTIAL_SUCCESS` removed) and promotes `0` Events; default Event search returns `0`.
3. **Lifecycle statuses**: `CANDIDATE`, `CONFIRMED`, `REJECTED`, `SUPERSEDED`, `STALE`, strictly separated from top-level visibility (`is_active_top_level`). Arbitrary `ACTIVE`/`EXCLUDED`/`DISSOLVED` removed.
4. **Split**: Mixed-purpose PR stays 1 `CANDIDATE` with a review flag (`auto_split_count=0`); explicit user split requires disjoint `evidence_scope`, outputs `PR_SEGMENT` (or `DIRECT_COMMIT`), and marks parent `SUPERSEDED`.
5. **Merge**: Same workspace & same repository across `PR`, `PR_SEGMENT`, `DIRECT_COMMIT` (PR + direct commit allowed); checks per-member Event versions; preserves immutable snapshots; marks members `SUPERSEDED`; rolls up member-only search hits once to parent `USER_COMPOSITE`; no auto-inheritance of member Fact/Claim.
6. **Dissolve**: Requires `expected_version`, `reason`, `idempotency_key`; sets Composite `SUPERSEDED` + `composite_membership_status='REMOVED'`; re-evaluates members (`CANDIDATE`/`CONFIRMED`/`STALE`); marks Composite `FACT`, `CLAIM`, and `EXPORT` as `STALE` without deletion or auto-transfer.
7. **Official Event API error codes**: `EVENT_VERSION_CONFLICT`, `CROSS_WORKSPACE_MEMBER`, `CROSS_REPOSITORY_MEMBER`, `DUPLICATE_COMPOSITE_MEMBER`, `NESTED_COMPOSITE_NOT_ALLOWED`, `CANNOT_LINK_CONFLICT`, `MEMBER_NOT_ACTIVE`, `MEMBER_ALREADY_COMPOSED`, `PR_ASSOCIATION_INCOMPLETE`. `CANNOT_LINK` is a relation semantic, not a catch-all wire code.

## 2. Remaining Unconfirmed Wire Tokens (`UNCONFIRMED`)

| Token Area | Affected Cases | Current Placeholder | Action Needed in T-001B |
| --- | --- | --- | --- |
| Command wire names (`spec_command_name`) | `EV-008`, `EV-009`, `EV-011`..`EV-016` | `"UNCONFIRMED"` | Fill exact HTTP endpoint / command identifiers from MVP API spec. |
| Mixed-purpose review flag string & UI | `EV-007` | `exact_flag_token: "UNCONFIRMED"` | Confirm exact flag token if API exposes a string literal. |
| Diagnostic observation / non-eligible record status | `EV-003`, `EV-004`, `EV-013` | `spec_hold_status: "UNCONFIRMED"` | Fill exact status token only if diagnostic records are part of API contract. |
| HTTP status codes & `If-Match` transport | `EV-013`..`EV-016` | `http_status` in `unconfirmed_fields` | Confirm `409` vs `412` / `If-Match` for `EVENT_VERSION_CONFLICT`, HTTP statuses for `NESTED_COMPOSITE_NOT_ALLOWED`, `MEMBER_NOT_ACTIVE`, `CROSS_REPOSITORY_MEMBER`, and unauthorized `403` vs `404`. |

## 3. Case-by-Case T-001B Review Queue (18 Cases)

| Review ID | Case | Scope | Official Refs | Priority | Open Items Requiring Human Decision / Sign-Off |
| --- | --- | --- | --- | --- | --- |
| `RQ-001` | [EV-001](cases/EV-001-pr-centered-basic.json) | `MVP` | `FR-006`, `EVENT-10` | `P1` | Confirm whether merge-commit SHAs on default branch belong in `source_commit_ids` alongside PR branch commits. |
| `RQ-002` | [EV-002](cases/EV-002-default-branch-direct-commit.json) | `MVP` | `FR-006`, `EVENT-10` | `P2` | Independent human approval prior to T-001B golden freeze (no open spec ambiguity). |
| `RQ-003` | [EV-003](cases/EV-003-unknown-association-hold.json) | `MVP` | `FR-006`, `EVENT-10` | `P1` | Confirm diagnostic `spec_hold_status` token if exposed (0 Events promoted & 0 default Event search results are fixed). |
| `RQ-004` | [EV-004](cases/EV-004-non-default-branch-no-pr.json) | `MVP` | `FR-006`, `EVENT-10` | `P2` | Confirm diagnostic record token if persisted (0 direct-commit candidates is fixed; physical collection stage separated). |
| `RQ-005` | [EV-005](cases/EV-005-issue-ticket-context-only.json) | `MVP` | `FR-006` | `P2` | Confirm ticket-only keyword search indexing/ranking expectations separately from `CONTEXT_ONLY` membership. |
| `RQ-006` | [EV-006](cases/EV-006-shared-ticket-no-auto-merge.json) | `MVP` | `FR-006` | `P2` | Confirm whether non-binding UI hints or ticket-only keyword ranking exist (no auto-merge is fixed). |
| `RQ-007` | [EV-007](cases/EV-007-mixed-purpose-pr.json) | `MVP` | `FR-006`, `FR-007` | `P1` | Fill exact review flag string token if exposed (1 `CANDIDATE`, mixed-purpose hint present, 0 auto-split are fixed). |
| `RQ-008` | [EV-008](cases/EV-008-user-split-mixed-pr.json) | `MVP` | `FR-007` | `P1` | Fill split `spec_command_name` and confirm whether a separate undo-split command exists in MVP. |
| `RQ-009` | [EV-009](cases/EV-009-user-manual-merge-composite.json) | `MVP` | `FR-007`, `EVENT-11` | `P1` | Fill manual-merge `spec_command_name` and approve `USER_COMPOSITE` + `SUPERSEDED` member proposal. |
| `RQ-010` | [EV-010](cases/EV-010-composite-no-double-counting.json) | `MVP` | `FR-007`, `EVENT-11` | `P1` | Confirm direct detail GET response shape for historical `SUPERSEDED` member Event IDs (member search roll-up is fixed). |
| `RQ-011` | [EV-011](cases/EV-011-user-exclude-event.json) | `MVP` | `FR-007` | `P1` | Fill exclude `spec_command_name`; confirm whether restore command or Composite-exclude cascade exists. |
| `RQ-012` | [EV-012](cases/EV-012-user-dissolve-composite.json) | `MVP` | `FR-007`, `EVENT-12` | `P0` | Fill dissolve `spec_command_name`; confirm error code when dissolve targets a non-`USER_COMPOSITE` Event. |
| `RQ-013` | [EV-013](cases/EV-013-cannot-link-context-and-unknown.json) | `MVP` | `FR-007`, `EVENT-12` | `P0` | Confirm whether non-Event IDs (ticket / unpromoted `UNKNOWN` commit) fail at validation/404 vs domain error (`CANNOT_LINK_CONFLICT` / `PR_ASSOCIATION_INCOMPLETE`). |
| `RQ-014` | [EV-014](cases/EV-014-cannot-link-nested-and-excluded.json) | `MVP` | `FR-007`, `EVENT-12` | `P0` | Confirm multi-violation error precedence for `cmd_syn_014_nested` (`MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`) and HTTP statuses. |
| `RQ-015` | [EV-015](cases/EV-015-version-conflict-optimistic-lock.json) | `MVP` | `FR-007`, `EVENT-12` | `P0` | Confirm HTTP status (`409` vs `412`) and whether `If-Match` header is used alongside request-body `expected_version`. |
| `RQ-016` | [EV-016](cases/EV-016-workspace-boundary-conflict.json) | `MVP` | `FR-007`, `EVENT-12` | `P0` | Confirm final external HTTP error/status (`403` vs `404` vs `CROSS_WORKSPACE_MEMBER`) for unauthorized foreign-workspace merge and exclude commands. |
| `RQ-017` | [EV-017](cases/EV-017-post-mvp-b003-auto-change-discovery.json) | `POST_MVP` (`B-003`) | `FR-006` | `P2` | Confirm whether downstream `B-003` evaluation will run this fixture behind a `POST_MVP` harness flag. |
| `RQ-018` | [EV-018](cases/EV-018-post-mvp-b003-late-association-recovery.json) | `POST_MVP` (`B-003`) | `FR-006` | `P2` | Define post-MVP `B-003` transition mechanics for superseding a `DIRECT_COMMIT` Event upon late PR association observation. |
