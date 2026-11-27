# T-001A Event Synthetic Dataset — Coverage Matrix (DRAFT)

> **Status**: `DRAFT` (agent-authored; **NOT frozen as golden**; pending independent human review and freeze in **T-001B**).  
> **Notion Notice**: Linked Notion original documents (`T-001A`, `AI 합성 Dataset 가이드`, `Snapshot Import`, `ADR-018`) require authentication and could not be accessed via HTTP. Confirmed behavioral rules from the official spec briefing are enforced across all 18 cases; **exact wire status names, error codes, and official Requirement/Test IDs are NOT guessed and remain explicitly `UNCONFIRMED` / `REVIEW_REQUIRED`**.

## 1. Confirmed Official Spec Rules vs. Unconfirmed Tokens vs. Truly Undecided Items

### A. Confirmed Rules Applied Across `EV-001..EV-018`
1. **Merged PR is the default Event candidate** (`EV-001`, `EV-005..EV-007`); associated commits attach as evidence without spawning duplicate direct-commit Events.
2. **Default-branch direct commit is a candidate ONLY when PR association lookup completed normally (`COMPLETED`) and observed `NO_MATCH_OBSERVED`** (`EV-002`, `EV-004`).
3. **`UNKNOWN` PR association is NEVER interpreted as `NO_MATCH_OBSERVED` (no PR)**; classification is held (`EV-003`, `EV-013`, `EV-018`, `NEG-003`).
4. **Issue/ticket links attach strictly as `CONTEXT_ONLY` and NEVER trigger automatic merging** (`EV-005`, `EV-006`, `EV-013`, `NEG-005`).
5. **User manual merge is allowed ONLY within the same repository** (and same workspace) and is aggregated/counted as **one `USER_COMPOSITE`** with zero member double counting (`EV-009`, `EV-010`, `EV-016`, `NEG-004`).
6. **`USER_COMPOSITE` nesting is strictly forbidden** — neither a `USER_COMPOSITE` root nor an already-subsumed member can be merged into another `USER_COMPOSITE` (`EV-014`).
7. **Dissolving a `USER_COMPOSITE` restores its constituent members and marks dependent Claims as `stale`** (`EV-012`).
8. **`B-003` scenarios are tagged `POST_MVP`**, **no OD-007 physical DB tables are assumed**, and **all expected proposals stay `DRAFT`** (`EV-017`, `EV-018`, `NEG-006`).

### B. Unconfirmed Wire Tokens (Left `UNCONFIRMED`, Never Guessed)
- Official Event type names for merged PR, direct commit, and split child Events (`spec_event_type="UNCONFIRMED"`; only `USER_COMPOSITE` is confirmed).
- Official lifecycle status names for active, excluded, dissolved, superseded, and subsumed Events (`spec_lifecycle_status="UNCONFIRMED"`).
- Official hold/deferred status names for `UNKNOWN` commits, non-default branch commits, and `POST_MVP` observations (`spec_hold_status="UNCONFIRMED"`).
- Official command wire names for split, manual merge, exclude, and dissolve (`spec_command_name="UNCONFIRMED"`).
- Official error codes and HTTP statuses for version conflict, workspace boundary conflict, and cross-repository manual merge (`spec_error_code="UNCONFIRMED"`; only `CANNOT_LINK` is confirmed).
- Official Claim status enum/field name for `stale` Claims (`spec_claim_status="UNCONFIRMED"`).
- Official Notion T-001A Requirement IDs (`REQ-EV-*`) and Test IDs (`T-001A-*`).

### C. Truly Undecided Behavioral Items (Human Decisions Needed in T-001B)
See `review-queue.md` Section 3 for the full per-case list (merge-commit SHA inclusion, consecutive direct-commit grouping, held-item search visibility, upstream Snapshot Import branch filtering, ticket-key search indexing, non-binding UI hints, 1-commit squash-PR split partitioning, PR + direct-commit manual merge, member-text search roll-up & direct GET, exclude reversibility/cascade, dissolve member-version check, non-Event ID pre-validation vs `CANNOT_LINK`, non-nesting append command vs dissolve+re-merge, optimistic-lock granularity, OD-010 foreign-workspace hiding, and `POST_MVP` harness assertion style).

## 2. Requirement / Test ID → Input → Observable Outcome (18 Cases)

