# Event Synthetic Dataset Coverage Matrix (`1.0.0-mvp-approved`)

- **Dataset Version / Approval Status**: `1.0.0-mvp-approved` / `PARTIALLY_APPROVED_MVP_REGRESSION` (`task_ref: T-001B`, baseline `qa/event-fixtures@0773ac5740b358f4168777fda1e578231cc6dff9`).
- **Case Count**: 18 cases = **16 approved MVP synthetic regression cases** (`EV-001`..`EV-016`, grouped into 12 same-source evaluation bundles) + **2 unapproved Post-MVP `B-003` drafts** (`EV-017`..`EV-018`, `POST_MVP_DRAFT_EXCLUDED`) + **6 isolated negative variants** (`NEG-001`..`NEG-006`, `eval_excluded: true`).
- **Evaluation Boundary**: Fixture lint checks **format, referential integrity, approval/bundle governance, SHA-256 integrity, and official domain invariants** only. **EVAL-EVENT** (`M3 / T-306A`) is downstream runtime evaluation and is **NOT** passed by fixture lint.

## 1. Official Traceability vs. Draft Local IDs

| Official Ref | Scope / Meaning | Mapped Cases | Status Note |
| --- | --- | --- | --- |
| `FR-006` | Source-based Event candidate generation & import boundary | `EV-001`..`EV-007`, `EV-017`, `EV-018` | `EV-001`..`EV-007` approved (MVP); `EV-017`..`EV-018` Post-MVP `DRAFT` |
| `FR-007` | Event review, curation commands, conflict & prohibition rules | `EV-007`..`EV-016` | Approved (MVP logical expectations; unconfirmed HTTP/precedence excluded) |
| `EVENT-10` | Direct commit eligibility & PR association lookup boundary | `EV-001`, `EV-002`, `EV-003`, `EV-004` | Approved (MVP logical expectations) |
| `EVENT-11` | User manual merge & single-count `USER_COMPOSITE` aggregation / roll-up | `EV-009`, `EV-010` | Approved (MVP logical expectations) |
| `EVENT-12` | Dissolve, prohibited links/nesting, version/workspace/repo conflicts & zero mutation | `EV-012`, `EV-013`, `EV-014`, `EV-015`, `EV-016` | Approved (MVP logical expectations + 0 mutation on rejection; unconfirmed HTTP/precedence excluded) |
| `EVAL-EVENT` | M3 downstream Event Builder runtime quality evaluation | *(M3 / T-306A)* | **Deferred — NOT passed by fixture lint** |

> Draft local IDs (`REQ-EV-*`, `T-001A-*`) are preserved in `draft_requirement_ids` / `draft_test_ids` strictly separated from official refs (`official_requirement_refs`, `official_test_refs`).

## 2. Comparison Policy (Approved vs. Excluded)

- **Approved & enforced for MVP synthetic regression (`EV-001`..`EV-016`)**:
  - **Anchor modes**: `PR`, `PR_SEGMENT`, `DIRECT_COMMIT`, `USER_COMPOSITE` (`PR` and `DIRECT_COMMIT` auto-generated; verified default-branch `NO_MATCH_OBSERVED` = 1 candidate per commit; multi-commit clustering out of MVP).
  - **Import runs**: `PARTIAL` (never `PARTIAL_SUCCESS`) when PR association lookup is incomplete; promotes `0` Events; `UNKNOWN` is never `NO_MATCH_OBSERVED`; default Event search returns `0`.
  - **Lifecycle statuses**: `CANDIDATE`, `CONFIRMED`, `REJECTED`, `SUPERSEDED`, `STALE`; top-level active/search/metrics visibility (`is_active_top_level`) is a separate boolean (no arbitrary `ACTIVE`/`EXCLUDED`/`DISSOLVED`).
  - **Split / Merge / Dissolve**: Disjoint `evidence_scope` split (`PR_SEGMENT` + `SUPERSEDED` parent); same-workspace & same-repo manual merge (`USER_COMPOSITE`, per-Event version check, snapshots, `SUPERSEDED` members, single-count search roll-up, no Fact/Claim auto-inheritance); dissolve (`expected_version` + `reason` + `idempotency_key`, `SUPERSEDED` + `REMOVED`, member re-evaluation, dependent `FACT`/`CLAIM`/`EXPORT` marked `STALE`).
  - **Mandatory rejection & zero-mutation checks**: Every rejected command MUST satisfy `outcome="REJECTED"`, `state_mutated=false`, `resulting_event_ids=[]`, and unmutated Event state, plus unambiguous official domain codes (`NESTED_COMPOSITE_NOT_ALLOWED`, `MEMBER_NOT_ACTIVE`, `EVENT_VERSION_CONFLICT`, `CROSS_REPOSITORY_MEMBER`).
