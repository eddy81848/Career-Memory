# T-001A Event Synthetic Dataset Coverage Matrix (DRAFT)

- **Approval Status**: `DRAFT` (`approved_by: null` — agent-authored, awaiting human review & golden freeze in **T-001B**).
- **Case Count**: 18 cases (`16 MVP` + `2 POST_MVP / B-003`) + 6 isolated negative variants (`NEG-001`..`NEG-006`).
- **Evaluation Boundary**: Fixture lint checks **format, referential integrity, governance, and official domain invariants** only. **EVAL-EVENT** (`M3 / T-306A`) is downstream runtime evaluation and is **NOT** passed by fixture lint.

## 1. Official Traceability vs. Draft Local IDs

| Official Ref | Scope / Meaning | Mapped Cases | Status Note |
| --- | --- | --- | --- |
| `FR-006` | Source-based Event candidate generation & import boundary | `EV-001`..`EV-007`, `EV-017`, `EV-018` | Draft fixtures (`DRAFT`) |
| `FR-007` | Event review, curation commands, conflict & prohibition rules | `EV-007`..`EV-016` | Draft fixtures (`DRAFT`) |
| `EVENT-10` | Direct commit eligibility & PR association lookup boundary | `EV-001`, `EV-002`, `EV-003`, `EV-004` | Draft fixtures (`DRAFT`) |
| `EVENT-11` | User manual merge & single-count `USER_COMPOSITE` aggregation / roll-up | `EV-009`, `EV-010` | Draft fixtures (`DRAFT`) |
| `EVENT-12` | Dissolve, prohibited links/nesting, version/workspace/repo conflicts & zero mutation | `EV-012`, `EV-013`, `EV-014`, `EV-015`, `EV-016` | Draft fixtures (`DRAFT`) |
| `EVAL-EVENT` | M3 downstream Event Builder runtime quality evaluation | *(M3 / T-306A)* | **Deferred — NOT passed by fixture lint** |

> Draft local IDs (`REQ-EV-*`, `T-001A-*`) are preserved in `draft_requirement_ids` / `draft_test_ids` strictly separated from official refs (`official_requirement_refs`, `official_test_refs`).

## 2. Official Spec Rules Applied vs. Remaining Open Items

- **Confirmed official rules applied (not re-decided)**:
  - **Anchor modes**: `PR`, `PR_SEGMENT`, `DIRECT_COMMIT`, `USER_COMPOSITE` (only `PR` and `DIRECT_COMMIT` auto-generated; 1 `DIRECT_COMMIT` per verified default-branch `NO_MATCH_OBSERVED` commit; multi-commit clustering out of MVP).
  - **Import runs**: `PARTIAL` (never `PARTIAL_SUCCESS`) when PR association lookup is incomplete; promotes `0` Events; `UNKNOWN` is never `NO_MATCH_OBSERVED`; default Event search returns `0`.
  - **Lifecycle statuses**: `CANDIDATE`, `CONFIRMED`, `REJECTED`, `SUPERSEDED`, `STALE`; top-level active/search/metrics visibility (`is_active_top_level`) is a separate boolean (no arbitrary `ACTIVE`/`EXCLUDED`/`DISSOLVED`).
  - **Split**: Mixed-purpose PR stays 1 `CANDIDATE` with review flag (`auto_split_count=0`); user split requires disjoint `evidence_scope` (`file_paths`, `diff_hunks`, `review_thread_ids`, `content_unit_ids`), produces `PR_SEGMENT` (or `DIRECT_COMMIT`), and marks parent `SUPERSEDED`.
  - **Merge**: Same workspace & same repository across eligible `PR`, `PR_SEGMENT`, `DIRECT_COMMIT` (PR + direct commit allowed); checks each member's Event version; preserves snapshots; marks members `SUPERSEDED`; rolls up member-only search hits once to parent `USER_COMPOSITE`; no auto-inheritance of member Fact/Claim.
  - **Dissolve**: Requires `expected_version`, `reason`, `idempotency_key`; sets Composite `SUPERSEDED` + `composite_membership_status='REMOVED'`; re-evaluates members to `CANDIDATE`/`CONFIRMED`/`STALE`; marks dependent `FACT`, `CLAIM`, and `EXPORT` as `STALE` (never retroactively deleted or auto-transferred).
  - **Event API error codes**: `EVENT_VERSION_CONFLICT`, `CROSS_WORKSPACE_MEMBER`, `CROSS_REPOSITORY_MEMBER`, `DUPLICATE_COMPOSITE_MEMBER`, `NESTED_COMPOSITE_NOT_ALLOWED`, `CANNOT_LINK_CONFLICT`, `MEMBER_NOT_ACTIVE`, `MEMBER_ALREADY_COMPOSED`, `PR_ASSOCIATION_INCOMPLETE`. `CANNOT_LINK` is a relation semantic, not a catch-all wire error code.
