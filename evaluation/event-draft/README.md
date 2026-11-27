# T-001A Event Synthetic Dataset Draft (`evaluation/event-draft/`)

This directory contains **AI-authored synthetic Event fixtures, DRAFT expected proposals, JSON Schemas, coverage/review metadata, isolated negative fixtures, and an automated integrity linter** for Career Memory **T-001A**.

## Scope & Boundaries

- **Included**:
  - 18 synthetic fixture cases (`cases/EV-001` through `cases/EV-018`: 16 `MVP` + 2 `POST_MVP` tagged `B-003`).
  - 6 isolated intentionally broken negative variants (`negative-fixtures/NEG-001` through `NEG-006`).
  - Strict separation of `synthetic_sources`, `user_commands`, `expected_proposal` (`approval_status="DRAFT"`, `approved_by=null`), and `rationale` (`confirmed_spec_rules` / `unconfirmed_spec_tokens` / `truly_undecided_items`).
  - JSON Schemas (`schemas/manifest.schema.json`, `schemas/case.schema.json`).
  - Coverage matrix (`coverage-matrix.json`, `coverage-matrix.md`) separating draft local IDs (`REQ-EV-*`, `T-001A-*`) from official refs (`FR-006`, `FR-007`, `EVENT-10`, `EVENT-11`, `EVENT-12`) and explicitly deferring `EVAL-EVENT` to M3.
  - T-001B human review queue (`review-queue.json`, `review-queue.md`).
  - Zero-dependency Python fixture linter (`lint/lint_fixtures.py`) and regression test suite (`lint/test_lint_fixtures.py`).
- **Excluded (explicitly NOT in T-001A)**:
  - Event Builder runtime implementation.
  - Human ground-truth review, approval, or golden dataset freeze (**T-001B**).
  - Real Event Builder runtime quality evaluation (**M3 / T-306A / EVAL-EVENT** — never marked passed by fixture lint).
  - Any CLAIM-QA files, shared schemas, CI workflows, or physical database table assumptions (**OD-007**).
  - Any real repository text, real accounts, GitHub/LLM API calls, or billable runs.

## Official Spec Rules Applied vs. Open Items

- **Official rules applied across all 18 cases**:
  - **Anchor modes**: `PR`, `PR_SEGMENT`, `DIRECT_COMMIT`, `USER_COMPOSITE` (`PR` and `DIRECT_COMMIT` auto-generated; default-branch direct commit = 1 candidate per commit when `SUCCESS` initial import + `COMPLETED` lookup `NO_MATCH_OBSERVED` + start/end default-branch verified).
  - **Import runs**: `PARTIAL` (never `PARTIAL_SUCCESS`) promotes `0` Events; `UNKNOWN` is never `NO_MATCH_OBSERVED`; default Event search returns `0`.
  - **Lifecycle statuses**: `CANDIDATE`, `CONFIRMED`, `REJECTED`, `SUPERSEDED`, `STALE` separated from top-level visibility (`is_active_top_level`). No arbitrary `ACTIVE`, `EXCLUDED`, or `DISSOLVED`.
  - **Split**: Mixed-purpose PR stays 1 `CANDIDATE` with a review flag (`auto_split_count=0`); user split requires disjoint `evidence_scope` (`file_paths`, `diff_hunks`, `review_thread_ids`, `content_unit_ids`), emits `PR_SEGMENT` (or `DIRECT_COMMIT`), and marks parent `SUPERSEDED`.
  - **Merge**: Same workspace & same repository across `PR`, `PR_SEGMENT`, `DIRECT_COMMIT` (PR + direct commit allowed); checks each member's Event version; preserves snapshots; marks members `SUPERSEDED`; rolls up member-only search hits once to parent `USER_COMPOSITE`; no auto-inheritance of member Fact/Claim.
  - **Dissolve**: Requires `expected_version`, `reason`, `idempotency_key`; sets Composite `SUPERSEDED` + `composite_membership_status='REMOVED'`; re-evaluates members (`CANDIDATE`/`CONFIRMED`/`STALE`); marks dependent `FACT`, `CLAIM`, and `EXPORT` as `STALE` without deletion or auto-transfer.
  - **Official Event API error codes**: `EVENT_VERSION_CONFLICT`, `CROSS_WORKSPACE_MEMBER`, `CROSS_REPOSITORY_MEMBER`, `DUPLICATE_COMPOSITE_MEMBER`, `NESTED_COMPOSITE_NOT_ALLOWED`, `CANNOT_LINK_CONFLICT`, `MEMBER_NOT_ACTIVE`, `MEMBER_ALREADY_COMPOSED`, `PR_ASSOCIATION_INCOMPLETE`. `CANNOT_LINK` is a relation semantic, not a catch-all wire code.
- **Unconfirmed details (`UNCONFIRMED` / `REVIEW_REQUIRED`)**:
  - Command wire names, exact review flag string/UI, diagnostic record status tokens, HTTP status codes (`409` vs `412`, `If-Match`, unauthorized `403` vs `404`), multi-violation precedence (`MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`), detail GET shape for historical `SUPERSEDED` members, and post-MVP `B-003` transition mechanics remain open in `review-queue.md`.

## Provenance & Reproducibility Limits

- **Base commit**: `efe251071fbee1be9a2ffee680a73f3ca3cd8084` (`main`; note `AGENTS.md` was not present at this commit).
- **Branch / Worktree**: `qa/event-fixtures` (`.worktrees/qa-event-fixtures`).
- **Generation method**: Interactive AI authoring and official-spec correction pass by the EVENT-QA agent; `0` external GitHub, Notion, or scripted LLM API calls.
- **Model / Seed limitation**: The local workspace environment does not expose the underlying model build/checkpoint identifier or a deterministic sampling seed/temperature. Token-for-token regeneration via seed is **not reproducible**; draft reproducibility relies on immutable git commits and deterministic lint checks.

## Running the Fixture Lint & Regression Tests

Run from the worktree root (`.worktrees/qa-event-fixtures`):

```sh
# 1. Lint the full valid draft dataset (manifest, 18 cases, coverage matrix, review queue)
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft

# 2. Run linter against an intentionally broken negative variant (expected: non-zero exit + rule code)
python3 -B evaluation/event-draft/lint/lint_fixtures.py --case-file evaluation/event-draft/negative-fixtures/NEG-001-broken-id-ref.json

# 3. Verify all 6 isolated negative variants fail with their exact expected rule codes
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives

# 4. Run full automated unit & regression test suite
python3 -B -m unittest discover -s evaluation/event-draft/lint -v

# 5. Check whitespace cleanliness
git diff --check
```

> **Note**: These tests validate **synthetic dataset format, referential integrity, governance rules, official-token discipline, and Event-domain safety invariants only**. Passing lint does **not** constitute ground-truth approval / golden freeze (**T-001B**) or Event Builder runtime quality evaluation (**M3 / T-306A / EVAL-EVENT**).

## T-001B Handoff Checklist

1. Review `review-queue.md` Sections 2–3 to fill remaining wire details (`spec_command_name`, HTTP status codes / `If-Match`, exact review flag string) and resolve open contract decisions (multi-violation precedence, unauthorized foreign-workspace HTTP exposure, detail GET history shape).
2. Independently verify all 18 `expected_proposal` objects against human engineering judgment before transitioning `approval_status` from `DRAFT` to frozen golden truth in **T-001B**.
3. Keep `negative-fixtures/` strictly isolated from the frozen evaluation set.