- **Excluded from truth comparison (unconfirmed / deferred)**:
  - Unconfirmed HTTP status codes (`409` vs `412`, `If-Match`, unauthorized `403` vs `404`), multi-violation error precedence (`EV-014` `cmd_syn_014_nested`: `MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`), non-Event ID / unauthorized cross-workspace external wire codes (`EV-013`, `EV-016`), command wire names (`spec_command_name`), exact review flag string (`EV-007`), diagnostic record tokens (`EV-003`/`EV-004`/`EV-013`), detail GET history shape (`EV-010`), and Post-MVP `B-003` cases (`EV-017`/`EV-018`).

## 3. Requirement & Test Coverage Matrix

| Draft Req ID | Draft Test ID(s) | Official Refs | Scope / Status | Scenario Area | Input Case(s) | Isolated Neg. | Observable Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `REQ-EV-PR-01` | `T-001A-01` | `FR-006`, `EVENT-10` | `MVP` / **APPROVED** | Merged PR default candidate | `EV-001` | `NEG-001` | 1 merged PR (2 commits) -> 1 `PR` `CANDIDATE`; 0 direct-commit candidates. |
| `REQ-EV-COMMIT-01` | `T-001A-02` | `FR-006`, `EVENT-10` | `MVP` / **APPROVED** | Default-branch direct commit | `EV-002` | — | Verified start/end default-branch commit (`COMPLETED` + `NO_MATCH_OBSERVED`) -> 1 `DIRECT_COMMIT` `CANDIDATE` (1 per commit). |
| `REQ-EV-ASSOC-01` | `T-001A-03`, `T-001A-13`, `T-001A-18` | `FR-006`, `FR-007`, `EVENT-10`, `EVENT-12` | `MVP` / **PARTIAL** (`EV-018` `DRAFT`) | `UNKNOWN` PR association & `PARTIAL` run | `EV-003`, `EV-013`, `EV-018` | `NEG-003` | `PARTIAL` run promotes 0 Events, default Event search = 0 (`EV-003`); non-mergeable with 0 mutation (`EV-013`); separated from Post-MVP `B-003` draft (`EV-018`). |
| `REQ-EV-BRANCH-01` | `T-001A-04` | `FR-006`, `EVENT-10` | `MVP` / **APPROVED** | Non-default branch commit boundary | `EV-004` | — | Non-default branch commit -> 0 `DIRECT_COMMIT` candidates (physical collection stage excluded from truth check). |
| `REQ-EV-CONTEXT-01` | `T-001A-05`, `T-001A-13` | `FR-006`, `FR-007`, `EVENT-12` | `MVP` / **APPROVED** | Issue/ticket `CONTEXT_ONLY` | `EV-005`, `EV-013` | — | Ticket attaches as `CONTEXT_ONLY` on `PR` `CANDIDATE`; never standalone Event or merge member. |
| `REQ-EV-CONTEXT-02` | `T-001A-06` | `FR-006` | `MVP` / **APPROVED** | Shared ticket no auto-merge | `EV-006` | `NEG-005` | 2 merged PRs sharing `SYN-102` stay 2 standalone `PR` `CANDIDATE`s (`total_user_composite_events=0`). |
| `REQ-EV-MERGE-01` | `T-001A-06`, `T-001A-09` | `FR-006`, `FR-007`, `EVENT-11` | `MVP` / **APPROVED** | Auto merge vs user manual merge | `EV-006`, `EV-009` | `NEG-005` | No auto multi-PR merge (`EV-006`); explicit same-repo `manual_merge` creates 1 `USER_COMPOSITE` (`EV-009`). |
| `REQ-EV-MIXED-01` | `T-001A-07`, `T-001A-08` | `FR-006`, `FR-007` | `MVP` / **APPROVED** | Mixed-purpose PR & user split | `EV-007`, `EV-008` | — | 1 `PR` `CANDIDATE` with mixed-purpose review flag, 0 auto-split (`EV-007`); explicit split yields 2 `PR_SEGMENT`s (`EV-008`). |
| `REQ-EV-CMD-SPLIT-01` | `T-001A-08` | `FR-007` | `MVP` / **APPROVED** | User split (`PR_SEGMENT`, disjoint scope) | `EV-008` | `NEG-002` | Parent `PR` -> `SUPERSEDED` (v2); 2 `PR_SEGMENT` `CANDIDATE`s with disjoint concrete `evidence_scope` (`total_countable_events=2`). |
| `REQ-EV-CMD-MERGE-01` | `T-001A-09`, `T-001A-16` | `FR-007`, `EVENT-11`, `EVENT-12` | `MVP` / **APPROVED** | User manual merge (same-repo only) | `EV-009`, `EV-016` | `NEG-005` | Same-repo merge checks per-Event versions, preserves snapshots, marks members `SUPERSEDED`, yields 1 `USER_COMPOSITE` (`EV-009`); cross-repo fails with `CROSS_REPOSITORY_MEMBER` + 0 mutation (`EV-016`). |
| `REQ-EV-COMP-01` | `T-001A-10`, `T-001A-12`, `T-001A-14` | `FR-007`, `EVENT-11`, `EVENT-12` | `MVP` / **APPROVED** | Composite single-count, roll-up & anti-nesting | `EV-010`, `EV-012`, `EV-014` | `NEG-004` | `SUPERSEDED` members never double-counted; member-only search hit rolls up once to `USER_COMPOSITE` (`EV-010`); nesting rejected with 0 mutation (`EV-014`); dissolve removes membership (`EV-012`). |
| `REQ-EV-CMD-EXCLUDE-01` | `T-001A-11` | `FR-007` | `MVP` / **APPROVED** | User exclude (`REJECTED`) | `EV-011` | `NEG-002` | Target transitions to `REJECTED` (v2, `is_active_top_level=false`), Source preserved, sibling stays `CANDIDATE`. |
| `REQ-EV-CMD-DISSOLVE-01` | `T-001A-12` | `FR-007`, `EVENT-12` | `MVP` / **APPROVED** | User dissolve & `STALE` propagation | `EV-012` | — | Requires `expected_version`, `reason`, `idempotency_key`; Composite -> `SUPERSEDED` + `REMOVED`; members re-evaluated (`CONFIRMED`/`CANDIDATE`/`STALE`); dependent `FACT`, `CLAIM`, `EXPORT` -> `STALE` (not deleted/transferred). |
| `REQ-EV-ERR-LINK-01` | `T-001A-13` | `FR-007`, `EVENT-12` | `MVP` / **APPROVED** | Non-Event ticket / `UNKNOWN` commit reject | `EV-013` | — | Ticket and `PARTIAL`-run `UNKNOWN` commit on distinct repo rejected with **0 mutation**; wire error codes & HTTP statuses excluded from truth comparison. |
| `REQ-EV-ERR-LINK-02` | `T-001A-14` | `FR-007`, `EVENT-12` | `MVP` / **APPROVED** | Nesting & inactive member reject | `EV-014` | — | Aligned `expected_version=2`; Composite root -> `NESTED_COMPOSITE_NOT_ALLOWED`; `REJECTED` member -> `MEMBER_NOT_ACTIVE`; already-composed `SUPERSEDED` member -> **0 mutation** (precedence & HTTP excluded). |
| `REQ-EV-ERR-VER-01` | `T-001A-15` | `FR-007`, `EVENT-12` | `MVP` / **APPROVED** | Per-Event version conflict | `EV-015` | — | Stale `expected_version=1` vs v2 -> `EVENT_VERSION_CONFLICT` + **0 mutation** (HTTP `409`/`412` and `If-Match` excluded). |
| `REQ-EV-ERR-WS-01` | `T-001A-16` | `FR-007`, `EVENT-12` | `MVP` / **APPROVED** | Cross-repo & cross-workspace isolation | `EV-016` | — | Same-WS cross-repo -> `CROSS_REPOSITORY_MEMBER`; cross-WS merge/exclude -> **0 mutation** (external HTTP `403`/`404` excluded); per-WS API counts (`2`, `1`) separated from fixture total (`3`). |
| `REQ-EV-POST-01` | `T-001A-17` | `FR-006` | `POST_MVP` (`B-003`) / **DRAFT** | Post-success auto change discovery | `EV-017` | `NEG-006` | Unapproved Post-MVP draft; post-`SUCCESS` PR #1702 stays in `unpersisted_external_observations` (`persisted_in_mvp=false`, `total_held_items=0`). |
| `REQ-EV-POST-02` | `T-001A-18` | `FR-006` | `POST_MVP` (`B-003`) / **DRAFT** | Post-success late PR association | `EV-018` | `NEG-006` | Unapproved Post-MVP draft; `SUCCESS` snapshot with `NO_MATCH_OBSERVED` emits `DIRECT_COMMIT` `CANDIDATE`; later PR #1805 observation stays `unpersisted_external_observations`. |