| Requirement ID | Test ID(s) | Scope | Input Case(s) | Negative Variant(s) | Observable Outcome (DRAFT) | Review Status |
| --- | --- | --- | --- | --- | --- | --- |
| `REQ-EV-PR-01` | `T-001A-01` | `MVP` | `EV-001` | `NEG-001` | Merged PR + 2 associated commits → 1 active merged-PR Event (`spec_event_type="UNCONFIRMED"`); commits attached as evidence, not duplicate Events | `REVIEW_REQUIRED` |
| `REQ-EV-COMMIT-01` | `T-001A-02` | `MVP` | `EV-002` | — | Default-branch commit with `pr_association_lookup_status="COMPLETED"` and `pr_association_status="NO_MATCH_OBSERVED"` → 1 active direct-commit Event | `REVIEW_REQUIRED` |
| `REQ-EV-ASSOC-01` | `T-001A-03`, `T-001A-13`, `T-001A-18` | `MVP` | `EV-003`, `EV-013`, `EV-018` | `NEG-003` | `UNKNOWN` PR association (`INCOMPLETE_OR_FAILED`) → held (`interpreted_as_no_pr=false`, `spec_hold_status="UNCONFIRMED"`); never treated as `NO_MATCH_OBSERVED`; non-linkable (`CANNOT_LINK`) | `REVIEW_REQUIRED` |
| `REQ-EV-BRANCH-01` | `T-001A-04` | `MVP` | `EV-004` | — | Non-default branch commit with `COMPLETED` + `NO_MATCH_OBSERVED` → not a direct-commit Event candidate (`spec_hold_status="UNCONFIRMED"`) | `REVIEW_REQUIRED` |
| `REQ-EV-CONTEXT-01` | `T-001A-05`, `T-001A-13` | `MVP` | `EV-005`, `EV-013` | — | Issue/Ticket attaches strictly as `CONTEXT_ONLY`; never creates standalone Event; rejected (`CANNOT_LINK`) if passed to manual merge | `REVIEW_REQUIRED` |
| `REQ-EV-CONTEXT-02` | `T-001A-06` | `MVP` | `EV-006` | `NEG-005` | Two merged PRs referencing the same `CONTEXT_ONLY` ticket stay 2 distinct Events (`total_user_composite_events=0`; no auto-merge via Issue/ticket link) | `REVIEW_REQUIRED` |
| `REQ-EV-MERGE-01` | `T-001A-06`, `T-001A-09` | `MVP` | `EV-006`, `EV-009` | `NEG-005` | Clear separation: zero automatic multi-PR merges (`EV-006`); `USER_COMPOSITE` created solely via same-repo user manual merge (`EV-009`, `merge_origin="USER_MANUAL_MERGE"`) | `REVIEW_REQUIRED` |
| `REQ-EV-MIXED-01` | `T-001A-07`, `T-001A-08` | `MVP` | `EV-007`, `EV-008` | — | Mixed-purpose PR stays 1 merged-PR Event initially (`EV-007`); explicit user split partitions into 2 active split-child Events (`EV-008`) | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-SPLIT-01` | `T-001A-08` | `MVP` | `EV-008` | `NEG-002` | User split supersedes parent (`version=2`, `counted_in_metrics=false`) and emits 2 active split-child Events (`total_countable_events=2`) | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-MERGE-01` | `T-001A-09`, `T-001A-16` | `MVP` | `EV-009`, `EV-016` | `NEG-005` | Same-repo manual merge emits 1 active `USER_COMPOSITE` and subsumes 2 member PR Events (`EV-009`: `total_countable_events=1`, `total_user_composite_events=1`); same-workspace cross-repo merge is rejected (`EV-016`) | `REVIEW_REQUIRED` |
| `REQ-EV-COMP-01` | `T-001A-10`, `T-001A-12`, `T-001A-14` | `MVP` | `EV-010`, `EV-012`, `EV-014` | `NEG-004` | Search & metrics count 1 `USER_COMPOSITE` and never double-count subsumed members (`EV-010`); nesting is strictly forbidden (`EV-014`); dissolve restores members (`EV-012`) | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-EXCLUDE-01` | `T-001A-11` | `MVP` | `EV-011` | `NEG-002` | User exclude marks target `logical_state="excluded"` (`spec_lifecycle_status="UNCONFIRMED"`, `version=2`), hides from active search/metrics, keeps sibling active and source immutable | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-DISSOLVE-01` | `T-001A-12` | `MVP` | `EV-012` | — | User dissolve marks `USER_COMPOSITE` dissolved (`counted_in_metrics=false`), restores 2 members to standalone active (`total_countable_events=2`), and marks dependent Claim `clm_syn_012_1` as `STALE` (`spec_claim_status="UNCONFIRMED"`) | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-LINK-01` | `T-001A-13` | `MVP` | `EV-013` | — | Manual merge targeting `CONTEXT_ONLY` ticket or `UNKNOWN`-held commit rejected with `CANNOT_LINK`, `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-LINK-02` | `T-001A-14` | `MVP` | `EV-014` | — | Manual merge targeting already-subsumed `USER_COMPOSITE` member, `USER_COMPOSITE` root (nesting forbidden), or excluded Event rejected with `CANNOT_LINK`, `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-VER-01` | `T-001A-15` | `MVP` | `EV-015` | — | Stale `expected_version=1` vs current `version=2` rejected (`rejection_category="version_conflict"`, `spec_error_code="UNCONFIRMED"`), `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-WS-01` | `T-001A-16` | `MVP` | `EV-016` | — | Cross-workspace manual merge, foreign-workspace exclude, and same-workspace cross-repo manual merge rejected (`spec_error_code="UNCONFIRMED"`), `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-POST-01` | `T-001A-17` | `POST_MVP` (`B-003`) | `EV-017` | `NEG-006` | Post-success automatic change discovery tagged `POST_MVP` (`B-003`); deferred in MVP (`spec_hold_status="UNCONFIRMED"`) | `REVIEW_REQUIRED` |
| `REQ-EV-POST-02` | `T-001A-18` | `POST_MVP` (`B-003`) | `EV-018` | `NEG-003`, `NEG-006` | Late source association recovery tagged `POST_MVP` (`B-003`); MVP holds `UNKNOWN` commit without interpreting it as `NO_MATCH_OBSERVED` | `REVIEW_REQUIRED` |

## 3. Automated Safety & Spec Invariants Enforced by `lint_fixtures.py`

| Invariant | Verified in Valid Cases (`EV-001..EV-018`) | Proven Rejected via Negative Variant / Regression Test | Lint Rule Code |
| --- | --- | --- | --- |
| Merged PR is default candidate (unmerged PR rejected) | `EV-001`, `EV-005..EV-016` | `test_unmerged_pr_candidate_rejected` | `INVARIANT_MERGED_PR_CANDIDATE` |
| Direct commit requires default branch + `COMPLETED` lookup + `NO_MATCH_OBSERVED` | `EV-002`, `EV-004` | `test_direct_commit_requires_completed_lookup_and_no_match_observed` | `INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED` |
| `UNKNOWN` is never interpreted as `NO_MATCH_OBSERVED` | `EV-003`, `EV-013`, `EV-018` | `NEG-003` | `INVARIANT_UNKNOWN_NOT_NO_PR` |
| `USER_COMPOSITE` same-repository only & nesting forbidden | `EV-009`, `EV-014`, `EV-016` | `test_cross_repo_manual_merge_acceptance_rejected`, `test_nested_user_composite_rejected` | `INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING` |
| `USER_COMPOSITE` members never double-counted in search or metrics | `EV-009`, `EV-010`, `EV-012`, `EV-014` | `NEG-004` | `INVARIANT_NO_COMPOSITE_DOUBLE_COUNT` |
| `CONTEXT_ONLY` relations never cause automatic multi-PR merge | `EV-005`, `EV-006`, `EV-013` | `NEG-005` | `INVARIANT_NO_CONTEXT_AUTO_MERGE` |
| Dissolving `USER_COMPOSITE` marks dependent Claims `STALE` | `EV-012` | `test_dissolve_missing_stale_dependent_claim_rejected` | `INVARIANT_DISSOLVE_MARKS_DEPENDENT_CLAIMS_STALE` |
| Unconfirmed wire status/error/command tokens are never guessed | All `EV-001..EV-018` | `test_guessed_unconfirmed_status_or_error_code_rejected` | `NO_GUESSED_UNCONFIRMED_SPEC_TOKENS` |
| Agent never approves/freezes expected proposals (`DRAFT` only) | All `EV-001..EV-018` | `NEG-006` | `APPROVAL_STATUS_MUST_BE_DRAFT` |
| `B-003` scenarios explicitly tagged `POST_MVP` & no OD-007 physical tables | `EV-017`, `EV-018` / All | `NEG-006` | `SCOPE_TAG_POLICY`, `OD007_NO_PHYSICAL_TABLE` |
