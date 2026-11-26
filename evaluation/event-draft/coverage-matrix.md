# T-001A Event Synthetic Dataset — Coverage Matrix (DRAFT)

> **Status**: `DRAFT` (agent-authored; pending independent human review and freeze in **T-001B**).  
> **Notion Notice**: Linked Notion contract/annotation pages required authentication and were unreadable in this environment. Requirement IDs (`REQ-EV-*`) and Test IDs (`T-001A-*`) below are provisional draft identifiers aligned to the T-001A task brief; exact canonical IDs/enums must be confirmed in T-001B.

## 1. Requirement / Test ID → Input → Observable Outcome

| Requirement ID | Test ID(s) | Scope | Input Case(s) | Negative Variant(s) | Observable Outcome (DRAFT) | Review Status |
| --- | --- | --- | --- | --- | --- | --- |
| `REQ-EV-PR-01` | `T-001A-01` | `MVP` | `EV-001` | `NEG-001` | Merged PR + 2 associated commits → 1 `ACTIVE` `PR_CENTERED` Event; commits attached as evidence, not duplicate Events | `REVIEW_REQUIRED` |
| `REQ-EV-COMMIT-01` | `T-001A-02` | `MVP` | `EV-002` | — | Default-branch commit with confirmed `pr_association_status="NONE"` → 1 `ACTIVE` `DIRECT_COMMIT` Event | `REVIEW_REQUIRED` |
| `REQ-EV-ASSOC-01` | `T-001A-03`, `T-001A-13`, `T-001A-18` | `MVP` | `EV-003`, `EV-013`, `EV-018` | `NEG-003` | `UNKNOWN` PR association → `CLASSIFICATION_HELD` (`interpreted_as_no_pr=false`); never treated as `NO_PR` / `DIRECT_COMMIT`; non-linkable (`CANNOT_LINK`) | `REVIEW_REQUIRED` |
| `REQ-EV-BRANCH-01` | `T-001A-04` | `MVP` | `EV-004` | — | Non-default branch commit without PR → not a `DIRECT_COMMIT` Event (`NON_ELIGIBLE_BRANCH` pending Snapshot Import filter confirmation) | `REVIEW_REQUIRED` |
| `REQ-EV-CONTEXT-01` | `T-001A-05`, `T-001A-13` | `MVP` | `EV-005`, `EV-013` | — | Issue/Ticket attaches strictly as `CONTEXT_ONLY`; never creates standalone Event; rejected (`CANNOT_LINK`) if passed to `MANUAL_MERGE` | `REVIEW_REQUIRED` |
| `REQ-EV-CONTEXT-02` | `T-001A-06` | `MVP` | `EV-006` | `NEG-005` | Two PRs referencing the same `CONTEXT_ONLY` ticket stay 2 independent `PR_CENTERED` Events (`total_composite_events=0`) | `REVIEW_REQUIRED` |
| `REQ-EV-MERGE-01` | `T-001A-06`, `T-001A-09` | `MVP` | `EV-006`, `EV-009` | `NEG-005` | Clear separation: zero automatic multi-PR merges (`EV-006`); Composite created solely via user `MANUAL_MERGE` (`EV-009`, `merge_origin="USER_MANUAL_MERGE"`) | `REVIEW_REQUIRED` |
| `REQ-EV-MIXED-01` | `T-001A-07`, `T-001A-08` | `MVP` | `EV-007`, `EV-008` | — | Mixed-purpose PR stays 1 `PR_CENTERED` Event initially (`EV-007`); user `SPLIT` partitions into 2 `SPLIT_CHILD` Events (`EV-008`) | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-SPLIT-01` | `T-001A-08` | `MVP` | `EV-008` | `NEG-002` | `SPLIT` supersedes parent (`version=2`, `counted_in_metrics=false`) and emits 2 active `SPLIT_CHILD` Events (`total_countable_events=2`) | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-MERGE-01` | `T-001A-09` | `MVP` | `EV-009` | `NEG-005` | `MANUAL_MERGE` emits 1 `ACTIVE` `COMPOSITE` Event and subsumes 2 member PR Events (`total_countable_events=1`) | `REVIEW_REQUIRED` |
| `REQ-EV-COMP-01` | `T-001A-10`, `T-001A-12`, `T-001A-14` | `MVP` | `EV-010`, `EV-012`, `EV-014` | `NEG-004` | Search & metrics never double-count Composite + subsumed members; shared keyword matches Composite once; workspace count excludes subsumed members | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-EXCLUDE-01` | `T-001A-11` | `MVP` | `EV-011` | `NEG-002` | `EXCLUDE` marks target `EXCLUDED` (`version=2`), hides from default search/metrics, keeps sibling active and source immutable | `REVIEW_REQUIRED` |
| `REQ-EV-CMD-DISSOLVE-01` | `T-001A-12` | `MVP` | `EV-012` | — | `DISSOLVE` marks Composite `DISSOLVED` (`counted_in_metrics=false`) and restores 2 members to `STANDALONE` active (`total_countable_events=2`) | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-LINK-01` | `T-001A-13` | `MVP` | `EV-013` | — | `MANUAL_MERGE` targeting `CONTEXT_ONLY` ticket or `UNKNOWN`-held commit rejected with `CANNOT_LINK`, `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-LINK-02` | `T-001A-14` | `MVP` | `EV-014` | — | `MANUAL_MERGE` targeting already-subsumed Composite member or `EXCLUDED` Event rejected with `CANNOT_LINK`, `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-VER-01` | `T-001A-15` | `MVP` | `EV-015` | — | Stale `expected_version=1` vs current `version=2` rejected with `VERSION_CONFLICT`, `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-ERR-WS-01` | `T-001A-16` | `MVP` | `EV-016` | — | Cross-workspace `MANUAL_MERGE` and foreign-workspace `EXCLUDE` rejected with `WORKSPACE_CONFLICT` (OD-010 hiding `REVIEW_REQUIRED`), `state_mutated=false` | `REVIEW_REQUIRED` |
| `REQ-EV-POST-01` | `T-001A-17` | `POST_MVP` (`B-003`) | `EV-017` | `NEG-006` | Post-success automatic change discovery tagged `POST_MVP` (`B-003`); deferred in MVP (`DEFERRED_POST_MVP`) | `REVIEW_REQUIRED` |
| `REQ-EV-POST-02` | `T-001A-18` | `POST_MVP` (`B-003`) | `EV-018` | `NEG-003`, `NEG-006` | Late source association recovery tagged `POST_MVP` (`B-003`); MVP holds `UNKNOWN` commit without misclassifying as `DIRECT_COMMIT` | `REVIEW_REQUIRED` |

