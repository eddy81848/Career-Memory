# T-001B Human Review Queue (Event Dataset Draft)

> **Important**: All `expected_proposal` blocks in `cases/EV-001..EV-018` remain **`DRAFT`** proposals (NOT approved, **NOT frozen as golden**).  
> Original Notion documents (`T-001A`, `AI 합성 Dataset 가이드`, `Snapshot Import`, `ADR-018`) require authentication and could not be accessed here. Therefore, this review queue explicitly separates:
> 1. **Confirmed Official Spec Rules** (already applied and enforced by linter),
> 2. **Unconfirmed Wire Tokens (`UNCONFIRMED`)** (NOT guessed — fill from Notion in T-001B), and
> 3. **Truly Undecided Behavioral Items** (require human engineering decisions in T-001B).

---

## 1. Confirmed Official Spec Rules (Applied Across `EV-001..EV-018`)

- **Merged PR** is the default Event candidate (`EV-001`, `EV-005..EV-016`).
- **Default-branch direct commit** is a candidate **only** when PR association lookup completed normally (`pr_association_lookup_status="COMPLETED"`) and observed `pr_association_status="NO_MATCH_OBSERVED"` (`EV-002`, `EV-004`).
- **`UNKNOWN`** is **never** interpreted as `NO_MATCH_OBSERVED` (no PR) (`EV-003`, `EV-013`, `EV-018`).
- **Issue/ticket links** are `CONTEXT_ONLY` and **never** trigger automatic merging (`EV-005`, `EV-006`, `EV-013`).
- **User manual merge** is permitted **only within the same repository** (and same workspace) and is aggregated/counted as **one `USER_COMPOSITE`** without double counting constituent members (`EV-009`, `EV-010`, `EV-016`).
- **`USER_COMPOSITE` nesting is strictly forbidden** — neither a `USER_COMPOSITE` root nor an already-subsumed member may be merged into another `USER_COMPOSITE` (`EV-014`).
- **Dissolving a `USER_COMPOSITE`** restores constituent members and marks dependent Claims as **`stale`** (`EV-012`).
- **`B-003`** scenarios are tagged `POST_MVP`, no **OD-007** physical tables are assumed, and dataset stays **`DRAFT`**.

---

## 2. Unconfirmed Spec Tokens Left as `UNCONFIRMED` (Not Guessed — Fill from Notion in T-001B)

Because Notion original pages could not be read, the following wire-level strings were intentionally **not guessed** (enforced by lint rule `NO_GUESSED_UNCONFIRMED_SPEC_TOKENS`):

1. **`spec_event_type`** (only `USER_COMPOSITE` is confirmed): exact official type names for merged PR Event, default-branch direct-commit Event, and split-child Event (`EV-001..EV-018`).
2. **`spec_lifecycle_status`**: exact official status names for active, excluded, dissolved, superseded, and subsumed Events (`EV-001..EV-018`).
3. **`spec_hold_status`**: exact official hold/deferred status names for `UNKNOWN` association commits, non-default branch orphan commits, and `POST_MVP` observations (`EV-003`, `EV-004`, `EV-013`, `EV-017`, `EV-018`).
4. **`spec_command_name`**: exact official command wire names for split, manual merge, exclude, and dissolve (`EV-008`, `EV-009`, `EV-011..EV-016`).
5. **`spec_error_code` & HTTP statuses** (only `CANNOT_LINK` is confirmed): exact error codes & HTTP statuses for version conflict (`EV-015`), cross-workspace boundary conflict (`EV-016`), same-workspace cross-repository manual merge rejection (`EV-016`, whether `CANNOT_LINK` or a distinct code), excluded-target merge (`EV-014`), and HTTP status for `CANNOT_LINK` (`EV-013`, `EV-014`).
6. **`spec_claim_status`**: exact official Claim enum/field name representing the confirmed `stale` state (`EV-012`, cross-check with CLAIM-QA).
7. **Official Requirement / Test IDs**: exact Notion T-001A Requirement IDs and Test IDs replacing provisional `REQ-EV-*` / `T-001A-*`.