- **Still open (`UNCONFIRMED` / `REVIEW_REQUIRED`)**:
  - Command wire names (`spec_command_name`), exact review flag string/UI (`EV-007`), diagnostic record status tokens (`EV-003`/`EV-004`/`EV-013`), HTTP status codes (`409` vs `412`, `If-Match`, unauthorized `403` vs `404`), multi-violation error precedence (`EV-014`), detail GET shape for historical `SUPERSEDED` members (`EV-010`), undo-split / restore / non-composite dissolve errors, and post-MVP `B-003` harness transition mechanics (`EV-017`/`EV-018`).

## 3. Requirement & Test Coverage Matrix

| Draft Req ID | Draft Test ID(s) | Official Refs | Scope | Scenario Area | Input Case(s) | Negative Variant(s) | Observable Outcome (DRAFT) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `REQ-EV-PR-01` | `T-001A-01` | `FR-006`, `EVENT-10` | `MVP` | Merged PR default candidate | `EV-001` | `NEG-001` | 1 merged PR (2 commits) -> 1 `PR` `CANDIDATE`; 0 direct-commit candidates. |
| `REQ-EV-COMMIT-01` | `T-001A-02` | `FR-006`, `EVENT-10` | `MVP` | Default-branch direct commit | `EV-002` | — | Verified start/end default-branch commit (`COMPLETED` + `NO_MATCH_OBSERVED`) -> 1 `DIRECT_COMMIT` `CANDIDATE` (1 per commit). |
| `REQ-EV-ASSOC-01` | `T-001A-03`, `T-001A-13`, `T-001A-18` | `FR-006`, `FR-007`, `EVENT-10`, `EVENT-12` | `MVP` | `UNKNOWN` PR association & `PARTIAL` run | `EV-003`, `EV-013`, `EV-018` | `NEG-003` | `PARTIAL` run promotes 0 Events, default Event search = 0 (`EV-003`); non-mergeable (`EV-013`); separated from post-`SUCCESS` `B-003` (`EV-018`). |
| `REQ-EV-BRANCH-01` | `T-001A-04` | `FR-006`, `EVENT-10` | `MVP` | Non-default branch commit boundary | `EV-004` | — | Non-default branch commit -> 0 `DIRECT_COMMIT` candidates (physical collection stage separated from semantic verdict). |
| `REQ-EV-CONTEXT-01` | `T-001A-05`, `T-001A-13` | `FR-006`, `FR-007`, `EVENT-12` | `MVP` | Issue/ticket `CONTEXT_ONLY` | `EV-005`, `EV-013` | — | Ticket attaches as `CONTEXT_ONLY` on `PR` `CANDIDATE`; never standalone Event or merge member. |
| `REQ-EV-CONTEXT-02` | `T-001A-06` | `FR-006` | `MVP` | Shared ticket no auto-merge | `EV-006` | `NEG-005` | 2 merged PRs sharing `SYN-102` stay 2 standalone `PR` `CANDIDATE`s (`total_user_composite_events=0`). |
| `REQ-EV-MERGE-01` | `T-001A-06`, `T-001A-09` | `FR-006`, `FR-007`, `EVENT-11` | `MVP` | Auto merge vs user manual merge | `EV-006`, `EV-009` | `NEG-005` | No auto multi-PR merge (`EV-006`); explicit same-repo `manual_merge` creates 1 `USER_COMPOSITE` (`EV-009`). |
| `REQ-EV-MIXED-01` | `T-001A-07`, `T-001A-08` | `FR-006`, `FR-007` | `MVP` | Mixed-purpose PR & user split | `EV-007`, `EV-008` | — | 1 `PR` `CANDIDATE` with mixed-purpose review flag, 0 auto-split (`EV-007`); explicit split yields 2 `PR_SEGMENT`s (`EV-008`). |
| `REQ-EV-CMD-SPLIT-01` | `T-001A-08` | `FR-007` | `MVP` | User split (`PR_SEGMENT`, disjoint scope) | `EV-008` | `NEG-002` | Parent `PR` -> `SUPERSEDED` (v2); 2 `PR_SEGMENT` `CANDIDATE`s with disjoint concrete `evidence_scope` (`total_countable_events=2`). |
| `REQ-EV-CMD-MERGE-01` | `T-001A-09`, `T-001A-16` | `FR-007`, `EVENT-11`, `EVENT-12` | `MVP` | User manual merge (same-repo only) | `EV-009`, `EV-016` | `NEG-005` | Same-repo merge checks per-Event versions, preserves snapshots, marks members `SUPERSEDED`, yields 1 `USER_COMPOSITE` (`EV-009`); cross-repo fails with `CROSS_REPOSITORY_MEMBER` (`EV-016`). |
| `REQ-EV-COMP-01` | `T-001A-10`, `T-001A-12`, `T-001A-14` | `FR-007`, `EVENT-11`, `EVENT-12` | `MVP` | Composite single-count, roll-up & anti-nesting | `EV-010`, `EV-012`, `EV-014` | `NEG-004` | `SUPERSEDED` members never double-counted; member-only search hit rolls up once to `USER_COMPOSITE` (`EV-010`); nesting rejected (`EV-014`); dissolve removes membership (`EV-012`). |
| `REQ-EV-CMD-EXCLUDE-01` | `T-001A-11` | `FR-007` | `MVP` | User exclude (`REJECTED`) | `EV-011` | `NEG-002` | Target transitions to `REJECTED` (v2, `is_active_top_level=false`), Source preserved, sibling stays `CANDIDATE`. |
| `REQ-EV-CMD-DISSOLVE-01` | `T-001A-12` | `FR-007`, `EVENT-12` | `MVP` | User dissolve & `STALE` propagation | `EV-012` | — | Requires `expected_version`, `reason`, `idempotency_key`; Composite -> `SUPERSEDED` + `REMOVED`; members re-evaluated (`CONFIRMED`/`CANDIDATE`/`STALE`); dependent `FACT`, `CLAIM`, `EXPORT` -> `STALE` (not deleted/transferred). |
| `REQ-EV-ERR-LINK-01` | `T-001A-13` | `FR-007`, `EVENT-12` | `MVP` | Non-Event ticket / `UNKNOWN` commit reject | `EV-013` | — | Ticket and `PARTIAL`-run `UNKNOWN` commit rejected with 0 mutation (`ev_init_013_pr` from independent `SUCCESS` precondition run); wire error codes `UNCONFIRMED`. |
| `REQ-EV-ERR-LINK-02` | `T-001A-14` | `FR-007`, `EVENT-12` | `MVP` | Nesting & inactive member reject | `EV-014` | — | Composite root -> `NESTED_COMPOSITE_NOT_ALLOWED`; `REJECTED` member -> `MEMBER_NOT_ACTIVE`; already-composed `SUPERSEDED` member -> 0 mutation (precedence `UNCONFIRMED`). |
| `REQ-EV-ERR-VER-01` | `T-001A-15` | `FR-007`, `EVENT-12` | `MVP` | Per-Event version conflict | `EV-015` | — | Stale `expected_version=1` vs v2 -> `EVENT_VERSION_CONFLICT`, 0 mutation (HTTP `409`/`412` and `If-Match` `UNCONFIRMED`). |
| `REQ-EV-ERR-WS-01` | `T-001A-16` | `FR-007`, `EVENT-12` | `MVP` | Cross-repo & cross-workspace isolation | `EV-016` | — | Same-WS cross-repo -> `CROSS_REPOSITORY_MEMBER`; cross-WS merge separates `internal_policy_code` from external HTTP; cross-WS exclude omits merge-only error; per-WS API counts (`2`, `1`) separated from fixture total (`3`). |
| `REQ-EV-POST-01` | `T-001A-17` | `FR-006` | `POST_MVP` (`B-003`) | Post-success auto change discovery | `EV-017` | `NEG-006` | Post-`SUCCESS` PR #1702 stays in `unpersisted_external_observations` (`persisted_in_mvp=false`, `total_held_items=0`). |
| `REQ-EV-POST-02` | `T-001A-18` | `FR-006` | `POST_MVP` (`B-003`) | Post-success late PR association | `EV-018` | `NEG-006` | `SUCCESS` snapshot with `NO_MATCH_OBSERVED` emits `DIRECT_COMMIT` `CANDIDATE`; later PR #1805 observation stays `unpersisted_external_observations` (separated from `PARTIAL` retry). |