## 2. Mandatory Safety Invariant Verification

| Invariant | Enforced in Valid Cases (`EV-001..EV-018`) | Proven Rejected via Negative Variant | Lint Rule Code |
| --- | --- | --- | --- |
| `UNKNOWN` is never interpreted as `NO_PR` / `DIRECT_COMMIT` | `EV-003`, `EV-013`, `EV-018` | `NEG-003` | `INVARIANT_UNKNOWN_NOT_NO_PR` |
| Composite members are never double-counted in search or metrics | `EV-009`, `EV-010`, `EV-012`, `EV-014` | `NEG-004` | `INVARIANT_NO_COMPOSITE_DOUBLE_COUNT` |
| `CONTEXT_ONLY` relations never cause automatic multi-PR merge | `EV-005`, `EV-006`, `EV-013` | `NEG-005` | `INVARIANT_NO_CONTEXT_AUTO_MERGE` |
| Agent never approves/freezes expected proposals (`DRAFT` only) | All `EV-001..EV-018` | `NEG-006` | `APPROVAL_STATUS_MUST_BE_DRAFT` |
| `B-003` scenarios explicitly tagged `POST_MVP` | `EV-017`, `EV-018` | `NEG-006` | `SCOPE_TAG_POLICY` |
| No OD-007 physical DB table assumptions | All `EV-001..EV-018` | `NEG-006` | `OD007_NO_PHYSICAL_TABLE` |

## 3. Known Coverage Gaps / Deferred to T-001B or M3

- **Unverified Notion Test IDs**: Official T-001A / ADR-018 test IDs and exact status/error enums could not be fetched from Notion; all provisional tokens are queued in `review-queue.md`.
- **Squash/Rebase Single-Commit PR Split**: `EV-008` partitions by `source_commit_ids`; how `SPLIT` partitions a 1-commit squash-merged PR is left as an open question in `review-queue.md`.
- **Reversing `EXCLUDE` (`RESTORE`/`INCLUDE`) & Multi-Event Command Partial Failure**: Deferred pending confirmation of exact MVP curation command list in the Notion Event Contract.
- **Cross-domain Event ↔ Claim Coupling** (e.g., dissolving a Composite referenced by a Claim): Explicitly out of EVENT-QA's single-domain draft scope; flagged in `review-queue.md` for T-001B cross-review with CLAIM-QA.