---

## 3. Truly Undecided Behavioral Items (Human Engineering Decisions Required in T-001B)

- **P0 — `EV-003` (`UNKNOWN` visibility)**: Are held `UNKNOWN` commits completely absent from default Event search (`expected_result_count=0`) and surfaced only in a dedicated pending/unclassified bucket?
- **P0 — `EV-007` & `EV-008` (Mixed-purpose PR & Split)**: Does MVP emit any non-binding mixed-purpose hint on `EV-007`? How does split partition a single-commit squash-merged PR, and is split reversible in MVP?
- **P0 — `EV-009` (Cross-kind same-repo merge)**: Can same-repository manual merge combine a merged PR Event with a default-branch `NO_MATCH_OBSERVED` direct-commit Event?
- **P0 — `EV-010` (`USER_COMPOSITE` search roll-up & member GET)**: Does a search query matching text unique to one member PR (`"replay CLI"`) roll up to the owning `USER_COMPOSITE` once? What does direct GET of a subsumed member Event ID return?
- **P0 — `EV-012` (Dissolve lock scope & non-composite error)**: Does dissolve validate `expected_version` only on the `USER_COMPOSITE` or also on each constituent member? What error applies if dissolve targets a non-`USER_COMPOSITE` Event?
- **P0 — `EV-013` (Non-Event IDs in manual merge)**: Does passing a `CONTEXT_ONLY` ticket ID or held `UNKNOWN` commit ID reach domain `CANNOT_LINK` or fail earlier at request validation?
- **P0 — `EV-014` (Adding a 3rd PR without nesting)**: Nesting is confirmed forbidden; does adding a 3rd PR require dissolve-then-merge in MVP or is there a separate non-nesting append-member command?
- **P0 — `EV-015` (Optimistic lock granularity)**: Is version checking per-Event (`target_versions`), per-Workspace, or via HTTP `If-Match`/ETag?
- **P0 — `EV-016` (OD-010 foreign-workspace hiding)**: When caller lacks membership in the foreign workspace (`ws_syn_016_b`), should foreign-workspace commands return existence-hiding `403/404`?
- **P1 — `EV-001`, `EV-002`, `EV-004`, `EV-005`, `EV-006`, `EV-011`**: Merge-commit SHA inclusion (`EV-001`), consecutive direct-commit grouping (`EV-002`), upstream Snapshot Import filtering of non-default orphan commits (`EV-004`), ticket-key search indexing & unreferenced tickets (`EV-005`), non-binding UI hints for shared tickets (`EV-006`), exclude reversibility & composite-exclude cascade (`EV-011`).
- **P2 — `EV-017`, `EV-018` (`POST_MVP` / `B-003`)**: Whether `POST_MVP` fixtures assert MVP non-ingestion (current draft) or B-003 post-sync state behind a flag; whether held `UNKNOWN` items are curation-locked until recovery; whether to add a late `NO_MATCH_OBSERVED` recovery variant.

---

## 4. Case-by-Case Review Queue (`RQ-001`..`RQ-018`)

| Review ID | Case ID | Scope | Priority | Case File | Unconfirmed Tokens (`UNCONFIRMED`) | Truly Undecided Decision Focus |
| --- | --- | --- | --- | --- | --- | --- |
| `RQ-001` | `EV-001` | `MVP` | P1 | `cases/EV-001-pr-centered-basic.json` | `spec_event_type`, `spec_lifecycle_status`, official IDs | Merge-commit SHA inclusion in `source_commit_ids` |
| `RQ-002` | `EV-002` | `MVP` | P1 | `cases/EV-002-default-branch-direct-commit.json` | `spec_event_type`, `spec_lifecycle_status` | Consecutive default-branch `NO_MATCH_OBSERVED` commit grouping |
| `RQ-003` | `EV-003` | `MVP` | P0 | `cases/EV-003-unknown-association-hold.json` | `spec_hold_status` | Default search exclusion (`count=0`) vs pending bucket |
| `RQ-004` | `EV-004` | `MVP` | P1 | `cases/EV-004-non-default-branch-no-pr.json` | `spec_hold_status` | Snapshot Import upstream filter vs Event Builder skip/hold |
| `RQ-005` | `EV-005` | `MVP` | P1 | `cases/EV-005-issue-ticket-context-only.json` | `spec_event_type`, `spec_lifecycle_status` | `CONTEXT_ONLY` ticket-key search matching & unreferenced tickets |
| `RQ-006` | `EV-006` | `MVP` | P1 | `cases/EV-006-shared-ticket-no-auto-merge.json` | `spec_event_type`, `spec_lifecycle_status` | Zero UI merge hints vs non-binding suggestion (auto-merge forbidden) |
| `RQ-007` | `EV-007` | `MVP` | P0 | `cases/EV-007-mixed-purpose-pr.json` | `spec_event_type`, `spec_lifecycle_status` | Non-destructive mixed-purpose hint flag vs unflagged Event |
| `RQ-008` | `EV-008` | `MVP` | P0 | `cases/EV-008-user-split-mixed-pr.json` | `spec_command_name`, `spec_lifecycle_status`, `spec_event_type` | Squash 1-commit PR split partitioning & undo-split scope |
| `RQ-009` | `EV-009` | `MVP` | P0 | `cases/EV-009-user-manual-merge-composite.json` | `spec_command_name`, `spec_lifecycle_status` | Same-repo merge of merged-PR Event + direct-commit Event |
| `RQ-010` | `EV-010` | `MVP` | P0 | `cases/EV-010-composite-no-double-counting.json` | `spec_lifecycle_status` | Member-only text search roll-up & direct GET of subsumed member |
| `RQ-011` | `EV-011` | `MVP` | P1 | `cases/EV-011-user-exclude-event.json` | `spec_command_name`, `spec_lifecycle_status` | Exclude reversibility & `USER_COMPOSITE` exclude cascade |
| `RQ-012` | `EV-012` | `MVP` | P0 | `cases/EV-012-user-dissolve-composite.json` | `spec_command_name`, `spec_lifecycle_status`, `spec_claim_status` | Dissolve member-version lock check & non-composite error (`stale` Claim confirmed) |
| `RQ-013` | `EV-013` | `MVP` | P0 | `cases/EV-013-cannot-link-context-and-unknown.json` | `spec_hold_status`, `spec_command_name`, HTTP status | Non-Event IDs reaching domain `CANNOT_LINK` vs pre-validation |
| `RQ-014` | `EV-014` | `MVP` | P0 | `cases/EV-014-cannot-link-nested-and-excluded.json` | HTTP status, excluded-target error code | Non-nesting append command vs dissolve+re-merge (nesting forbidden confirmed) |
| `RQ-015` | `EV-015` | `MVP` | P0 | `cases/EV-015-version-conflict-optimistic-lock.json` | `spec_error_code`, HTTP status, `spec_command_name` | Optimistic lock granularity (per-Event vs Workspace vs `If-Match`) |
| `RQ-016` | `EV-016` | `MVP` | P0 | `cases/EV-016-workspace-boundary-conflict.json` | `spec_error_code` (workspace & cross-repo), HTTP status | OD-010 foreign-workspace existence hiding (same-repo merge confirmed) |
| `RQ-017` | `EV-017` | `POST_MVP` | P2 | `cases/EV-017-post-mvp-b003-auto-change-discovery.json` | `spec_event_type`, `spec_lifecycle_status`, `spec_hold_status` | `POST_MVP` runner assertion style (MVP non-ingestion vs B-003 state) |
| `RQ-018` | `EV-018` | `POST_MVP` | P2 | `cases/EV-018-post-mvp-b003-late-association-recovery.json` | `spec_hold_status` | Held-item curation lock & companion late `NO_MATCH_OBSERVED` case |
